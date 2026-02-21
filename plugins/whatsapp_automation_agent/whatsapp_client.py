import os
import sys
import time
import asyncio
import traceback
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
        
        # We store the session data in the plugin folder
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.user_data_dir = os.path.join(plugin_dir, "whatsapp_session")
        self.session_dir = self.user_data_dir  # alias for settings_ui

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
                import subprocess
                subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
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
                # Look for unread message badges in the chat list
                unread_selectors = [
                    "div[aria-label*='unread message']",
                    "div[aria-label*='رسالة غير مقروءة']",
                    "span[aria-label*='unread message']",
                    "span[aria-label*='رسالة غير مقروءة']"
                ]
                unread_chats = []
                for selector in unread_selectors:
                    elements = await self.page.locator(selector).all()
                    unread_chats.extend(elements)
                
                # Use a set to avoid processing the same chat multiple times if selectors overlap
                # (Playwright locators are unhashable though, so just process them and they'll be read)
                processed_in_this_loop = False
                
                for chat in unread_chats:
                    try:
                        # Ensure we can click it
                        if not await chat.is_visible():
                            continue
                            
                        # Click the chat to open it
                        await chat.click(timeout=5000)
                        await asyncio.sleep(1.5) # wait for messages to load and read state to update
                        processed_in_this_loop = True
                        
                        # Apply Sender Filtering
                        allowed_sender = self.plugin.get_setting('allowed_sender', "")
                        if allowed_sender and allowed_sender.strip():
                            header_title = ""
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
                                        msg_in = self.page.locator("div.message-in").last
                                        if await msg_in.count() > 0:
                                            data_id = await msg_in.get_attribute("data-id")
                                            if data_id:
                                                import re
                                                # Pattern usually looks like "false_966592328502@c.us_..."
                                                number_match = re.search(r'false_(\d+)(?:@c\.us|@s\.whatsapp\.net)', data_id)
                                                if number_match:
                                                    extracted_number = number_match.group(1)
                                                    logger.info(f"[WA] Strategy 4 natively extracted raw phone number: '{extracted_number}'")
                                                    # We append this raw number to the header title to guarantee a match against the user's settings!
                                                    header_title += " " + extracted_number
                                    except Exception as e:
                                        logger.warning(f"[WA] Could not extract raw number from DOM: {e}")
                            except Exception as e:
                                logger.warning(f"Could not read chat header: {e}")
                            
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
                                
                        # Get the last message in the list
                        messages = await self.page.locator("div.message-in").all()
                        if messages:
                            last_message = messages[-1]
                            has_downloaded = False
                            
                            # 1. Check for documents/files with a direct downward arrow download button inside the message
                            dl_icon_selectors = "span[data-icon='down'], span[data-icon='arrow-down']"
                            download_btn = last_message.locator(dl_icon_selectors).first
                            
                            if await download_btn.count() == 0:
                                download_btn = last_message.locator("div[role='button'][aria-label='Download'], div[role='button'][aria-label='تنزيل']").first
                                
                            if await download_btn.count() > 0 and await download_btn.is_visible():
                                logger.info("Found downloadable media attachment.")
                                try:
                                    async with self.page.expect_download(timeout=15000) as download_info:
                                        await download_btn.click()
                                    download = await download_info.value
                                    
                                    file_path = os.path.join(downloads_dir, download.suggested_filename)
                                    await download.save_as(file_path)
                                    logger.info(f"Downloaded media document: {file_path}")
                                    has_downloaded = True
                                    
                                    # Forward to main app via API
                                    if hasattr(self.plugin.api, 'processing'):
                                        self.plugin.api.processing.import_file_to_queue(file_path, "WhatsApp")
                                        
                                except Exception as e:
                                    logger.error(f"Error downloading media document: {e}")

                            # 2. Check for displayed images (WhatsApp strips direct download buttons from displayed images)
                            if not has_downloaded:
                                img_element = last_message.locator("img[src^='blob:']").first
                                if await img_element.count() > 0 and await img_element.is_visible():
                                    logger.info("Found image message.")
                                    try:
                                        # Click image to open the media viewer
                                        await img_element.click()
                                        await asyncio.sleep(1)
                                        
                                        # Click the download button in the top right of the viewer
                                        viewer_download_btn = self.page.locator("div[role='button'][aria-label='Download'], div[role='button'][aria-label='تنزيل'], span[data-icon='download']").first
                                        if await viewer_download_btn.count() > 0 and await viewer_download_btn.is_visible():
                                            async with self.page.expect_download(timeout=15000) as download_info:
                                                await viewer_download_btn.click()
                                            download = await download_info.value
                                            
                                            file_path = os.path.join(downloads_dir, download.suggested_filename)
                                            await download.save_as(file_path)
                                            logger.info(f"Downloaded image: {file_path}")
                                            has_downloaded = True
                                            
                                            # Queue for processing
                                            if hasattr(self.plugin.api, 'processing'):
                                                self.plugin.api.processing.import_file_to_queue(file_path, "WhatsApp")
                                        else:
                                            logger.warning("Could not find download button in image viewer.")
                                            
                                        # Close viewer
                                        await self.page.keyboard.press("Escape")
                                        await asyncio.sleep(0.5)
                                    except Exception as e:
                                        logger.error(f"Error downloading image: {e}")
                                        # Ensure viewer is closed
                                        await self.page.keyboard.press("Escape")

                            # 3. Read any text attached
                            text_element = last_message.locator("span.selectable-text")
                            msg_text = ""
                            if await text_element.count() > 0:
                                msg_text = await text_element.first.inner_text()
                                logger.info(f"Received WhatsApp Message: {msg_text}")
                                
                            # 4. Auto-reply logic
                            if msg_text or has_downloaded:
                                bot_val = self.plugin.get_setting('bot_mode', False, type=bool)
                                if bot_val:
                                    reply_msg = "I received your message! I am the Invoices Reader AI agent."
                                    if has_downloaded:
                                        reply_msg = "I received your file and queued it for processing! I am the Invoices Reader AI agent."
                                    await self.auto_reply(reply_msg)
                                    
                    except Exception as e:
                        logger.warning(f"Error checking individual message: {e}")
                
                # If we processed chats, maybe wait a bit less
                if processed_in_this_loop:
                    await asyncio.sleep(2)
                else:
                    await asyncio.sleep(5)
            except Exception as e:
                # Silently catch broad scraping errors to keep the loop resilient
                await asyncio.sleep(5)
            
    async def auto_reply(self, text: str):
        """Types and sends a message in the currently open chat."""
        try:
            # Find the message input box - support English and Arabic titles
            input_box = self.page.locator("div[title='Type a message'], div[title='اكتب رسالة']")
            if await input_box.count() > 0:
                await input_box.fill(text)
                await self.page.keyboard.press("Enter")
                logger.info("Sent auto-reply.")
        except Exception as e:
            logger.error(f"Failed to auto-reply: {e}")

    async def send_invoice_async(self, phone: str, text: str, file_path: str = None) -> tuple[bool, str]:
        """Sends a message and optional file to a specific phone number."""
        if not self.is_logged_in or not self.page:
            return False, "WhatsApp Agent is not logged in."
            
        try:
            logger.info(f"Targeting WhatsApp send to {phone}. File: {file_path}")
            if file_path:
                logger.info(f"File exists: {os.path.exists(file_path)} (Path: {os.path.abspath(file_path)})")

            from urllib.parse import quote
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
