from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout, QFrame
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPixmap, QColor
import os
import logging

logger = logging.getLogger(__name__)

class WhatsAppSettingsWidget(QWidget):
    """
    Custom settings widget for WhatsApp Agent plugin.
    Displays status, QR code for login, and control buttons.
    """
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setup_ui()
        
        # Timer to refresh status and QR code
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_ui)
        self.refresh_timer.start(2000) # Refresh every 2 seconds
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Status Label
        self.status_container = QFrame()
        self.status_container.setStyleSheet("background-color: #f3f4f6; border-radius: 8px;")
        status_layout = QHBoxLayout(self.status_container)
        
        self.status_label = QLabel("Status: Unknown")
        self.status_label.setStyleSheet("font-weight: bold; color: #374151; padding: 5px;")
        status_layout.addWidget(self.status_label)
        
        self.status_indicator = QLabel()
        self.status_indicator.setFixedSize(12, 12)
        self.status_indicator.setStyleSheet("background-color: #9ca3af; border-radius: 6px;")
        status_layout.addWidget(self.status_indicator)
        status_layout.addStretch()
        
        layout.addWidget(self.status_container)
        
        # QR Code / Info Area
        self.qr_area = QFrame()
        self.qr_area.setMinimumHeight(280)
        self.qr_area.setStyleSheet("background-color: white; border: 1px solid #e5e7eb; border-radius: 8px;")
        qr_layout = QVBoxLayout(self.qr_area)
        qr_layout.setAlignment(Qt.AlignCenter)
        
        self.qr_label = QLabel("Scan the QR code to connect")
        self.qr_label.setWordWrap(True)
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setStyleSheet("color: #6b7280; font-size: 14px;")
        qr_layout.addWidget(self.qr_label)
        
        self.qr_image = QLabel()
        self.qr_image.setFixedSize(250, 250)
        self.qr_image.setAlignment(Qt.AlignCenter)
        self.qr_image.setStyleSheet("border: 1px dashed #d1d5db;")
        qr_layout.addWidget(self.qr_image)
        
        layout.addWidget(self.qr_area)
        
        from PyQt5.QtWidgets import QCheckBox, QLineEdit, QTextEdit
        
        # Configuration Fields
        self.config_area = QFrame()
        self.config_area.setStyleSheet("background-color: transparent;")
        config_layout = QVBoxLayout(self.config_area)
        config_layout.setContentsMargins(0, 10, 0, 10)
        
        self.auto_start_chk = QCheckBox("Auto-start WhatsApp Background Agent")
        self.auto_start_chk.setChecked(self.plugin.get_setting('auto_start', False, type=bool))
        self.auto_start_chk.stateChanged.connect(lambda s: self.plugin.set_setting('auto_start', bool(s)))
        config_layout.addWidget(self.auto_start_chk)
        
        self.bot_mode_chk = QCheckBox("Enable AI Bot Auto-reply")
        self.bot_mode_chk.setChecked(self.plugin.get_setting('bot_mode', False, type=bool))
        self.bot_mode_chk.stateChanged.connect(lambda s: self.plugin.set_setting('bot_mode', bool(s)))
        config_layout.addWidget(self.bot_mode_chk)
        
        sender_lbl = QLabel("Allowed Sender (Name or Number):")
        self.allowed_sender_edit = QLineEdit()
        self.allowed_sender_edit.setPlaceholderText("Leave blank to allow all...")
        self.allowed_sender_edit.setText(self.plugin.get_setting('allowed_sender', ""))
        self.allowed_sender_edit.textChanged.connect(lambda t: self.plugin.set_setting('allowed_sender', t))
        config_layout.addWidget(sender_lbl)
        config_layout.addWidget(self.allowed_sender_edit)
        
        template_lbl = QLabel("Outgoing Message Template (for 'Send WhatsApp' action):")
        self.template_edit = QTextEdit()
        self.template_edit.setMaximumHeight(100)
        default_template = """\U0001F4C4 *Invoice #{invoice_number}*
\U0001F4C5 *Date:* {date}

\U0001F464 *From:* {vendor_name}
\U0001F4B3 *VAT ID:* {vat_id}

\U0001F4CB *Items:*
{line_items}

\U0001F4B0 *Subtotal:* {currency} {subtotal}
\U0001F4CA *VAT ({vat_rate}%):* {currency} {vat_total}
\U0001F4B5 *Total:* {currency} {total}

Thanks!"""
        self.template_edit.setPlainText(self.plugin.get_setting('message_template', default_template))
        self.template_edit.textChanged.connect(lambda: self.plugin.set_setting('message_template', self.template_edit.toPlainText()))
        config_layout.addWidget(template_lbl)
        config_layout.addWidget(self.template_edit)
        
        layout.addWidget(self.config_area)
        
        # Controls
        controls_layout = QHBoxLayout()
        
        self.start_btn = QPushButton("Start Agent")
        self.start_btn.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 8px;")
        self.start_btn.clicked.connect(self.on_start_clicked)
        controls_layout.addWidget(self.start_btn)
        
        self.stop_btn = QPushButton("Stop Agent")
        self.stop_btn.setStyleSheet("background-color: #dc2626; color: white; font-weight: bold; padding: 8px;")
        self.stop_btn.clicked.connect(self.on_stop_clicked)
        controls_layout.addWidget(self.stop_btn)
        
        self.logout_btn = QPushButton("Logout / Reset")
        self.logout_btn.setStyleSheet("background-color: #6b7280; color: white; font-weight: bold; padding: 8px;")
        self.logout_btn.clicked.connect(self.on_logout_clicked)
        controls_layout.addWidget(self.logout_btn)
        
        layout.addLayout(controls_layout)
        
        # Help text
        help_text = QLabel("To connect: Click 'Start Agent' and scan the QR code with your WhatsApp app (Linked Devices).")
        help_text.setWordWrap(True)
        help_text.setStyleSheet("color: #9ca3af; font-size: 11px; font-style: italic;")
        layout.addWidget(help_text)
        
        self.refresh_ui()

    def refresh_ui(self):
        """Update status and images from plugin/client state."""
        client = self.plugin.wa_client
        
        # Update Status
        status_text = "Stopped"
        color = "#9ca3af" # Gray
        
        if client.is_running:
            if client.is_logged_in:
                status_text = "Connected"
                color = "#059669" # Green
            elif "qr" in self.plugin._status_message.lower():
                status_text = "Waiting for Scan"
                color = "#ea580c" # Orange
            else:
                status_text = "Starting..."
                color = "#3b82f6" # Blue
        
        self.status_label.setText(f"Status: {status_text}")
        self.status_indicator.setStyleSheet(f"background-color: {color}; border-radius: 6px;")
        
        # Update QR Code
        qr_path = os.path.join(os.path.dirname(__file__), "qr.png")
        if client.is_running and not client.is_logged_in and os.path.exists(qr_path):
            pixmap = QPixmap(qr_path)
            if not pixmap.isNull():
                self.qr_image.setPixmap(pixmap.scaled(250, 250, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self.qr_label.setText("Scan now with WhatsApp:")
            else:
                self.qr_image.clear()
                self.qr_label.setText("Preparing QR code...")
        elif client.is_logged_in:
            self.qr_image.setText("✅")
            self.qr_image.setStyleSheet("font-size: 80px; color: #059669; border: none;")
            self.qr_label.setText("Successfully connected to WhatsApp!")
        else:
            self.qr_image.clear()
            self.qr_image.setStyleSheet("border: 1px dashed #d1d5db;")
            if not client.is_running:
                self.qr_label.setText("Agent is not running.")
            else:
                self.qr_label.setText("Initializing browser...")

        # Update Buttons
        self.start_btn.setEnabled(not client.is_running)
        self.stop_btn.setEnabled(client.is_running)
        self.logout_btn.setEnabled(client.is_running or os.path.exists(client.session_dir))

    def on_start_clicked(self):
        self.plugin.start_agent()
        self.refresh_ui()

    def on_stop_clicked(self):
        self.plugin.stop_agent()
        self.refresh_ui()

    def on_logout_clicked(self):
        from PyQt5.QtWidgets import QMessageBox
        reply = QMessageBox.question(self, 'Reset Session', 
                                    "This will stop the agent and delete the local session. You will need to re-scan the QR code next time. Proceed?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        
        if reply == QMessageBox.Yes:
            self.plugin.stop_agent()
            client = self.plugin.wa_client
            if os.path.exists(client.session_dir):
                import shutil
                try:
                    shutil.rmtree(client.session_dir)
                    logger.info("Session directory cleared.")
                except Exception as e:
                    logger.error(f"Failed to clear session dir: {e}")
            self.refresh_ui()
