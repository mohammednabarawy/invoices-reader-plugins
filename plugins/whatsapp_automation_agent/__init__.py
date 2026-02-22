from core.plugins import DeclarativePlugin, Action, Field, hook
from core.plugins.sdk import get_logger
import threading
import os
from .whatsapp_client import WhatsAppClient

logger = get_logger(__name__)

class WhatsAppAgentPlugin(DeclarativePlugin):
    """
    WhatsApp Agent integration via Playwright.
    Allows sending and receiving WhatsApp messages through the host application.
    """
    
    # Plugin metadata (used by framework before manifest is loaded)
    id = "whatsapp_agent"
    name = "WhatsApp Automation Agent"
    version = "1.0.0"
    description = "Powerful automation that integrates Invoices Reader with WhatsApp via an embedded browser."
    
    def __init__(self):
        super().__init__()
        from PyQt5.QtCore import QSettings
        self.settings = QSettings("InvoicesReader", "Plugin_WhatsAppAgent")
        self.wa_client = WhatsAppClient(self)
        self.agent_thread = None
        self._status_message = "Waiting for agent to start..."

    def get_setting(self, key: str, default_val=None, type=None):
        """Helper to get a setting using QSettings"""
        if type:
            return self.settings.value(key, default_val, type=type)
        return self.settings.value(key, default_val)

    def set_setting(self, key: str, value):
        """Helper to set a setting using QSettings"""
        self.settings.setValue(key, value)

    def on_load(self):
        """Called after framework initializes the plugin (API is available)."""
        # Register settings UI in the Integrations page
        try:
            from .settings_ui import WhatsAppSettingsWidget
            self.api.register_settings_tab(
                plugin_id=self.id,
                label="WhatsApp Agent",
                widget_factory=lambda: WhatsAppSettingsWidget(self)
            )
        except Exception as e:
            logger.error(f"Failed to register WhatsApp settings tab: {e}")
        
        auto_start_val = self.get_setting('auto_start', False, type=bool)
        if auto_start_val:
            self.start_agent()

    def on_source_processing_event(self, source, status, metadata, payload):
        """Generic plugin callback for source-processing events."""
        if str(source).lower() != 'whatsapp':
            return

        if not self.wa_client or not self.wa_client.is_running:
            return

        normalized_status = str(status or "").lower()
        safe_metadata = metadata or {}

        if normalized_status == 'duplicate':
            self.wa_client.notify_duplicate(payload or {}, safe_metadata)
            return

        if normalized_status == 'completed':
            self.wa_client.notify_processing_result(payload or {}, safe_metadata)
            return

        if normalized_status == 'failed':
            if isinstance(payload, dict):
                error_text = payload.get('error', 'Unknown processing error')
            else:
                error_text = str(payload) if payload else 'Unknown processing error'
            self.wa_client.notify_processing_failed(error_text, safe_metadata)
            return

    @Action(label="Start WhatsApp Agent", location="settings", icon="fa5b.whatsapp")
    def start_agent(self, *args):
        """Start the Playwright agent in the background."""
        if self.wa_client.is_running:
            self.api.ui.toast("WhatsApp Agent is already running.", "warning")
            return
            
        self.api.ui.toast("Starting WhatsApp Agent...", "info")
        self._status_message = "Starting browser..."
        
        self.agent_thread = threading.Thread(target=self.wa_client.run, daemon=True)
        self.agent_thread.start()

    @Action(label="Stop Agent", location="settings", icon="fa5s.stop-circle")
    def stop_agent(self, *args):
        """Stop the agent and close the browser."""
        if not self.wa_client.is_running:
            return
            
        self.api.ui.toast("Stopping WhatsApp Agent...", "info")
        self.wa_client.stop()
        if self.agent_thread:
            self.agent_thread.join(timeout=5)
            
        self._status_message = "Agent stopped."
        self.api.ui.toast("WhatsApp Agent stopped.", "success")

    @Action(label="Send WhatsApp", location="toolbar:right", icon="fa5b.whatsapp")
    def send_via_whatsapp(self, invoice: dict = None):
        """Action hook to send the current invoice via WhatsApp."""
        if not self.wa_client.is_logged_in:
            self.api.ui.toast("WhatsApp Agent is not logged in!", "error")
            return
            
        if not invoice:
            self.api.ui.toast("No invoice selected.", "warning")
            return
            
        # Ensure we have a valid file to send
        file_path = invoice.get('file_path')
        if not file_path and invoice.get('image_file'):
            file_path = os.path.join(self.api.get_base_path(), invoice.get('image_file'))
            
        if not file_path or not os.path.exists(file_path):
            self.api.ui.toast("No valid file attached to this invoice.", "error")
            return
            
        # Ask user for phone number
        phone = self.api.ui.show_input(
            "Send WhatsApp",
            "Enter phone number (include country code, e.g., 9665...):",
            ""
        )
        if not phone:
            return  # user cancelled
            
        # Format message using the template logic from whatsapp-redirect
        text = self._format_message(invoice)
        
        self.api.ui.toast("Queuing WhatsApp message...", "info")
        
        # Define an internal callback to handle the result
        def _send_callback(future):
            try:
                success, msg = future.result()
                if success:
                    # Thread-safe ui call
                    self.update_status(f"Sent invoice to {phone}")
                else:
                    self.update_status(f"Failed to send: {msg}")
            except Exception as e:
                self.update_status(f"Error checking send result: {e}")
                
        # Schedule the async coroutine in the agent's event loop
        import asyncio
        if hasattr(self.wa_client, 'loop') and self.wa_client.loop and self.wa_client.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self.wa_client.send_invoice_async(phone, text, file_path),
                self.wa_client.loop
            )
            future.add_done_callback(_send_callback)
        else:
            self.api.ui.toast("Agent loop is not running.", "error")

    def _format_message(self, data: dict) -> str:
        """Replace variables in template with data (cloned from whatsapp-redirect)"""
        template = self.get_setting('message_template', """\U0001F4C4 *Invoice #{invoice_number}*
\U0001F4C5 *Date:* {date}

\U0001F464 *From:* {vendor_name}
\U0001F4B3 *VAT ID:* {vat_id}

\U0001F4CB *Items:*
{line_items}

\U0001F4B0 *Subtotal:* {currency} {subtotal}
\U0001F4CA *VAT ({vat_rate}%):* {currency} {vat_total}
\U0001F4B5 *Total:* {currency} {total}

Thanks!""", type=str)
        
        # Get date with multiple fallbacks
        date = data.get('date') or data.get('invoice_date') or data.get('created_date') or ''
        if date:
            if 'T' in str(date):
                date = str(date).split('T')[0]
        else:
            date = 'N/A'
        
        # Get totals
        invoice_total = data.get('invoice_total') or data.get('total_amount') or data.get('total') or 0.0
        vat_total = data.get('vat_total') or data.get('tax_amount') or 0.0
        
        # Calculate subtotal (total - vat)
        try:
            subtotal = float(invoice_total) - float(vat_total)
        except (ValueError, TypeError):
            subtotal = invoice_total
        
        # Calculate VAT rate
        try:
            if subtotal and float(subtotal) > 0:
                vat_rate = round((float(vat_total) / float(subtotal)) * 100)
            else:
                vat_rate = 15
        except (ValueError, TypeError, ZeroDivisionError):
            vat_rate = 15
        
        # Format line items
        line_items_text = self._format_line_items(data.get('line_items', []))
        
        # Safe formatting using simple replace
        if not template:
            template = ""
            
        text = template
        replacements = {
            "{vendor_name}": data.get('vendor_name', 'Vendor'),
            "{vat_id}": data.get('vat_id') or data.get('vendor_vat_id') or 'N/A',
            "{invoice_number}": data.get('invoice_number', 'Unknown'),
            "{total}": f"{float(invoice_total):,.2f}" if invoice_total else "0.00",
            "{subtotal}": f"{float(subtotal):,.2f}" if subtotal else "0.00",
            "{vat_total}": f"{float(vat_total):,.2f}" if vat_total else "0.00",
            "{vat_rate}": str(vat_rate),
            "{currency}": data.get('currency', 'SAR'),
            "{date}": date,
            "{line_items}": line_items_text,
        }
        
        for key, value in replacements.items():
            text = text.replace(key, str(value) if value else "")
            
        return text

    def _format_line_items(self, line_items: list) -> str:
        """Format line items as a readable list"""
        if not line_items:
            return "No items"
        
        lines = []
        for i, item in enumerate(line_items, 1):
            desc = item.get('description', 'Item')
            qty = item.get('quantity', 1)
            price = item.get('unit_price', 0)
            total = item.get('line_total') or (float(qty) * float(price))
            
            lines.append(f"{i}. {desc}")
            lines.append(f"   {qty} x {float(price):,.2f} = {float(total):,.2f}")
        
        return "\n".join(lines)

    def on_unload(self):
        """Clean up resources before plugin is unloaded."""
        if self.wa_client.is_running:
            self.wa_client.stop()

    def update_status(self, message: str):
        """Helper to update the UI status from the background thread."""
        self._status_message = message
        logger.info(f"WhatsApp Status: {message}")
