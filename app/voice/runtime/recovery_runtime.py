"""Technical channel for Hand's recovery steps. Only the closed catalog crosses the wire."""
import requests
from app.heart.catalog import ACTION_ID, ACTIONS, TARGETS
from app.voice.runtime.contract import proxy_url, proxy_headers


class RecoveryRejected(ValueError):
    """The executor refused the step before any side effect."""


class ProxyRecoveryExecutor:
    def _check(self, response):
        if 400 <= response.status_code < 500:
            raise RecoveryRejected(response.text[:300])
        response.raise_for_status()
        return response.json()

    def submit(self, action_id, action, target):
        if not ACTION_ID.fullmatch(action_id) or action not in ACTIONS or target not in TARGETS:
            raise RecoveryRejected('Paso de recuperación fuera del catálogo')
        return self._check(requests.post(proxy_url('recovery-actions'), headers=proxy_headers(), timeout=15,
                                         json={'action_id': action_id, 'action': action, 'target': target}))

    def status(self, action_id):
        if not ACTION_ID.fullmatch(action_id):
            raise RecoveryRejected('Identificador de acción inválido')
        return self._check(requests.get(proxy_url('recovery-actions/' + action_id), headers=proxy_headers(), timeout=15))
