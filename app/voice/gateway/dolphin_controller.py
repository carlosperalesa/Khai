"""Controlador HTTPS de Google Compute Engine para modos de Khai (/mode low, mid, high, ?)."""
import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from urllib.parse import quote

import requests

from app.kernel.database import adopt_schema, connection

logger = logging.getLogger(__name__)

PROFILES = {
    'mid': {
        'machine_type': 'e2-standard-8',
        'label': 'mid (Dolphin CPU Spot e2-standard-8, 8 vCPU / 32 GB RAM)',
        'rate_clp_per_hour': 152.0,
    },
    'high': {
        'machine_type': 'g2-standard-4',
        'label': 'high (Dolphin GPU Spot g2-standard-4, NVIDIA L4 24 GB VRAM)',
        'rate_clp_per_hour': 418.0,
    },
}

MACHINE_TO_MODE = {
    'e2-standard-8': 'mid',
    'g2-standard-4': 'high',
}

INACTIVITY_TIMEOUT_SEC = 600
PROFILE_LABELS = {'low': 'Low', 'mid': 'Medium', 'high': 'High'}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _sign_rs256(private_key_pem: str, signing_input: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(private_key_pem.encode('utf-8'), password=None)
        return key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    except ImportError:
        with tempfile.NamedTemporaryFile('w', suffix='.pem', delete=False, encoding='utf-8') as f:
            f.write(private_key_pem)
            key_path = f.name
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        try:
            proc = subprocess.run(
                ['openssl', 'dgst', '-sha256', '-sign', key_path],
                input=signing_input,
                capture_output=True,
                check=True,
                timeout=10,
            )
            return proc.stdout
        finally:
            try:
                os.remove(key_path)
            except OSError:
                pass


class DolphinController:
    """Administra el modo conversacional (low=Gemini, mid=Dolphin CPU Spot, high=Dolphin GPU Spot)."""

    def __init__(self, data_dir: str, secrets=None):
        self.data_dir = str(data_dir)
        self.secrets = secrets
        self.project = self._cfg('KHAI_DOLPHIN_GCP_PROJECT', 'gen-lang-client-0149640270')
        self.zone = self._cfg('KHAI_DOLPHIN_GCP_ZONE', 'us-west1-a')
        self.instance = self._cfg('KHAI_DOLPHIN_GCP_INSTANCE', 'dolphin-node')
        self.disk = self._cfg('KHAI_DOLPHIN_GCP_DISK', 'dolphin-boot-disk')
        self.endpoint = self._cfg('KHAI_DOLPHIN_URL', 'http://10.200.0.2:11434').rstrip('/')
        self.credit_pool_clp = float(self._cfg('KHAI_DOLPHIN_CREDIT_CLP', '285000'))
        self.db_path = os.path.join(self.data_dir, 'voice', 'dolphin_usage.sqlite3')
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with connection(self.db_path) as conn:
            conn.execute(
                'CREATE TABLE IF NOT EXISTS dolphin_sessions('
                'id INTEGER PRIMARY KEY AUTOINCREMENT,'
                'profile TEXT NOT NULL,'
                'machine_type TEXT NOT NULL,'
                'started_at REAL NOT NULL,'
                'ended_at REAL,'
                'rate_clp_per_hour REAL NOT NULL)'
            )
            conn.execute(
                'CREATE TABLE IF NOT EXISTS dolphin_mode_state('
                'id INTEGER PRIMARY KEY CHECK (id = 1),'
                'active_mode TEXT NOT NULL,'
                'notified_mode TEXT NOT NULL)'
            )
            conn.execute(
                "INSERT OR IGNORE INTO dolphin_mode_state(id, active_mode, notified_mode) "
                "VALUES (1, 'low', 'low')"
            )
        adopt_schema(self.db_path, 'dolphin-sessions', ['dolphin_sessions', 'dolphin_mode_state'])
        self._token = None
        self._token_exp = 0.0

    def _cfg(self, key: str, default: str = '') -> str:
        if self.secrets is not None and hasattr(self.secrets, 'get'):
            val = self.secrets.get(key)
            if val is not None and str(val).strip():
                return str(val).strip()
        return os.getenv(key, default).strip()

    def get_active_mode(self) -> str:
        with connection(self.db_path) as conn:
            row = conn.execute('SELECT active_mode FROM dolphin_mode_state WHERE id = 1').fetchone()
            return row['active_mode'] if row else 'low'

    def _set_mode_state(self, active_mode: str, *, mark_notified: bool = False) -> None:
        with connection(self.db_path) as conn:
            if mark_notified:
                conn.execute(
                    'UPDATE dolphin_mode_state SET active_mode = ?, notified_mode = ? WHERE id = 1',
                    (active_mode, active_mode),
                )
            else:
                conn.execute(
                    'UPDATE dolphin_mode_state SET active_mode = ? WHERE id = 1',
                    (active_mode,),
                )

    def mark_fallback_to_low(self) -> None:
        """Registra que Dolphin se apagó/no está disponible y vuelve a modo low (Gemini)."""
        self._record_stop()
        self._set_mode_state('low', mark_notified=False)
        try:
            self._delete_instance(wait=False)
        except Exception as exc:
            logger.warning('No se pudo apagar Dolphin tras el fallback (%s)', type(exc).__name__)

    def consume_transition_banner(self, effective_mode: str) -> str:
        """Devuelve '[Modo X]\\n' solo cuando el modo efectivo cambió respecto al último notificado."""
        with connection(self.db_path) as conn:
            row = conn.execute(
                'SELECT active_mode, notified_mode FROM dolphin_mode_state WHERE id = 1'
            ).fetchone()
            last_notified = row['notified_mode'] if row else 'low'
            if last_notified != effective_mode:
                conn.execute(
                    'UPDATE dolphin_mode_state SET active_mode = ?, notified_mode = ? WHERE id = 1',
                    (effective_mode, effective_mode),
                )
                label = PROFILE_LABELS.get(effective_mode, effective_mode.capitalize())
                return f'[Modo {label}]\n'
        return ''

    def _load_service_account(self) -> dict:
        raw_json = self._cfg('KHAI_DOLPHIN_GCP_SA_JSON', '')
        if raw_json:
            if raw_json.startswith('{'):
                return json.loads(raw_json)
            return json.loads(base64.b64decode(raw_json).decode('utf-8'))
        sa_path = self._cfg('KHAI_DOLPHIN_GCP_SA_PATH', 'data/certs/khai-dolphin-sa.json')
        if os.path.isfile(sa_path):
            with open(sa_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        raise RuntimeError('Credencial de Service Account GCP para Dolphin no configurada')

    def _access_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        sa = self._load_service_account()
        iat = int(now)
        exp = iat + 3600
        header = _b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode('utf-8'))
        payload = _b64url(json.dumps({
            'iss': sa['client_email'],
            'scope': 'https://www.googleapis.com/auth/compute',
            'aud': sa.get('token_uri', 'https://oauth2.googleapis.com/token'),
            'iat': iat,
            'exp': exp,
        }).encode('utf-8'))
        signing_input = f'{header}.{payload}'.encode('ascii')
        signature = _b64url(_sign_rs256(sa['private_key'], signing_input))
        assertion = f'{header}.{payload}.{signature}'
        resp = requests.post(
            sa.get('token_uri', 'https://oauth2.googleapis.com/token'),
            data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
                'assertion': assertion,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data['access_token']
        self._token_exp = now + int(data.get('expires_in', 3600))
        return self._token

    def _gce_url(self, suffix: str = '') -> str:
        base = (
            f'https://compute.googleapis.com/compute/v1/projects/'
            f'{quote(self.project)}/zones/{quote(self.zone)}'
        )
        return f'{base}/{suffix.lstrip("/")}' if suffix else base

    def _headers(self) -> dict:
        return {
            'Authorization': f'Bearer {self._access_token()}',
            'Content-Type': 'application/json',
        }

    def get_instance(self) -> dict | None:
        resp = requests.get(
            self._gce_url(f'instances/{quote(self.instance)}'),
            headers=self._headers(),
            timeout=15,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _wait_operation(self, op_name: str, timeout_sec: int = 90) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            resp = requests.get(
                self._gce_url(f'operations/{quote(op_name)}'),
                headers=self._headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get('status') == 'DONE':
                if 'error' in data:
                    errors = data['error'].get('errors', [])
                    msg = '; '.join(e.get('message', e.get('code', 'GCE error')) for e in errors)
                    raise RuntimeError(msg or 'Operación GCE falló')
                return
            time.sleep(2)
        raise TimeoutError(f'Operación GCE {op_name} excedió {timeout_sec}s')

    def _record_start(self, profile: str, machine_type: str, rate: float) -> None:
        now = time.time()
        with connection(self.db_path) as conn:
            conn.execute(
                f'UPDATE dolphin_sessions SET ended_at = MIN(?, started_at + {INACTIVITY_TIMEOUT_SEC}) '
                'WHERE ended_at IS NULL',
                (now,),
            )
            conn.execute(
                'INSERT INTO dolphin_sessions(profile, machine_type, started_at, ended_at, rate_clp_per_hour) '
                'VALUES (?, ?, ?, NULL, ?)',
                (profile, machine_type, now, rate),
            )

    def _record_stop(self) -> None:
        now = time.time()
        with connection(self.db_path) as conn:
            conn.execute(
                f'UPDATE dolphin_sessions SET ended_at = MIN(?, started_at + {INACTIVITY_TIMEOUT_SEC}) '
                f'WHERE ended_at IS NULL AND ? - started_at > {INACTIVITY_TIMEOUT_SEC + 120}',
                (now, now),
            )
            conn.execute(
                'UPDATE dolphin_sessions SET ended_at = ? WHERE ended_at IS NULL',
                (now,),
            )

    def billing_summary(self) -> str:
        now = time.time()
        total_clp = 0.0
        with connection(self.db_path) as conn:
            rows = conn.execute(
                'SELECT started_at, ended_at, rate_clp_per_hour FROM dolphin_sessions'
            ).fetchall()
        for row in rows:
            end = row['ended_at'] if row['ended_at'] is not None else min(now, row['started_at'] + INACTIVITY_TIMEOUT_SEC)
            dur_sec = max(60.0, end - row['started_at'])
            total_clp += (dur_sec / 3600.0) * float(row['rate_clp_per_hour'])
        remaining_clp = max(0.0, self.credit_pool_clp - total_clp)
        next_step_clp = (int(total_clp // 1000) + 1) * 1000
        mode = 'CUBIERTO POR CREDITOS' if remaining_clp > 0 else 'COBRO REAL EN TARJETA'
        return (
            f'Modalidad de cobro: {mode}. '
            f'Consumo acumulado Dolphin: ${total_clp:,.0f} CLP '
            f'(próxima alarma de presupuesto en ${next_step_clp:,.0f} CLP). '
            f'Crédito estimado disponible: ${remaining_clp:,.0f} CLP.'
        )

    def _wait_ollama_ready(self, timeout_sec: int = 45) -> bool:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                r = requests.get(f'{self.endpoint}/api/tags', timeout=3, allow_redirects=False)
                if r.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(2)
        return False

    def _warmup_model(self) -> None:
        """Carga el modelo y exige una inferencia mínima antes de declarar el modo activo."""
        timeout = max(30, min(300, int(self._cfg('KHAI_DOLPHIN_WARMUP_TIMEOUT', '180'))))
        model = self._cfg('KHAI_DOLPHIN_MODEL', 'dolphin3:8b')
        response = requests.post(
            f'{self.endpoint}/api/generate',
            json={
                'model': model,
                'prompt': 'Responde solamente OK.',
                'stream': False,
                'keep_alive': '10m',
                'options': {'num_predict': 2, 'temperature': 0},
            },
            timeout=timeout,
            allow_redirects=False,
        )
        response.raise_for_status()
        answer = response.json().get('response')
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError('Ollama no devolvió respuesta durante la precarga')

    def _confirm_ready(self) -> None:
        if not self._wait_ollama_ready(timeout_sec=60):
            raise TimeoutError('Ollama no abrió su API dentro del plazo de arranque')
        self._warmup_model()

    def _delete_instance(self, *, wait: bool = True) -> None:
        current = self.get_instance()
        if current is None:
            return
        response = requests.delete(
            self._gce_url(f'instances/{quote(self.instance)}'),
            headers=self._headers(),
            timeout=20,
        )
        if response.status_code == 404:
            return
        response.raise_for_status()
        if wait:
            self._wait_operation(response.json()['name'])

    def _activation_error(self, profile: str, exc: Exception) -> ValueError:
        if isinstance(exc, (requests.Timeout, TimeoutError)):
            detail = 'El arranque o la precarga excedieron el tiempo permitido.'
        elif isinstance(exc, requests.HTTPError):
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            detail = f'El proveedor respondió HTTP {status}.' if status else 'El proveedor rechazó la solicitud.'
        elif isinstance(exc, RuntimeError):
            detail = str(exc).strip()[:180] or 'La activación fue rechazada.'
        else:
            detail = f'Fallo interno de activación ({type(exc).__name__}).'
        return ValueError(
            f'[Modo Low]\nNo se pudo activar Dolphin {profile}. '
            f'Khai continúa con Gemini. {detail}'
        )

    def _rollback_activation(self) -> None:
        self._record_stop()
        self._set_mode_state('low', mark_notified=True)
        try:
            self._delete_instance(wait=True)
        except Exception as exc:
            logger.warning('No se pudo limpiar la VM tras una activacion fallida (%s)', type(exc).__name__)

    def set_mode(self, mode: str) -> str:
        mode = (mode or '?').strip().lower()
        aliases = {'h': 'high', 'm': 'mid', 'l': 'low', 'medium': 'mid'}
        mode = aliases.get(mode, mode)
        if mode in {'?', 'status', 'info'}:
            return self.status()
        if mode == 'low':
            return self.stop()
        if mode not in PROFILES:
            raise ValueError("Modo inválido. Usa '/m low' (Gemini), '/m mid' (Dolphin CPU), '/m high' (Dolphin GPU) o '/m ?'.")
        try:
            return self.start(mode)
        except (requests.RequestException, RuntimeError, TimeoutError, KeyError, ValueError) as exc:
            logger.warning('Activacion Dolphin %s fallida (%s)', mode, type(exc).__name__)
            self._rollback_activation()
            raise self._activation_error(mode, exc) from None

    def start(self, profile: str = 'mid') -> str:
        profile = (profile or 'mid').strip().lower()
        aliases = {'h': 'high', 'm': 'mid', 'l': 'low', 'medium': 'mid'}
        profile = aliases.get(profile, profile)
        if profile == 'low':
            return self.stop()
        if profile not in PROFILES:
            raise ValueError("Modo inválido. Usa '/m low', '/m mid', '/m high' o '/m ?'.")
        spec = PROFILES[profile]
        target_mt = spec['machine_type']
        label = PROFILE_LABELS.get(profile, profile.capitalize())
        current = self.get_instance()
        if current is not None:
            curr_mt = current.get('machineType', '').rsplit('/', 1)[-1]
            curr_status = current.get('status', '')
            if curr_mt == target_mt and curr_status == 'RUNNING':
                self._confirm_ready()
                self._set_mode_state(profile, mark_notified=True)
                return f"[Modo {label}]"
            if curr_mt == target_mt and curr_status == 'TERMINATED':
                resp = requests.post(
                    self._gce_url(f'instances/{quote(self.instance)}/start'),
                    headers=self._headers(),
                    timeout=20,
                )
                resp.raise_for_status()
                self._wait_operation(resp.json()['name'])
                self._record_start(profile, target_mt, spec['rate_clp_per_hour'])
                self._confirm_ready()
                self._set_mode_state(profile, mark_notified=True)
                return f"[Modo {label}]"
            del_resp = requests.delete(
                self._gce_url(f'instances/{quote(self.instance)}'),
                headers=self._headers(),
                timeout=20,
            )
            if del_resp.status_code != 404:
                del_resp.raise_for_status()
                self._wait_operation(del_resp.json()['name'])

        body = {
            'name': self.instance,
            'machineType': f'zones/{self.zone}/machineTypes/{target_mt}',
            'scheduling': {
                'provisioningModel': 'SPOT',
                'instanceTerminationAction': 'STOP',
                'onHostMaintenance': 'TERMINATE',
                'automaticRestart': False,
            },
            'disks': [
                {
                    'boot': True,
                    'autoDelete': False,
                    'source': f'projects/{self.project}/zones/{self.zone}/disks/{self.disk}',
                }
            ],
            'networkInterfaces': [
                {
                    'network': 'global/networks/default',
                    'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}],
                }
            ],
        }
        ins_resp = requests.post(
            self._gce_url('instances'),
            headers=self._headers(),
            json=body,
            timeout=20,
        )
        if not ins_resp.ok:
            raise RuntimeError(f'No se pudo crear instancia Spot ({target_mt}): {ins_resp.text[:300]}')
        try:
            self._wait_operation(ins_resp.json()['name'])
        except RuntimeError as op_err:
            if profile == 'high' and 'not enough resources' in str(op_err).lower():
                logger.warning('GCP sin cupo GPU Spot para high; fallback automático a mid (CPU)')
                return self.start('mid')
            raise

        self._record_start(profile, target_mt, spec['rate_clp_per_hour'])
        self._confirm_ready()
        self._set_mode_state(profile, mark_notified=True)
        return f"[Modo {label}]"

    def stop(self) -> str:
        current = self.get_instance()
        self._record_stop()
        self._set_mode_state('low', mark_notified=True)
        if current is None:
            return "[Modo Low]"
        self._delete_instance(wait=True)
        return "[Modo Low]"

    def status(self) -> str:
        current = self.get_instance()
        if current is None:
            self._record_stop()
            self._set_mode_state('low', mark_notified=True)
            return (
                f"[Modo Low]\nEstado: Gemini activo (Dolphin APAGADO; sin VM activa, disco {self.disk} de 32 GB en reposo). "
                f"{self.billing_summary()}"
            )
        curr_mt = current.get('machineType', '').rsplit('/', 1)[-1]
        curr_status = current.get('status', 'UNKNOWN')
        if curr_status != 'RUNNING':
            self._record_stop()
            self._set_mode_state('low', mark_notified=True)
            return (
                f"[Modo Low]\nEstado: Gemini activo (instancia Dolphin en estado {curr_status}, máquina {curr_mt}, disco {self.disk}). "
                f"{self.billing_summary()}"
            )
        inferred_mode = MACHINE_TO_MODE.get(curr_mt, 'mid')
        self._set_mode_state(inferred_mode, mark_notified=True)
        label = PROFILE_LABELS.get(inferred_mode, inferred_mode.capitalize())
        return (
            f"[Modo {label}]\nEstado Dolphin: {curr_status} (máquina {curr_mt}, disco {self.disk}). "
            f"{self.billing_summary()}"
        )
