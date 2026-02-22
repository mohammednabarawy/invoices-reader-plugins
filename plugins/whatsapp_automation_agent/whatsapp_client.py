import os
import sys
import time
import asyncio
import traceback
import hashlib
import base64
import json
import os
import sys
import time
import asyncio
import traceback
import hashlib
import base64
import json
import re
from urllib.parse import quote
from collections import deque
from playwright.async_api import async_playwright
from core.plugins.sdk import get_logger

logger = get_logger(__name__)

class WhatsAppClient:
    def __init__(self, plugin_instance):
        self.plugin = plugin_instance
        self.is_running = False
        self.is_logged_in = False
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.loop = None
        
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.user_data_dir = os.path.join(plugin_dir, "whatsapp_session")
        self.session_dir = self.user_data_dir  # alias for settings_ui
        self.pending_replies = []  # Thread-safe queue for delayed UI feedback
        self._recent_reply_keys = deque(maxlen=500)
        self._recent_reply_lookup = set()
        self._send_lock = asyncio.Lock()
        self._is_frozen_runtime = (
            getattr(sys, "frozen", False)
            or hasattr(sys, "_MEIPASS")
            or hasattr(sys, "__nuitka_binary_dir")
            or "__compiled__" in globals()
        )

    async def _install_playwright_chromium(self) -> bool:
        """Install Playwright Chromium without recursively relaunching frozen app."""
        try:
            if self._is_frozen_runtime:
                from playwright.__main__ import main as playwright_main

                def _run_install():
                    try:
                        # Newer Playwright exposes main(argv)
                        return playwright_main(["install", "chromium"])
                    except TypeError:
                        # Older variants read sys.argv directly.
                        previous_argv = list(sys.argv)
                        try:
                            sys.argv = ["playwright", "install", "chromium"]
                            return playwright_main()
                        finally:
                            sys.argv = previous_argv
                    except SystemExit as exc:
                        return exc.code

                result = await asyncio.to_thread(_run_install)
                return result in (0, None)

            import subprocess

            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                check=False,
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"[WA] Failed to install Playwright Chromium: {e}")
            return False

    def _normalize_download_filename(self, filename_hint: str, default_ext: str = ".pdf") -> str:
        """Sanitize a filename for local filesystem writes."""
        name = (filename_hint or "").strip().replace("\n", " ")
        if name:
            name = os.path.basename(name)
            name = re.sub(r'[\\/:*?"<>|]+', "_", name)

        if not name:
            name = f"whatsapp_document_{int(time.time())}{default_ext}"
        elif "." not in os.path.basename(name):
            name = f"{name}{default_ext}"

        return name

    async def _download_document_blob_fallback(self, message_element, downloads_dir: str):
        """Fallback download path: fetch blob URL bytes from the page context and save locally."""
        blob_url = ""
        filename_hint = ""

        # 1) Prefer links inside the target message.
        if message_element is not None:
            link_candidates = [
                message_element.locator("a[href^='blob:']").first,
                message_element.locator("a[href*='blob:']").first,
            ]
            for link in link_candidates:
                try:
                    if await link.count() == 0:
                        continue
                    href = await link.get_attribute("href")
                    if not href:
                        continue
                    blob_url = href
                    filename_hint = (
                        (await link.get_attribute("download"))
                        or (await link.get_attribute("title"))
                        or ""
                    )
                    if not filename_hint:
                        try:
                            filename_hint = (await link.inner_text()).strip()
                        except Exception:
                            filename_hint = ""
                    break
                except Exception:
                    continue

        # 2) Broader fallback: any visible blob link in main chat area.
        if not blob_url:
            global_link_selectors = [
                "#main a[href^='blob:']",
                "a[href^='blob:']",
                "#main a[download]",
                "a[download]",
            ]
            for global_selector in global_link_selectors:
                try:
                    global_link = self.page.locator(global_selector).last
                    if await global_link.count() == 0:
                        continue
                    blob_url = await global_link.get_attribute("href") or ""
                    filename_hint = (
                        (await global_link.get_attribute("download"))
                        or (await global_link.get_attribute("title"))
                        or filename_hint
                    )
                    if blob_url:
                        break
                except Exception:
                    continue

        if not blob_url:
            return None

        try:
            payload_b64 = await self.page.evaluate(
                """
                async ({url}) => {
                    const response = await fetch(url);
                    if (!response.ok) return null;
                    const buffer = await response.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    const chunk = 0x8000;
                    let binary = "";
                    for (let i = 0; i < bytes.length; i += chunk) {
                        binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
                    }
                    return btoa(binary);
                }
                """,
                {"url": blob_url},
            )
            if not payload_b64:
                return None

            file_name = self._normalize_download_filename(filename_hint or "invoice.pdf", default_ext=".pdf")
            file_path = os.path.join(downloads_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(payload_b64))
            return file_path
        except Exception as e:
            logger.warning(f"[WA] Blob extraction fallback failed: {e}")
            return None

    async def _resolve_incoming_message(self, message_data_id: str = ""):
        """Return a resilient locator for the current target incoming message."""
        if not self.page:
            return None

        if message_data_id:
            try:
                selector_value = json.dumps(message_data_id)
                exact_match = self.page.locator(f"div.message-in[data-id={selector_value}]").last
                if await exact_match.count() > 0:
                    return exact_match

                alt_exact = self.page.locator(f"#main div[data-id={selector_value}]").last
                if await alt_exact.count() > 0:
                    return alt_exact
            except Exception:
                pass

        fallback_selectors = [
            "#main div.message-in",
            "div.message-in",
            "#main div[data-id*='false_']",
            "div[data-id*='false_']",
        ]
        for selector in fallback_selectors:
            try:
                fallback_last = self.page.locator(selector).last
                if await fallback_last.count() > 0:
                    return fallback_last
            except Exception:
                continue

        return None

    def _extract_phone_candidate(self, metadata: dict | None = None, key: str = "") -> str:
        """Return best-effort WhatsApp phone candidate for restoring reply context."""
        safe_metadata = metadata or {}

        direct_phone = str(safe_metadata.get("whatsapp_sender_phone") or "").strip()
        if direct_phone:
            digits = re.sub(r"\D", "", direct_phone)
            if 9 <= len(digits) <= 15:
                return digits

        chat_title = str(safe_metadata.get("whatsapp_chat_title") or "")
        if chat_title:
            for match in re.findall(r"\d{9,15}", chat_title):
                return match

        key_text = str(key or "")
        if key_text:
            for match in re.findall(r"\d{9,15}", key_text):
                return match

        return ""

    async def _restore_reply_context(self, metadata: dict | None = None, key: str = "") -> bool:
        """Try to restore a reply-ready chat view when composer is temporarily unavailable."""
        if not self.page:
            return False

        phone = self._extract_phone_candidate(metadata, key)
        if not phone:
            return False

        try:
            url = f"https://web.whatsapp.com/send/?phone={quote(phone)}&text=&type=phone_number&app_absent=0"
            await self.page.goto(url, timeout=20000)
            await self.page.wait_for_selector("#main", state="visible", timeout=20000)
            await asyncio.sleep(0.4)
            logger.info(f"[WA] Restored reply context using phone hint: {phone}")
            return True
        except Exception as e:
            logger.warning(f"[WA] Failed to restore reply context for phone {phone}: {e}")
            return False

    def run(self):
        """Entry point for the background thread."""
        self.is_running = True
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        try:
            self.loop.run_until_complete(self.async_run())
        except Exception as e:
            logger.error(f"WhatsApp Agent crashed: {e}")
            logger.error(traceback.format_exc())
            self.plugin.update_status(f"Error: {e}")
        finally:
            self.is_running = False
            self.is_logged_in = False
            if self.loop.is_running():
                self.loop.close()

    def stop(self):
        """Signal the background thread to stop."""
        self.is_running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.async_stop(), self.loop)

    async def async_stop(self):
        """Cleanup playwright resources."""
        if self.browser:
            await self.browser.close()
            self.browser = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None

    async def async_run(self):
        """The main async loop running the playwright browser."""
        self.plugin.update_status("Starting browser...")
        
        # Ensure playwright is installed
        try:
            self.playwright = await async_playwright().start()
        except ImportError:
            if self._is_frozen_runtime:
                self.plugin.update_status("Playwright package missing in frozen app build.")
                logger.error(
                    "[WA] Playwright dependency missing in frozen runtime. "
                    "Bundle plugin dependencies instead of runtime pip install."
                )
                return
            self.plugin.update_status("Playwright package missing. Please wait...")
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "playwright"], check=True)
            self.playwright = await async_playwright().start()

        # Launch Chromium with persistent context to save login session
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled', # Avoid bot detection
                    '--no-sandbox',
                    '--disable-setuid-sandbox'
                ],
                viewport={'width': 1280, 'height': 720}
            )
        except Exception as e:
            if "Executable doesn't exist" in str(e) or "playwright install" in str(e).lower():
                self.plugin.update_status("Downloading browser binaries (first time)...")
                if not await self._install_playwright_chromium():
                    self.plugin.update_status("Failed to install Playwright browser binaries.")
                    logger.error("[WA] Playwright browser installation failed.")
                    return
                # Retry launch
                self.context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=self.user_data_dir,
                    headless=True,
                    args=[
                        '--disable-blink-features=AutomationControlled',
                        '--no-sandbox',
                        '--disable-setuid-sandbox'
                    ],
                    viewport={'width': 1280, 'height': 720}
                )
            else:
                raise e
        
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        
        # Set a realistic user agent
        await self.page.set_extra_http_headers({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"})
        
        self.plugin.update_status("Navigating to WhatsApp Web...")
        try:
            await self.page.goto("https://web.whatsapp.com/", timeout=60000)
        except Exception as e:
            self.plugin.update_status("Failed to load WhatsApp Web. Check connection.")
            return

        # Wait for either QR code or successful login
        self.plugin.update_status("Checking login status...")
        
        while self.is_running:
            try:
                # Check if we are logged in (chats pane is visible)
                logged_in = await self.page.locator("div#pane-side").count() > 0
                
                if logged_in:
                    self.is_logged_in = True
                    self.plugin.update_status("Connected and Listening.")
                    # Start polling for new messages
                    await self.poll_messages()
                    break
                    
                # Check for QR code
                qr_canvas = self.page.locator("canvas")
                if await qr_canvas.count() > 0:
                    self.plugin.update_status("Please scan QR code to connect...")
                    
                    # You could optionally screenshot the QR and show it in the UI, 
                    # but WhatsApp updates the QR frequently. 
                    # A better way is to show the screenshot in the UI dynamically.
                    # For now, we just tell the user to wait or we take a screenshot 
                    # and save it to the plugin folder for the UI to load.
                    qr_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr.png")
                    await qr_canvas.first.screenshot(path=qr_path)
                    self.plugin.update_status(f"QR Ready. Open {qr_path} to scan.")

                # Wait before checking again
                await asyncio.sleep(2)
                
            except Exception as e:
                logger.warning(f"Error during login check loop: {e}")
                await asyncio.sleep(5)

    async def poll_messages(self):
        """Polls for new unread messages in the chat list."""
        logger.info("WhatsApp Agent ready for messages.")
        
        # Temporary directory to save downloads
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        downloads_dir = os.path.join(plugin_dir, "downloads")
        os.makedirs(downloads_dir, exist_ok=True)
        
        while self.is_running:
            try:
                unread_chats = await self._collect_unread_badges()
                
                # Use a set to avoid processing the same chat multiple times if selectors overlap
                # (Playwright locators are unhashable though, so just process them and they'll be read)
                processed_in_this_loop = False
                
                for chat in unread_chats:
                    try:
                        # Open the unread chat by clicking its row (not the unread badge itself).
                        if not await self._open_chat_from_badge(chat):
                            continue
                            
                        await asyncio.sleep(1.5) # wait for messages to load and read state to update
                        processed_in_this_loop = True
                        
                        # Apply Sender Filtering & Extract Chat Header unconditionally
                        header_title = ""
                        allowed_sender = self.plugin.get_setting('allowed_sender', "")
                        
                        try:
                            # Look for chat title in the open chat header
                            header_element = self.page.locator("#main header").first
                            if await header_element.count() > 0:
                                # Strategy 1: The standard span[dir='auto'] usually strictly holds the name/number
                                span_auto = header_element.locator("span[dir='auto']").first
                                if await span_auto.count() > 0:
                                    header_title = await span_auto.inner_text()
                                    logger.info(f"[WA] Strategy 1 extracted: '{header_title}'")
                                    
                                # Strategy 2: Look for elements with a title attribute, discarding "profile details"
                                if not header_title or header_title.lower().strip() == "profile details":
                                    elements_with_title = await header_element.locator("[title]").all()
                                    for el in elements_with_title:
                                        t = await el.get_attribute("title")
                                        logger.info(f"[WA] Strategy 2 inspecting title attribute: '{t}'")
                                        if t and t.strip() and t.lower().strip() != "profile details" and not t.lower().strip().startswith("profile"):
                                            header_title = t
                                            break
                                            
                                # Strategy 3: Raw text extraction, taking the first valid line
                                if not header_title or header_title.lower().strip() == "profile details":
                                    full_text = await header_element.inner_text()
                                    logger.info(f"[WA] Strategy 3 raw header inner_text: '{full_text}'")
                                    if full_text:
                                        for line in full_text.split('\n'):
                                            line = line.strip()
                                            if line and line.lower() != "profile details" and not line.lower().startswith("profile"):
                                                header_title = line
                                                break
                                                
                                # Strategy 4 (OpenClaw style): Extract actual raw phone number from incoming message data-ids, bypassing contact names
                                try:
                                    # Wait a moment for messages to load in the pane
                                    msg_locator = self.page.locator("div.message-in, div[data-id*='false_']")
                                    msg_count = await msg_locator.count()
                                    logger.info(f"[WA] Strategy 4 found {msg_count} incoming messages in pane.")
                                    
                                    if msg_count > 0:
                                        # Check the last few messages to find a valid data-id
                                        for i in range(msg_count - 1, max(-1, msg_count - 5), -1):
                                            msg_element = msg_locator.nth(i)
                                            data_id = await msg_element.get_attribute("data-id")
                                            if data_id:
                                                import re
                                                # Pattern usually looks like "false_966592328502@c.us_..."
                                                number_match = re.search(r'false_(\d+)(?:@c\.us|@s\.whatsapp\.net|@g\.us)', data_id)
                                                if number_match:
                                                    extracted_number = number_match.group(1)
                                                    logger.info(f"[WA] Strategy 4 successfully extracted raw phone number: '{extracted_number}' from '{data_id}'")
                                                    # We append this raw number to the header title to guarantee a match against the user's settings!
                                                    header_title += " " + extracted_number
                                                    break
                                except Exception as e:
                                    logger.warning(f"[WA] Strategy 4 failed to extract raw number: {e}")
                        except Exception as e:
                            logger.warning(f"Could not read chat header: {e}")

                        # Continue with sender filtering if setting exists
                        if allowed_sender and allowed_sender.strip():
                            if not header_title:
                                logger.info(f"Skipping messages: empty chat title extracted (matched against allowed '{allowed_sender}')")
                                continue
                                
                            import re
                            
                            def normalize_for_match(val):
                                val = val.lower().strip()
                                digits = re.sub(r'\D', '', val)
                                if len(digits) >= 7:
                                    # It's likely a phone number. 
                                    # Discard country codes and leading zeros by taking the last 9 digits.
                                    # If it's shorter than 9, just take as many as we confidently have.
                                    return digits[-9:]
                                else:
                                    # It's a contact name, strip spaces and symbols
                                    return re.sub(r'[^a-z0-9]', '', val)
                                    
                            normalized_allowed = normalize_for_match(allowed_sender)
                            normalized_header = normalize_for_match(header_title)
                            
                            if normalized_allowed not in normalized_header and normalized_header not in normalized_allowed:
                                logger.info(f"Skipping messages: chat '{header_title}' doesn't match allowed sender '{allowed_sender}'")
                                continue
                                
                        # Get incoming messages from the active chat only.
                        incoming_messages = self.page.locator("div.message-in")
                        incoming_count = await incoming_messages.count()
                        if incoming_count > 0:
                            last_message = incoming_messages.nth(incoming_count - 1)
                            message_data_id = (await last_message.get_attribute("data-id")) or ""
                            message_target = await self._resolve_incoming_message(message_data_id)
                            if not message_target:
                                logger.warning("[WA] Incoming message disappeared before processing; skipping this cycle.")
                                continue

                            has_downloaded = False
                            message_key = await self._get_message_key(message_target, header_title)
                            
                            # 1. Check for documents/files with robust selectors.
                            # WhatsApp often hides document download buttons until message hover.
                            try:
                                await message_target.hover(timeout=1200)
                            except Exception:
                                pass

                            document_download_selectors = [
                                "span[data-icon='down']",
                                "span[data-icon='arrow-down']",
                                "span[data-icon='download']",
                                "span[data-icon='ic-download']",
                                "div[role='button'][aria-label='Download']",
                                "div[role='button'][aria-label='تنزيل']",
                                "button[aria-label='Download']",
                                "button[aria-label='تنزيل']",
                            ]
                            download_btn = None
                            for selector in document_download_selectors:
                                candidate = message_target.locator(selector).first
                                if await candidate.count() > 0:
                                    download_btn = candidate
                                    break

                            document_indicators = [
                                "span[data-icon='document']",
                                "span[data-icon='doc']",
                                "div[aria-label*='Document']",
                                "div[aria-label*='مستند']",
                                "span:has-text('.pdf')",
                            ]
                            is_document_like = False
                            for selector in document_indicators:
                                indicator = message_target.locator(selector).first
                                if await indicator.count() > 0:
                                    is_document_like = True
                                    break

                            if download_btn or is_document_like:
                                logger.info("Found downloadable media attachment.")
                                # Avoid typing a reply before document download. Sending UI actions here can
                                # trigger list rerenders and make the document bubble disappear from DOM.

                                file_path = None
                                last_download_error = None
                                try:
                                    if download_btn:
                                        for force_click in (False, True):
                                            try:
                                                async with self.page.expect_download(timeout=15000) as download_info:
                                                    await download_btn.click(timeout=4000, force=force_click)
                                                download = await download_info.value
                                                file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                await download.save_as(file_path)
                                                break
                                            except Exception as click_error:
                                                last_download_error = click_error
                                                mode = "force-click" if force_click else "normal click"
                                                logger.warning(f"[WA] Direct document download via {mode} failed: {click_error}")

                                    # Fallback: open message menu and click Download for document bubbles.
                                    if not file_path and is_document_like:
                                        logger.info("[WA] Trying document download fallback via message menu.")
                                        menu_openers = [
                                            "span[data-icon='ic-chevron-down-menu']",
                                            "span[data-icon='down-context']",
                                            "div[role='button'][aria-label='Menu']",
                                            "div[role='button'][aria-label='القائمة']",
                                        ]
                                        menu_opened = False
                                        for opener_selector in menu_openers:
                                            opener = message_target.locator(opener_selector).first
                                            if await opener.count() == 0:
                                                continue
                                            try:
                                                await opener.click(timeout=2500)
                                                menu_opened = True
                                                break
                                            except Exception:
                                                try:
                                                    await opener.click(timeout=2500, force=True)
                                                    menu_opened = True
                                                    break
                                                except Exception:
                                                    continue

                                        if menu_opened:
                                            menu_download_selectors = [
                                                "div[role='button']:has-text('Download')",
                                                "div[role='button']:has-text('تنزيل')",
                                                "li:has-text('Download')",
                                                "li:has-text('تنزيل')",
                                            ]
                                            for menu_selector in menu_download_selectors:
                                                menu_item = self.page.locator(menu_selector).first
                                                if await menu_item.count() == 0:
                                                    continue
                                                try:
                                                    async with self.page.expect_download(timeout=15000) as download_info:
                                                        await menu_item.click(timeout=3000)
                                                    download = await download_info.value
                                                    file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                    await download.save_as(file_path)
                                                    break
                                                except Exception as menu_error:
                                                    last_download_error = menu_error
                                                    logger.warning(f"[WA] Menu download attempt failed via '{menu_selector}': {menu_error}")
                                                    continue

                                        try:
                                            await self.page.keyboard.press("Escape")
                                            await asyncio.sleep(0.2)
                                        except Exception:
                                            pass

                                    # Final fallback: open document bubble viewer and click top-bar download.
                                    if not file_path and is_document_like:
                                        logger.info("[WA] Trying document download fallback via viewer open.")
                                        opened_viewer = False
                                        viewer_targets = [
                                            message_target.locator("span[data-icon='document']").first,
                                            message_target.locator("div[aria-label*='Document']").first,
                                            message_target.locator("div[aria-label*='مستند']").first,
                                            message_target.locator("div[role='button']").first,
                                            message_target,
                                        ]
                                        for target in viewer_targets:
                                            try:
                                                if await target.count() == 0:
                                                    continue
                                                await target.click(timeout=3000)
                                                opened_viewer = True
                                                break
                                            except Exception:
                                                try:
                                                    await target.click(timeout=3000, force=True)
                                                    opened_viewer = True
                                                    break
                                                except Exception:
                                                    continue

                                        if opened_viewer:
                                            await asyncio.sleep(1.2)
                                            viewer_download_selectors = [
                                                "div[role='button'][aria-label='Download']",
                                                "div[role='button'][aria-label='تنزيل']",
                                                "span[data-icon='download']",
                                                "span[data-icon='ic-download']",
                                                "button[title='Download']",
                                                "div[title='Download']",
                                                "div[title='تنزيل']",
                                            ]
                                            for selector in viewer_download_selectors:
                                                viewer_btn = self.page.locator(selector).first
                                                if await viewer_btn.count() == 0:
                                                    continue
                                                try:
                                                    async with self.page.expect_download(timeout=15000) as download_info:
                                                        await viewer_btn.click(timeout=3000)
                                                    download = await download_info.value
                                                    file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                    await download.save_as(file_path)
                                                    break
                                                except Exception as viewer_error:
                                                    last_download_error = viewer_error
                                                    logger.warning(f"[WA] Viewer download attempt failed via '{selector}': {viewer_error}")
                                                    continue

                                        try:
                                            await self.page.keyboard.press("Escape")
                                            await asyncio.sleep(0.2)
                                        except Exception:
                                            pass

                                    # Additional fallback: some builds trigger download by clicking document bubble itself.
                                    if not file_path and is_document_like:
                                        logger.info("[WA] Trying direct document bubble download fallback.")
                                        for force_click in (False, True):
                                            try:
                                                live_target = await self._resolve_incoming_message(message_data_id)
                                                if not live_target:
                                                    try:
                                                        await self.page.keyboard.press("Escape")
                                                        await asyncio.sleep(0.15)
                                                    except Exception:
                                                        pass
                                                    await self._restore_reply_context(
                                                        {"whatsapp_chat_title": header_title or ""},
                                                        message_key
                                                    )
                                                    live_target = await self._resolve_incoming_message(message_data_id)
                                                if not live_target:
                                                    live_target = await self._resolve_incoming_message("")
                                                if not live_target:
                                                    raise RuntimeError("Incoming message target is no longer available")
                                                async with self.page.expect_download(timeout=12000) as download_info:
                                                    await live_target.click(timeout=3000, force=force_click)
                                                download = await download_info.value
                                                file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                await download.save_as(file_path)
                                                break
                                            except Exception as bubble_error:
                                                last_download_error = bubble_error
                                                mode = "force-click" if force_click else "normal click"
                                                logger.warning(f"[WA] Direct bubble download via {mode} failed: {bubble_error}")

                                    # Event-based fallback: capture any download regardless of exact trigger selector.
                                    if not file_path and is_document_like:
                                        logger.info("[WA] Trying event-based download fallback.")
                                        download_state = {"download": None}

                                        def _on_download(download):
                                            if download_state["download"] is None:
                                                download_state["download"] = download

                                        try:
                                            self.page.on("download", _on_download)
                                        except Exception:
                                            _on_download = None

                                        trigger_selectors = [
                                            "div[role='button'][aria-label='Download']",
                                            "div[role='button'][aria-label='تنزيل']",
                                            "span[data-icon='download']",
                                            "span[data-icon='ic-download']",
                                            "#main a[download]",
                                            "a[download]",
                                        ]

                                        for trigger_selector in trigger_selectors:
                                            if file_path:
                                                break
                                            try:
                                                trigger = self.page.locator(trigger_selector).first
                                                if await trigger.count() == 0:
                                                    continue
                                                await trigger.click(timeout=2500, force=True)
                                            except Exception as trigger_error:
                                                last_download_error = trigger_error
                                                continue

                                            for _ in range(15):
                                                if download_state["download"] is not None:
                                                    break
                                                await asyncio.sleep(0.2)

                                            if download_state["download"] is not None:
                                                try:
                                                    download = download_state["download"]
                                                    file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                    await download.save_as(file_path)
                                                    break
                                                except Exception as save_error:
                                                    last_download_error = save_error
                                                    file_path = None
                                                    continue

                                        if _on_download is not None:
                                            try:
                                                self.page.remove_listener("download", _on_download)
                                            except Exception:
                                                pass

                                    # Last-resort fallback: pull blob bytes directly from the DOM/page.
                                    if not file_path and is_document_like:
                                        logger.info("[WA] Trying document download fallback via blob extraction.")
                                        live_target = await self._resolve_incoming_message(message_data_id)
                                        if live_target:
                                            message_target = live_target
                                        elif message_target is None:
                                            await self._restore_reply_context(
                                                {"whatsapp_chat_title": header_title or ""},
                                                message_key
                                            )
                                            message_target = await self._resolve_incoming_message("")
                                        file_path = await self._download_document_blob_fallback(
                                            message_target,
                                            downloads_dir
                                        )

                                    if file_path:
                                        logger.info(f"Downloaded media document: {file_path}")
                                        has_downloaded = True

                                        # Forward to main app via API
                                        if hasattr(self.plugin.api, 'processing'):
                                            wa_metadata = {
                                                'whatsapp_message_key': message_key,
                                                'whatsapp_chat_title': header_title or "",
                                                'whatsapp_sender_phone': self._extract_phone_candidate(
                                                    {'whatsapp_chat_title': header_title or ""},
                                                    message_key
                                                )
                                            }
                                            enqueued = self.plugin.api.processing.import_file_to_queue(
                                                file_path,
                                                "WhatsApp",
                                                metadata=wa_metadata
                                            )
                                            if enqueued:
                                                await self._reply_once(
                                                    f"{message_key}:queued",
                                                    "📥 Invoice received and added to processing queue."
                                                )
                                            else:
                                                await self._reply_once(
                                                    f"{message_key}:queue_failed",
                                                    "❌ Failed to add invoice to queue."
                                                )
                                        else:
                                            await self._reply_once(
                                                f"{message_key}:queue_failed",
                                                "❌ Failed to add invoice to queue."
                                            )
                                    else:
                                        if last_download_error:
                                            logger.warning(f"[WA] Document download failed after all fallbacks: {last_download_error}")
                                        await self._reply_once(
                                            f"{message_key}:download_failed",
                                            "❌ Download failed."
                                        )
                                except Exception as e:
                                    logger.error(f"Error downloading media document: {e}")
                            # 2. Check for displayed images (WhatsApp strips direct download buttons from displayed images)
                            if not has_downloaded:
                                img_element = message_target.locator("img[src^='blob:']").first
                                if await img_element.count() > 0 and await img_element.is_visible():
                                    logger.info("Found image message.")
                                    await self._reply_once(
                                        f"{message_key}:downloading",
                                        "⏳ Downloading invoice..."
                                    )
                                    try:
                                        # Click image to open the media viewer.
                                        # WhatsApp DOM is dynamic; image nodes can detach between locate and click.
                                        opened_viewer = False
                                        click_targets = [
                                            img_element,
                                            self.page.locator("#main div.message-in img[src^='blob:']").last
                                        ]
                                        for idx, target in enumerate(click_targets, start=1):
                                            try:
                                                if await target.count() == 0:
                                                    continue
                                                try:
                                                    await target.scroll_into_view_if_needed()
                                                except Exception:
                                                    pass

                                                try:
                                                    await target.click(timeout=4000)
                                                except Exception as click_error:
                                                    logger.warning(
                                                        f"[WA] Image click attempt {idx} failed, retrying force-click: {click_error}"
                                                    )
                                                    await target.click(timeout=4000, force=True)

                                                opened_viewer = True
                                                break
                                            except Exception as e:
                                                logger.warning(f"[WA] Image click attempt {idx} failed: {e}")

                                        if not opened_viewer:
                                            raise RuntimeError("Could not open image viewer from incoming message")

                                        await asyncio.sleep(1.5)  # Allow viewer overlay to settle.
                                        
                                        # Locate the download button in the viewer using multiple robust strategies
                                        btn_selectors = [
                                            "div[role='button'][aria-label='Download']",
                                            "div[role='button'][aria-label='تنزيل']",
                                            "span[data-icon='download']",
                                            "span[data-icon='ic-download']",
                                            "button[title='Download']",
                                            "div[title='Download']",
                                            "div[title='تنزيل']"
                                        ]
                                        
                                        viewer_download_btn = None
                                        for selector in btn_selectors:
                                            btn = self.page.locator(selector).first
                                            if await btn.count() > 0:
                                                logger.info(f"[WA] Found potential download button in viewer: '{selector}'")
                                                viewer_download_btn = btn
                                                break
                                                
                                        if viewer_download_btn and await viewer_download_btn.is_visible():
                                            try:
                                                async with self.page.expect_download(timeout=15000) as download_info:
                                                    await viewer_download_btn.click()
                                                download = await download_info.value
                                                
                                                file_path = os.path.join(downloads_dir, download.suggested_filename)
                                                await download.save_as(file_path)
                                                logger.info(f"Downloaded image: {file_path}")
                                                has_downloaded = True
                                                
                                                # Close viewer before sending acknowledgements.
                                                try:
                                                    await self.page.keyboard.press("Escape")
                                                    await asyncio.sleep(0.3)
                                                except Exception:
                                                    pass

                                                # Queue for processing
                                                if hasattr(self.plugin.api, 'processing'):
                                                    wa_metadata = {
                                                        'whatsapp_message_key': message_key,
                                                        'whatsapp_chat_title': header_title or "",
                                                        'whatsapp_sender_phone': self._extract_phone_candidate(
                                                            {'whatsapp_chat_title': header_title or ""},
                                                            message_key
                                                        )
                                                    }
                                                    enqueued = self.plugin.api.processing.import_file_to_queue(
                                                        file_path,
                                                        "WhatsApp",
                                                        metadata=wa_metadata
                                                    )
                                                    if enqueued:
                                                        await self._reply_once(
                                                            f"{message_key}:queued",
                                                            "📥 Invoice received and added to processing queue."
                                                        )
                                                    else:
                                                        await self._reply_once(
                                                            f"{message_key}:queue_failed",
                                                            "❌ Failed to add invoice to queue."
                                                        )
                                                else:
                                                    await self._reply_once(
                                                        f"{message_key}:queue_failed",
                                                        "❌ Failed to add invoice to queue."
                                                    )
                                            except Exception as e:
                                                logger.error(f"Failed during download trigger in viewer: {e}")
                                                await self._reply_once(
                                                    f"{message_key}:download_failed",
                                                    "❌ Download failed."
                                                )
                                        else:
                                            logger.warning("Could not find visible download button in image viewer after 2s.")
                                            await self._reply_once(
                                                f"{message_key}:download_failed",
                                                "❌ Download failed."
                                            )
                                            # Diagnostic: list potential icons in the header
                                            try:
                                                icons = await self.page.locator("span[data-icon]").all()
                                                icon_names = [await i.get_attribute("data-icon") for i in icons]
                                                logger.info(f"[WA] Diagnostic - All icons on screen: {icon_names}")
                                            except:
                                                pass
                                            
                                        # Ensure viewer is closed.
                                        await self.page.keyboard.press("Escape")
                                        await asyncio.sleep(0.5)
                                    except Exception as e:
                                        logger.error(f"Error handling image viewer: {e}")
                                        await self._reply_once(
                                            f"{message_key}:download_failed",
                                            "❌ Download failed."
                                        )
                                        # Ensure viewer is closed
                                        await self.page.keyboard.press("Escape")

                            # 3. Read any text attached
                            text_element = message_target.locator("span.selectable-text")
                            msg_text = ""
                            if await text_element.count() > 0:
                                msg_text = await text_element.first.inner_text()
                                logger.info(f"Received WhatsApp Message: {msg_text}")
                                
                            # 4. Auto-reply for text mode (file acknowledgements are always-on above)
                            if msg_text and not has_downloaded:
                                bot_val = self.plugin.get_setting('bot_mode', False, type=bool)
                                if bot_val:
                                    await self._reply_once(
                                        f"{message_key}:bot_reply",
                                        "I received your message! I am the Invoices Reader AI agent."
                                    )
                                    
                    except Exception as e:
                        error_text = str(e)
                        if "intercepts pointer events" in error_text or "Timeout" in error_text:
                            logger.debug(f"Skipping busy chat row in this cycle: {error_text}")
                        else:
                            logger.warning(f"Error checking individual message: {e}")
                
                # If we processed chats, maybe wait a bit less
                if processed_in_this_loop:
                    await asyncio.sleep(2)
                else:
                    await asyncio.sleep(5)
                
                # Check for pending replies (e.g., from duplicate / error signals sent from another thread)
                while self.pending_replies:
                    reply_task = self.pending_replies.pop(0)
                    recipient = reply_task.get('recipient')
                    message = reply_task.get('message')
                    if recipient and message:
                        logger.info(f"Processing pending reply to {recipient}")
                        await self.send_message_to_chat_safely(recipient, message)
                        await asyncio.sleep(3) # Wait before next action
                
            except Exception as e:
                # Silently catch broad scraping errors to keep the loop resilient
                await asyncio.sleep(5)

    async def _collect_unread_badges(self):
        """Collect unread badges and deduplicate by chat row when possible."""
        unread_selectors = [
            "div[aria-label*='unread message']",
            "div[aria-label*='رسالة غير مقروءة']",
            "span[aria-label*='unread message']",
            "span[aria-label*='رسالة غير مقروءة']"
        ]
        unread_badges = []
        seen_row_keys = set()

        for selector in unread_selectors:
            elements = await self.page.locator(selector).all()
            for badge in elements:
                row_key = None
                try:
                    row = badge.locator("xpath=ancestor::div[@role='listitem'][1]").first
                    if await row.count() > 0:
                        row_key = await row.get_attribute("data-id")
                        if not row_key:
                            box = await row.bounding_box()
                            if box:
                                row_key = f"{int(box['x'])}:{int(box['y'])}"
                except Exception:
                    row_key = None

                if row_key and row_key in seen_row_keys:
                    continue
                if row_key:
                    seen_row_keys.add(row_key)
                unread_badges.append(badge)

        return unread_badges

    async def _open_chat_from_badge(self, badge):
        """Open chat row that owns an unread badge with resilient click fallbacks."""
        click_targets = [
            badge.locator("xpath=ancestor::div[@role='listitem'][1]").first,
            badge.locator("xpath=ancestor::div[@role='button'][1]").first,
            badge.first
        ]
        last_error = None

        for target in click_targets:
            try:
                if await target.count() == 0:
                    continue
                if not await target.is_visible():
                    continue
                try:
                    await target.scroll_into_view_if_needed()
                except Exception:
                    pass

                try:
                    await target.click(timeout=3000)
                    return True
                except Exception as click_error:
                    last_error = click_error
                    await target.click(timeout=3000, force=True)
                    return True
            except Exception as target_error:
                last_error = target_error

        # Fallback to JS click if Playwright's click failed
        try:
            handle = await badge.element_handle()
            if handle:
                clicked = await self.page.evaluate(
                    """
                    (el) => {
                        const target = el.closest("div[role='listitem']") || el.closest("div[role='button']") || el.parentElement;
                        if (!target) return false;
                        target.click();
                        return true;
                    }
                    """,
                    handle
                )
                if clicked:
                    return True
        except Exception as js_error:
            last_error = js_error

        if last_error:
            logger.debug(f"Could not open unread chat row this cycle: {last_error}")
        return False

    def queue_reply(self, recipient: str, message: str):
        """Thread-safe way to queue a reply to be sent by the asyncio loop."""
        self.pending_replies.append({
            'recipient': recipient,
            'message': message
        })

    async def _get_message_key(self, message_element, header_title: str = "") -> str:
        """Create a stable key for deduplicating auto-replies."""
        try:
            data_id = await message_element.get_attribute("data-id")
            if data_id:
                return data_id
        except Exception:
            pass

        safe_header = (header_title or "unknown_chat").strip().lower().replace(" ", "_")

        try:
            pre_plain = await message_element.get_attribute("data-pre-plain-text")
            if pre_plain:
                digest = hashlib.sha1(pre_plain.encode("utf-8", "ignore")).hexdigest()[:12]
                return f"fallback:{safe_header}:{digest}"
        except Exception:
            pass

        try:
            message_text = await message_element.inner_text()
            if message_text:
                digest = hashlib.sha1(message_text.encode("utf-8", "ignore")).hexdigest()[:12]
                return f"fallback:{safe_header}:{digest}"
        except Exception:
            pass

        return f"fallback:{safe_header}:{int(time.time())}"

    def _mark_reply_key(self, key: str):
        """Track sent reply keys with bounded memory."""
        if not key:
            return

        if key in self._recent_reply_lookup:
            return

        if len(self._recent_reply_keys) == self._recent_reply_keys.maxlen:
            evicted = self._recent_reply_keys[0]
            self._recent_reply_lookup.discard(evicted)

        self._recent_reply_keys.append(key)
        self._recent_reply_lookup.add(key)

    async def _reply_once(self, key: str, text: str, metadata: dict | None = None):
        """Send a reply once per key within the current agent session."""
        if key in self._recent_reply_lookup:
            return

        async def _attempt_send(max_attempts: int, delay_seconds: float) -> bool:
            for attempt in range(1, max_attempts + 1):
                try:
                    sent = await self.auto_reply(text, metadata=metadata, reply_key=key)
                    if sent:
                        return True
                except Exception as send_error:
                    logger.warning(f"Auto-reply attempt {attempt}/{max_attempts} failed for key '{key}': {send_error}")
                if attempt < max_attempts:
                    await asyncio.sleep(delay_seconds)
            return False

        try:
            # Immediate retries handle transient UI states (media overlay, focus changes).
            sent = await _attempt_send(max_attempts=4, delay_seconds=0.35)
            if sent:
                self._mark_reply_key(key)
                return

            # One deferred retry window for late async completions (e.g. processing result).
            logger.warning(
                f"Auto-reply postponed for key '{key}': chat input not available after immediate retries."
            )

            async def _deferred_retry():
                await asyncio.sleep(2.0)
                if key in self._recent_reply_lookup:
                    return
                deferred_sent = await _attempt_send(max_attempts=3, delay_seconds=0.5)
                if deferred_sent:
                    self._mark_reply_key(key)
                else:
                    logger.warning(f"Auto-reply skipped for key '{key}': chat input not available.")

            asyncio.create_task(_deferred_retry())
        except Exception as e:
            logger.error(f"Failed to send deduplicated auto-reply: {e}")

    def notify_duplicate(self, existing_data: dict, metadata: dict | None = None):
        """Schedule duplicate-notification reply to the active WhatsApp chat."""
        if not self.is_running or not self.loop or not self.loop.is_running():
            return

        safe_metadata = metadata or {}

        async def _send_notice():
            def _clean(val, fallback="N/A"):
                if val is None:
                    return fallback
                text = str(val).strip()
                if not text or text.lower() in {"none", "null", "nan"}:
                    return fallback
                if text in {"0", "0.0"}:
                    return fallback
                return text

            vendor = _clean((existing_data or {}).get('vendor_name'), "Unknown")
            inv_num = _clean((existing_data or {}).get('invoice_number'))
            inv_date = _clean((existing_data or {}).get('date'))
            total = (existing_data or {}).get('invoice_total')
            currency = _clean((existing_data or {}).get('currency'), "")
            amount_str = f"{total} {currency}".strip() if total not in (None, "") else "N/A"

            duplicate_msg = "\n".join([
                "⚠️ Duplicate invoice detected",
                f"🏢 Vendor: {vendor}",
                f"🧾 Invoice #: {inv_num}",
                f"📅 Date: {inv_date}",
                f"💰 Total: {amount_str}",
                "No new action was taken.",
            ])

            message_key = safe_metadata.get('whatsapp_message_key') or "wa_duplicate"
            logger.info(
                f"[WA] Sending duplicate notice (message_key={message_key}, invoice={inv_num}, vendor={vendor})"
            )
            await self._reply_once(f"{message_key}:duplicate", duplicate_msg, safe_metadata)

        try:
            asyncio.run_coroutine_threadsafe(_send_notice(), self.loop)
        except Exception as e:
            logger.error(f"Failed to schedule duplicate WhatsApp notice: {e}")

    def notify_processing_result(self, data: dict, metadata: dict | None = None):
        """Send a completion reply with extracted invoice details."""
        if not self.is_running or not self.loop or not self.loop.is_running():
            return

        safe_metadata = metadata or {}
        safe_data = data or {}

        async def _send_notice():
            def _clean(val, fallback="N/A"):
                if val is None:
                    return fallback
                text = str(val).strip()
                if not text or text.lower() in {"none", "null", "nan"}:
                    return fallback
                if text in {"0", "0.0"}:
                    return fallback
                return text

            vendor = _clean(safe_data.get('vendor_name'), "Unknown")
            inv_num = _clean(safe_data.get('invoice_number'))
            inv_date = _clean(safe_data.get('date'))
            total = safe_data.get('invoice_total')
            currency = _clean(safe_data.get('currency'), "")
            phase = _clean(safe_data.get('einvoice_phase') or safe_data.get('qr_phase'), "")
            compatible = safe_data.get('einvoice_compatible')

            amount_str = f"{total} {currency}".strip() if total not in (None, "") else "N/A"
            if compatible is True or compatible == 1:
                zatca = "Compliant"
            elif compatible is False or compatible == 0:
                zatca = "Not compliant"
            else:
                zatca = "Unknown"

            lines = [
                "✅ Invoice processed successfully",
                f"🏢 Vendor: {vendor}",
                f"🧾 Invoice #: {inv_num}",
                f"📅 Date: {inv_date}",
                f"💰 Total: {amount_str}",
            ]
            if phase:
                lines.append(f"🧾 ZATCA Phase: {phase}")
            lines.append(f"🧪 ZATCA: {zatca}")

            message_key = safe_metadata.get('whatsapp_message_key') or "wa_processed"
            logger.info(
                f"[WA] Sending processing result notice (message_key={message_key}, invoice={inv_num}, vendor={vendor})"
            )
            await self._reply_once(f"{message_key}:processed", "\n".join(lines), safe_metadata)

        try:
            asyncio.run_coroutine_threadsafe(_send_notice(), self.loop)
        except Exception as e:
            logger.error(f"Failed to schedule processing result notice: {e}")

    def notify_processing_failed(self, error: str, metadata: dict | None = None):
        """Send a processing-failed reply."""
        if not self.is_running or not self.loop or not self.loop.is_running():
            return

        safe_metadata = metadata or {}
        safe_error = str(error) if error else "Unknown processing error"

        async def _send_notice():
            message_key = safe_metadata.get('whatsapp_message_key') or "wa_failed"
            reply_text = (
                "❌ Failed to process invoice.\n"
                f"Error: {safe_error}"
            )
            logger.info(f"[WA] Sending processing failed notice (message_key={message_key})")
            await self._reply_once(f"{message_key}:failed", reply_text, safe_metadata)

        try:
            asyncio.run_coroutine_threadsafe(_send_notice(), self.loop)
        except Exception as e:
            logger.error(f"Failed to schedule processing failed notice: {e}")

    async def _find_chat_input(self):
        """Find the visible chat composer element in the currently open chat."""
        selectors = [
            "#main footer div[contenteditable='true'][role='textbox']",
            "#main footer div[contenteditable='true']",
            "footer div[contenteditable='true'][role='textbox']",
            "footer div[contenteditable='true']",
            "div[contenteditable='true'][data-tab='10']",
            "div[contenteditable='true'][data-tab='6']",
            "div[title='Type a message']",
            "div[title='اكتب رسالة']",
        ]

        # Pass 1: prefer visible candidates.
        for selector in selectors:
            candidate = self.page.locator(selector).first
            try:
                if await candidate.count() > 0 and await candidate.is_visible():
                    return candidate
            except Exception:
                continue

        # Pass 2: fallback to existing nodes even if visibility probe is unstable.
        for selector in selectors:
            candidate = self.page.locator(selector).first
            try:
                if await candidate.count() > 0:
                    return candidate
            except Exception:
                continue
        return None

    async def auto_reply(self, text: str, metadata: dict | None = None, reply_key: str = "") -> bool:
        """Types and sends a message in the currently open chat."""
        try:
            normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
            if not normalized_text:
                return False

            async with self._send_lock:
                input_box = await self._find_chat_input()
                if not input_box:
                    # If overlay blocks input (e.g., media viewer), close it and retry once.
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.2)
                    input_box = await self._find_chat_input()

                if not input_box:
                    restored = await self._restore_reply_context(metadata, reply_key)
                    if restored:
                        input_box = await self._find_chat_input()

                if not input_box:
                    return False

                await input_box.click(timeout=2000)
                await self.page.keyboard.press("ControlOrMeta+A")
                await self.page.keyboard.press("Backspace")

                # Insert multiline text safely: Shift+Enter adds line breaks without sending.
                lines = normalized_text.split("\n")
                for idx, line in enumerate(lines):
                    if line:
                        await self.page.keyboard.insert_text(line)
                    if idx < len(lines) - 1:
                        await self.page.keyboard.down("Shift")
                        await self.page.keyboard.press("Enter")
                        await self.page.keyboard.up("Shift")

                await self.page.keyboard.press("Enter")
                logger.info("Sent auto-reply.")
                return True
        except Exception as e:
            logger.error(f"Failed to auto-reply: {e}")
            return False

    async def send_message_to_chat_safely(self, chat_name: str, message: str) -> bool:
        """Sends a message either by direct phone navigation OR by searching the contact name."""
        try:
            import re
            phone_candidates = re.findall(r'\d{7,20}', chat_name)
            if phone_candidates:
                target = phone_candidates[-1]
                logger.info(f"Sending via phone navigation to extracted number: {target}")
                success, _ = await self.send_invoice_async(target, message, None)
                return success

            # If no obvious phone number, use the search box
            logger.info(f"Using search to find contact/group: {chat_name}")
            search_box = self.page.locator("div[title='Search input textbox'], div[title='مربع نص البحث في جهات الاتصال'], div[title='Search']").first
            if await search_box.count() == 0:
                search_box = self.page.locator("div.lexical-rich-text-input > div").first
                
            if await search_box.count() > 0:
                await search_box.click()
                await self.page.keyboard.press("Control+A")
                await self.page.keyboard.press("Backspace")
                await search_box.fill(chat_name)
                await asyncio.sleep(2)
                
                result = self.page.locator("div[role='listitem']").first
                if await result.count() > 0:
                    await result.click()
                    await asyncio.sleep(1)
                    await self.auto_reply(message)
                    
                    # Clear search
                    clear_btn = self.page.locator("button[aria-label='Cancel search'], button[aria-label='إلغاء البحث']").first
                    if await clear_btn.count() > 0:
                        await clear_btn.click()
                        
                    return True
                else:
                    logger.warning(f"Contact not found via search: {chat_name}")
                    return False
            else:
                logger.warning("Search box not found in WhatsApp Web UI")
                return False
        except Exception as e:
            logger.error(f"Failed to send safely to {chat_name}: {e}")
            return False

    async def send_invoice_async(self, phone: str, text: str, file_path: str = None) -> tuple[bool, str]:
        """Sends a message and optional file to a specific phone number."""
        if not self.is_logged_in or not self.page:
            return False, "WhatsApp Agent is not logged in."
            
        try:
            logger.info(f"Targeting WhatsApp send to {phone}. File: {file_path}")
            if file_path:
                logger.info(f"File exists: {os.path.exists(file_path)} (Path: {os.path.abspath(file_path)})")

            safe_text = quote(text)
            url = f"https://web.whatsapp.com/send/?phone={phone}&text={safe_text}&type=phone_number&app_absent=0"
            await self.page.goto(url)
            
            # Wait for chat to load (either chat input or invalid phone generic dialog)
            try:
                # Wait for the main pane or the "Phone number shared via url is invalid" dialog
                await self.page.wait_for_selector(
                    "div[contenteditable='true'], div[role='dialog'], #main", 
                    state="visible",
                    timeout=45000
                )
            except Exception as e:
                logger.error(f"Navigation to chat timed out or failed: {e}")
                return False, "Timeout waiting for chat to load. Try checking your connection."
                
            # If invalid phone dialog exists, return error - support English and Arabic buttons
            dialog_btn_selectors = "div[role='button']:has-text('OK'), div[role='button']:has-text('Close'), div[role='button']:has-text('تم'), div[role='button']:has-text('موافق'), div[role='button']:has-text('إغلاق')"
            if await self.page.locator(dialog_btn_selectors).count() > 0:
                logger.warning(f"WhatsApp reported invalid phone number: {phone}")
                # Click close/ok
                await self.page.locator(dialog_btn_selectors).first.click()
                return False, "Invalid phone number."
                
            # Allow some time for the 'connecting' overlay to disappear and the input to become active
            await asyncio.sleep(2) 
            
            if file_path and os.path.exists(file_path):
                # Click the attach icon - include Arabic label 'إرفاق'
                attach_selectors = "span[data-icon='plus'], span[data-icon='attach-menu-plus'], span[data-icon='clip'], [aria-label='Attach'], [aria-label='إرفاق'], [title='Attach'], [title='إرفاق']"
                attach_icon = self.page.locator(attach_selectors).first
                
                if await attach_icon.count() > 0:
                    await attach_icon.click()
                    await asyncio.sleep(2) # Wait for menu to fully expand
                    
                    # Target 'Photos & Videos' specifically to avoid sticker/document behavior
                    # Broadened to support many Arabic variations found in different WhatsApp versions
                    media_selectors = [
                        "span[data-icon='attach-menu-image']",
                        "span[data-icon='attach-image']",
                        "[aria-label*='Photos']",
                        "[aria-label*='الصور']",
                        "[aria-label*='الوسائط']",
                        "li:has-text('Photos')",
                        "li:has-text('الصور')",
                        "button:has-text('Photos')",
                        "button:has-text('الصور')"
                    ]
                    
                    # Target 'Document' as a secondary media fallback (often sends as image if it's a known format)
                    doc_selectors = [
                        "span[data-icon='attach-menu-document']",
                        "[aria-label*='Document']",
                        "[aria-label*='مستند']",
                        "li:has-text('Document')",
                        "li:has-text('مستند')"
                    ]
                    
                    try:
                        target_btn = None
                        # Try media selectors first
                        for selector in media_selectors:
                            btn = self.page.locator(selector).first
                            if await btn.count() > 0 and await btn.is_visible():
                                target_btn = btn
                                logger.info(f"Targeting media button via: {selector}")
                                break
                        
                        # If no media button, try document as fallback
                        if not target_btn:
                            for selector in doc_selectors:
                                btn = self.page.locator(selector).first
                                if await btn.count() > 0 and await btn.is_visible():
                                    target_btn = btn
                                    logger.info(f"Targeting document button via: {selector}")
                                    break
                        
                        if target_btn:
                            # Use expect_file_chooser for maximum reliability
                            async with self.page.expect_file_chooser() as fc_info:
                                await target_btn.click(force=True)
                            file_chooser = await fc_info.value
                            await file_chooser.set_files(file_path)
                            logger.info("File selected via menu button.")
                        else:
                            # Direct input fallback - seek specific media inputs
                            file_input = self.page.locator("input[type='file'][accept*='image/*']").first
                            if await file_input.count() == 0:
                                file_input = self.page.locator("input[type='file']").first
                            
                            await file_input.set_input_files(file_path)
                            logger.info("Used direct file input fallback (no menu buttons found).")
                    except Exception as e:
                        logger.error(f"Failed to set input files: {e}")
                        return False, f"File upload failed: {e}"
                    
                    # Wait for preview modal to appear and click send
                    try:
                        # Broaden selectors to include new WhatsApp Design System (WDS) icons
                        send_selectors = "span[data-icon='send'], [aria-label='Send'], [data-icon='wds-ic-send-filled']"
                        await self.page.wait_for_selector(send_selectors, state="visible", timeout=20000)
                        
                        # Try to fill caption if text is provided
                        if text:
                            try:
                                caption_selectors = [
                                    "div[contenteditable='true'][aria-placeholder='Add a caption']",
                                    "div[contenteditable='true'][aria-placeholder='إضافة شرح']",
                                    "div[contenteditable='true'][title='Add a caption']",
                                    "div[contenteditable='true'][title='إضافة شرح']",
                                    "div[contenteditable='true']"
                                ]
                                for c_selector in caption_selectors:
                                    caption_box = self.page.locator(c_selector).last
                                    if await caption_box.count() > 0 and await caption_box.is_visible():
                                        # Use fill for speed, or type if message box is finicky
                                        await caption_box.fill(text)
                                        logger.info("Caption filled in preview modal.")
                                        break
                            except Exception as caption_err:
                                logger.warning(f"Failed to fill caption (will try to send anyway): {caption_err}")
                        
                        await asyncio.sleep(1) # Final stabilization for UI animations
                        
                        # Use force=True to bypass pointer-event interception by internal icons/spans
                        await self.page.locator(send_selectors).last.click(force=True)
                        logger.info("Clicked attachment send button with force=True.")
                    except Exception as e:
                        logger.warning(f"Failed to find or click send button in preview modal: {e}")
                        # Fallback: try pressing Enter if the modal is focused
                        await self.page.keyboard.press("Enter")
                        logger.info("Attempted Enter key fallback after click failure.")
                else:
                    return False, "Could not find the attach button. The WhatsApp UI might have changed."
            else:
                # Just send the pre-filled text
                send_selectors = "span[data-icon='send'], [aria-label='Send'], [data-icon='wds-ic-send-filled']"
                send_icon = self.page.locator(send_selectors).first
                if await send_icon.count() > 0:
                    await send_icon.click(force=True)
                else:
                    await self.page.keyboard.press("Enter")
                logger.info("Sent text-only message.")
            
            # Wait for message to actually leave the outbox
            await asyncio.sleep(4)
            return True, "Message sent successfully!"
            
        except Exception as e:
            logger.error(f"Failed to send WhatsApp message: {e}")
            return False, str(e)
