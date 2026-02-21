# Import from SDK for cleaner plugin code
from core.plugins.sdk import *
import urllib.parse
import os

class WhatsAppRedirectPlugin(DeclarativePlugin):
    """
    Redirects to WhatsApp Web/Desktop with invoice details 
    and copies invoice image to clipboard for easy pasting.
    """
    
    id = "whatsapp_redirect"
    name = "WhatsApp Redirect"
    version = "1.0.0"
    author = "Invoices Reader"
    description = "Send invoice summaries to WhatsApp and copy invoice image to clipboard for quick sharing."

    # Using Unicode escapes to ensure safe rendering on all platforms/encodings
    DEFAULT_TEMPLATE = """\U0001F4C4 *Invoice #{invoice_number}*
\U0001F4C5 *Date:* {date}

\U0001F464 *From:* {vendor_name}
\U0001F4B3 *VAT ID:* {vat_id}

\U0001F4CB *Items:*
{line_items}

\U0001F4B0 *Subtotal:* {currency} {subtotal}
\U0001F4CA *VAT ({vat_rate}%):* {currency} {vat_total}
\U0001F4B5 *Total:* {currency} {total}

Thanks!"""

    def __init__(self):
        super().__init__()
        self.settings = QSettings("InvoicesReader", "Plugin_WhatsAppRedirect")
        self.message_template = self.DEFAULT_TEMPLATE
        self.auto_copy_image = True

    def on_load(self) -> bool:
        """Called when plugin is enabled"""
        # Load persistent settings
        self._load_settings()
        
        # Register settings tab
        self.api.ui.add_settings_page("whatsapp-settings", "WhatsApp", self.create_settings_widget())
        return True

    def _load_settings(self):
        """Load settings from QSettings"""
        # Using v2 key to force reset of template for users with corrupted emoji cache
        self.message_template = self.settings.value("message_template_v2", self.DEFAULT_TEMPLATE, type=str)
        self.auto_copy_image = self.settings.value("auto_copy_image", True, type=bool)

    def _save_setting(self, key, value):
        """Save a single setting and update memory"""
        # Map legacy key calls to v2 if needed, though we update callers below
        if key == "message_template":
            key = "message_template_v2"
            self.message_template = value
        elif key == "auto_copy_image":
            self.auto_copy_image = value
        
        self.settings.setValue(key, value)

    def create_settings_widget(self):
        """Create the settings UI widget"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # Template
        lbl = QLabel("Message Template (use {variable} for invoice fields):")
        help_lbl = QLabel("Variables: {vendor_name}, {vat_id}, {invoice_number}, {date}, {currency}, {subtotal}, {vat_total}, {vat_rate}, {total}, {line_items}")
        help_lbl.setStyleSheet("color: gray; font-size: 10px;")
        self.template_edit = QTextEdit()
        self.template_edit.setPlainText(self.message_template)
        # Connect to save logic
        self.template_edit.textChanged.connect(
            lambda: self._save_setting('message_template', self.template_edit.toPlainText())
        )
        self.template_edit.setPlaceholderText("📄 *Invoice #{invoice_number}*...")
        
        # Auto Copy Checkbox
        self.copy_chk = QCheckBox("Automatically copy invoice image to clipboard (Ctrl+V to paste in WhatsApp)")
        self.copy_chk.setChecked(self.auto_copy_image)
        self.copy_chk.stateChanged.connect(
            lambda s: self._save_setting('auto_copy_image', bool(s))
        )

        layout.addWidget(lbl)
        layout.addWidget(help_lbl)
        layout.addWidget(self.template_edit)
        layout.addWidget(self.copy_chk)
        layout.addStretch()
        
        return widget

    @Action(label="WhatsApp", location="toolbar", icon="fa5b.whatsapp", tooltip="Share to WhatsApp")
    def share_to_whatsapp(self, invoice=None):
        """Main action: Copy image, format text, open WhatsApp"""
        
        # 1. Get Data (now includes line_items from API)
        invoice_data = invoice
        if not invoice_data:
            invoice_data = self.api.get_current_invoice()

        if not invoice_data:
            self.api.ui.toast("No invoice selected", "warning")
            return

        # 2. Copy Image (if enabled)
        if self.auto_copy_image:
            image_path = invoice_data.get('file_path') or invoice_data.get('image_path')
            if image_path and os.path.exists(image_path):
                self._copy_image_to_clipboard(image_path)
            else:
                self.api.ui.toast("Invoice image not found", "warning")

        # 3. Format Message (line_items are now included from API)
        message = self._format_message(invoice_data)
        
        # 4. Open URL
        encoded_msg = urllib.parse.quote(message)
        # Using api.whatsapp.com/send avoids issues with wa.me redirections and emoji encoding on Desktop
        url = f"https://api.whatsapp.com/send?text={encoded_msg}"
        self.api.system.open_url(url)
        
        # 5. User Feedback
        self.api.ui.toast("Review WhatsApp & Paste Image (Ctrl+V)", "success")

    def _copy_image_to_clipboard(self, image_path: str):
        """Load image and set to system clipboard"""
        try:
            image = QImage(image_path)
            if not image.isNull():
                clipboard = QApplication.clipboard()
                clipboard.setImage(image)
                self.api.ui.toast("Image copied to clipboard!", "info")
            else:
                print(f"Failed to load image: {image_path}")
        except Exception as e:
            print(f"Error copying to clipboard: {e}")

    def _format_message(self, data: dict) -> str:
        """Replace variables in template with data"""
        template = self.message_template
        
        # Get date with multiple fallbacks
        date = data.get('date') or data.get('invoice_date') or data.get('created_date') or ''
        if date:
            # Clean up ISO dates (e.g., "2022-06-27T10:22:32Z" -> "2022-06-27")
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
                vat_rate = 15  # Default Saudi VAT
        except (ValueError, TypeError, ZeroDivisionError):
            vat_rate = 15
        
        # Format line items
        line_items_text = self._format_line_items(data.get('line_items', []))
        
        # Safe formatting using simple replace to avoid KeyErrors
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
            
            # Numbered list format
            # 1. Item Name
            #    Qty x Price = Total
            lines.append(f"{i}. {desc}")
            lines.append(f"   {qty} x {float(price):,.2f} = {float(total):,.2f}")
        
        return "\n".join(lines)
