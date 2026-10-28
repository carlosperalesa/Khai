"""Controlador HTTPS de Google Compute Engine para el nodo efímero Dolphin (Spot CPU/GPU)."""
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
    'low': {
        'machine_type': 'e2-standard-8',
        'label': 'low (CPU Spot e2-standard-8, 8 vCPU / 32 GB RAM)',
        'rate_clp_per_hour': 152.0,
    },
    'high': {
        'machine_type': 'g2-standard-4',
        'label': 'high (GPU Spot g2-standard-4, NVIDIA L4 24 GB VRAM)',
        'rate_clp_per_hour': 418.0,
    },
}


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
    """Administra la VM Spot efímera de Dolphin en GCE sobre un único disco persistente."""

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
        adopt_schema(self.db_path, 'dolphin-sessions', ['dolphin_sessions'])
        self._token = None
        self._token_exp = 0.0

    def _cfg(self, key: str, default: str = '') -> str:
        if self.secrets and key in self.secrets and str(self.secrets[key]).strip():
            return str(self.secrets[key]).strip()
        return os.getenv(key, default).strip()

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
                'UPDATE dolphin_sessions SET ended_at = MIN(?, started_at + 300) '
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
                'UPDATE dolphin_sessions SET ended_at = MIN(?, started_at + 300) '
                'WHERE ended_at IS NULL AND ? - started_at > 360',
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
            end = row['ended_at'] if row['ended_at'] is not None else min(now, row['started_at'] + 300)
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

    def start(self, profile: str = 'low') -> str:
        profile = (profile or 'low').strip().lower()
        if profile not in PROFILES:
            raise ValueError("Perfil inválido. Usa '/on low' o '/on high'.")
        spec = PROFILES[profile]
        target_mt = spec['machine_type']
        current = self.get_instance()
        if current is not None:
            curr_mt = current.get('machineType', '').rsplit('/', 1)[-1]
            curr_status = current.get('status', '')
            if curr_mt == target_mt and curr_status == 'RUNNING':
                ready = self._wait_ollama_ready(timeout_sec=10)
                estado_red = 'conectado por túnel WireGuard' if ready else 'arrancando servicio Ollama'
                return f"Dolphin ya está activo en modo {spec['label']} ({estado_red}). {self.billing_summary()}"
            if curr_mt == target_mt and curr_status == 'TERMINATED':
                resp = requests.post(
                    self._gce_url(f'instances/{quote(self.instance)}/start'),
                    headers=self._headers(),
                    timeout=20,
                )
                resp.raise_for_status()
                self._wait_operation(resp.json()['name'])
                self._record_start(profile, target_mt, spec['rate_clp_per_hour'])
                ready = self._wait_ollama_ready(timeout_sec=45)
                suf = 'Túnel WireGuard y Ollama listos.' if ready else 'VM iniciada; Ollama terminará de responder en unos segundos.'
                return (
                    f"Dolphin encendido en modo {spec['label']}. {suf} "
                    f"Auto-apagado tras 5 min sin actividad. {self.billing_summary()}"
                )
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
        self._wait_operation(ins_resp.json()['name'])
        self._record_start(profile, target_mt, spec['rate_clp_per_hour'])
        ready = self._wait_ollama_ready(timeout_sec=45)
        suf = 'Túnel WireGuard y Ollama listos.' if ready else 'VM creada; Ollama terminará de responder en unos segundos.'
        return (
            f"Dolphin encendido en modo {spec['label']} sobre disco {self.disk} (32 GB). "
            f"{suf} Auto-apagado tras 5 min sin actividad. {self.billing_summary()}"
        )

    def stop(self) -> str:
        current = self.get_instance()
        self._record_stop()
        if current is None:
            return f"Dolphin ya estaba apagado (instancia eliminada; disco {self.disk} en reposo). {self.billing_summary()}"
        resp = requests.delete(
            self._gce_url(f'instances/{quote(self.instance)}'),
            headers=self._headers(),
            timeout=20,
        )
        if resp.status_code != 404:
            resp.raise_for_status()
            self._wait_operation(resp.json()['name'])
        return (
            f"Dolphin apagado: instancia Spot eliminada y cobro de cómputo detenido. "
            f"El disco {self.disk} (32 GB) permanece en reposo. {self.billing_summary()}"
        )

    def status(self) -> str:
        current = self.get_instance()
        if current is None:
            return f"Estado Dolphin: APAGADO (sin VM activa; disco {self.disk} de 32 GB en reposo). {self.billing_summary()}"
        curr_mt = current.get('machineType', '').rsplit('/', 1)[-1]
        curr_status = current.get('status', 'UNKNOWN')
        if curr_status == 'TERMINATED':
            self._record_stop()
        return (
            f"Estado Dolphin: {curr_status} (máquina {curr_mt}, disco {self.disk}). "
            f"{self.billing_summary()}"
        )
