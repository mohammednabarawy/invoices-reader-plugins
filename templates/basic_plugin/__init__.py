from core.plugins import DeclarativePlugin, Action, Field, get_logger, hook

logger = get_logger(__name__)

class BasicPlugin(DeclarativePlugin):
    """
    A basic plugin template.
    """
    
    # Define a persistent setting (auto-saved)
    enable_feature = Field(
        type="checkbox", 
        label="Enable Feature", 
        persist=True,
        default=True
    )

    @Action(label="Say Hello", location="toolbar", icon="fa5s.hand-spock")
    def say_hello(self, invoice: dict):
        """Called when the toolbar button is clicked"""
        self.api.ui.toast("Hello from Basic Plugin!", "success")
        logger.info("Basic Plugin says hello!")
