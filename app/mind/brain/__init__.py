"""Brain module: reasoning facade, inference cascade and model/VM coordination."""
import uuid
import time
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
        return self._infer('generate_content', lambda: self._gateway.generate_content(prompt, **kwargs))

    def complete(self, messages, tools=None, **kwargs):
        return self._infer('complete', lambda: self._gateway.complete(messages, tools or [], **kwargs))

    def _infer(self, operation, call):
        from app.kernel.journal import Journal
        from app.pixy.budget import current_goal_revision
        journal = getattr(self._gateway, 'journal', None)
        if not isinstance(journal, Journal):
            return call()
        goal = current_goal_revision.get()
        refs = {'operation': operation}
        if self.controller is not None:
            profile = self.controller.get_active_mode()
            if isinstance(profile, str):
                refs['profile'] = profile
        if goal:
            refs.update(task_id=goal[0], goal_revision=goal[1])
        with journal.scope('model_call', uuid.uuid4().hex, refs=refs) as cycle:
            started = time.monotonic()
            result = call()
            meta = getattr(self._gateway, '_response_meta', None)
            technical = {'elapsed_seconds': time.monotonic() - started}
            if meta is not None:
                technical.update({key: value for key, value in meta.get().items()
                                  if key in {'model', 'provider'} and isinstance(value, str)})
                meta.set({**meta.get(), 'cycle_id': cycle})
            if isinstance(result, dict):
                technical.update({key: value for key, value in result.items()
                                  if key in {'model', 'provider'} and isinstance(value, str)})
            journal.event(cycle, 'inference_returned', data=technical)
            return result

    def web_search(self, query, **kwargs):
        return self._gateway.web_search(query, **kwargs)

    def read_document(self, url, allowed_urls):
        return self._gateway.read_document(url, allowed_urls)

    def record_contract_failure(self, contract, raw, errors, schema):
        return self._gateway.record_contract_failure(contract, raw, errors, schema)

    def diagnostic_snapshot(self):
        return self._gateway.diagnostic_snapshot()

    def contract_rejection(self, reference):
        return self._gateway.contract_rejection(reference)

    def test_all_connections(self):
        return self._gateway.test_all_connections()

    # Model and VM lifecycle coordination
    def set_mode(self, mode: str, notifier=None) -> str:
        transitions = getattr(self._gateway, 'transitions', None)
        if transitions is not None:
            if str(mode).strip().lower() in {'?', 'status', 'info'}:
                snapshot = transitions.snapshot()
                active = [t for t in snapshot['transitions'] if t['status'] == 'active']
                return f"Modo efectivo: {snapshot['effective_mode']}." + (
                    f" Cambio a {active[0]['requested_mode']} en curso: {active[0]['id']}. /stop para cancelar." if active else '')
            if str(mode).strip().lower() in {'cancel', 'cancelar'}:
                result = transitions.cancel(notifier=notifier)
                return f"Cambio {result['status']}; modo {result['effective_mode']} conservado."
            result = transitions.request(mode, notifier=notifier)
            return (f"Cambio solicitado a {result['requested_mode']}; modo efectivo {result['effective_mode']}. "
                    f"ID: {result['id']}. /stop para cancelar.")
        if self.controller is not None:
            return self.controller.set_mode(mode, notifier=notifier)
        raise RuntimeError("No model controller configured in Brain.")

    def cancel_transition(self):
        transitions = getattr(self._gateway, 'transitions', None)
        return transitions.cancel() if transitions is not None else {'status': 'idle', 'ids': []}

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
