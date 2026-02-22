# WhatsApp Automation Agent Plugin

Automate inbound and outbound invoice workflows through WhatsApp Web using an embedded Playwright browser session.

## Features
- **Embedded Browser Session**: Works inside the app context (no manual browser tab switching required).
- **Incoming File Intake**: Detects incoming images and downloadable documents (including PDFs), downloads them, and sends them to the app queue.
- **Telegram-Style Receive Replies**: Sends status acknowledgements for incoming files:
  - `⏳ Downloading invoice...`
  - `📥 Received. Added to processing queue.`
  - `❌ Download failed.` (when needed)
- **Sender Filtering**: Optional allow-list by chat name or number.
- **Outgoing Send Action**: Adds **Send WhatsApp** action in toolbar for sharing current invoice.
- **Optional Text Bot Reply**: `bot_mode` can send an automatic text reply for plain text messages.

## Setup
1. Enable the plugin from **Settings → Integrations**.
2. Open **WhatsApp Agent** settings tab.
3. Click **Start Agent** and scan the QR code.
4. After connection, use:
   - **Send WhatsApp** for outbound sharing from current invoice.
   - Incoming chat messages/files for automated queue ingestion.

## Notes
- Python runtime dependencies are vendored under `libs/` for frozen app builds.
- In packaged (`Nuitka`) app mode, runtime `pip install` is disabled for safety.
- First start may download Playwright browser binaries.
- Keep the session active to avoid repeated QR scans.
- Use document upload in WhatsApp for most reliable PDF intake.
