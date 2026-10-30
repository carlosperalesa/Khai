"""Brain module: reasoning facade, inference cascade and model/VM coordination."""
from app.mind.brain.controller import (
    ModelController,
    DolphinController,
    PROFILES,
    PROFILE_LABELS,
    _format_error_reason,
    INACTIVITY_TIMEOUT_SEC,
)


class Brain:
    """Reasoning and inference coordinator for Khai.

    Coordinates model selection, inference cascade (high > mid > low > nano),
    and model lifecycle. Delegates actual network transport and secrets to Voice.
    """

    def __init__(self, gateway, controller=None):
        self._gateway = gateway
        self.controller = controller or getattr(gateway, 'dolphin', None)

    def generate_content(self, prompt, **kwargs):
        return self._gateway.generate_content(prompt, **kwargs)

    def complete(self, messages, tools=None, **kwargs):
        return self._gateway.complete(messages, tools or [], **kwargs)

    def web_search(self, query, **kwargs):
        return self._gateway.web_search(query, **kwargs)

    def test_all_connections(self):
        return self._gateway.test_all_connections()

    # Model and VM lifecycle coordination
    def set_mode(self, mode: str, notifier=None) -> str:
        if self.controller is not None:
            return self.controller.set_mode(mode, notifier=notifier)
        raise RuntimeError("No model controller configured in Brain.")

    def get_active_mode(self) -> str:
        if self.controller is not None:
            return self.controller.get_active_mode()
        return "nano"

    def status(self) -> str:
        if self.controller is not None:
            return self.controller.status()
        return "[Modo nano conectado] (Cloud API activa)"

    def billing_summary(self) -> str:
        if self.controller is not None:
            return self.controller.billing_summary()
        return "Sin consumo de cómputo local."


__all__ = [
    'Brain',
    'ModelController',
    'DolphinController',
    'PROFILES',
    'PROFILE_LABELS',
    '_format_error_reason',
    'INACTIVITY_TIMEOUT_SEC',
]
