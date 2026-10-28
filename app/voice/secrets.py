"""Provider credentials belong to Voice; never exported into worker environments."""
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from dotenv import dotenv_values


def migrate_secrets(data_dir, legacy):
    """Copy once without overwriting either source; never display credential values."""
    target = Path(data_dir)/'voice'/'secrets.env'
    if not target.exists() and Path(legacy).is_file():
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as stream:
            stream.write(Path(legacy).read_bytes())
        if os.name=='posix':
            target.chmod(0o600)
    return target


class Secrets:
    _write_lock = threading.RLock()
    _allowed_key = re.compile(r'^[A-Z][A-Z0-9_]{1,127}$')

    def __init__(self, data_dir):
        self.path = Path(data_dir)/'voice'/'secrets.env'
        self._values = {}
        self.reload()

    def reload(self):
        path = self.path
        self._values = {**os.environ, **{k:v for k,v in dotenv_values(path).items() if v is not None}}
        return self

    def get(self, key, default=''):
        return self._values.get(key, default)

    def items(self):
        return self._values.items()

    def put(self, key, value, expected_version=0):
        """Atomically update one allowlisted env key without returning its value."""
        if not self._allowed_key.fullmatch(str(key)):
            raise ValueError('Nombre de credencial inválido')
        if not isinstance(value, str) or not value or '\n' in value or '\r' in value:
            raise ValueError('Valor de credencial inválido')
        if not isinstance(expected_version, int) or expected_version < 0:
            raise ValueError('Versión de credencial inválida')
        with self._write_lock:
            current = self._file_version(key)
            if current != expected_version:
                raise PermissionError('La versión de la credencial cambió')
            lines = self.path.read_text(encoding='utf-8').splitlines(keepends=True) if self.path.exists() else []
            next_version = expected_version + 1
            replacement = key + '=' + value + '\n'
            version_marker = '# KHAI_VERSION ' + key + '=' + str(next_version) + '\n'
            found = False
            updated = []
            for line in lines:
                if line.startswith('# KHAI_VERSION ' + key + '='):
                    continue
                if re.match(r'^' + re.escape(key) + r'=', line):
                    if not found:
                        updated.append(replacement)
                        found = True
                else:
                    updated.append(line)
            if not found:
                if updated and not updated[-1].endswith('\n'):
                    updated[-1] += '\n'
                updated.append(replacement)
            for index, line in enumerate(updated):
                if line == replacement:
                    updated.insert(index, version_marker)
                    break
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.secrets-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                    stream.write(''.join(updated))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
                if os.name == 'posix':
                    self.path.chmod(0o600)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.reload()
            return {'key': key, 'version': expected_version + 1}

    def _file_version(self, key):
        if not self.path.exists():
            return 0
        lines = self.path.read_text(encoding='utf-8').splitlines()
        for line in lines:
            match = re.match(r'^# KHAI_VERSION ' + re.escape(key) + r'=(\d+)$', line)
            if match:
                return int(match.group(1))
        return 1 if any(re.match(r'^' + re.escape(key) + r'=', line) for line in lines) else 0

    def version(self, key):
        with self._write_lock:
            return self._file_version(key)


class CredentialStatusPort:
    """Expose credential availability to coordination without exporting values."""

    def __init__(self, secrets):
        self.__secrets = secrets

    def status(self, key):
        return {'available': bool(self.__secrets.get(key)),
                'version': self.__secrets.version(key)}


class SecretCaptureService:
    """Voice boundary for private Telegram credential captures."""

    COMMANDS = {'/us': 'username', '/pas': 'password', '/api': 'api_key'}

    def __init__(self, secrets, port):
        self.secrets, self.port = secrets, port

    def capture(self, command, value, user_id, chat_id):
        kind = self.COMMANDS.get(command.lower())
        if not kind:
            raise ValueError('Comando de credencial inválido')
        if not value or value.startswith('/'):
            raise ValueError('Captura de credencial inválida')
        requests = self.port.pending(user_id, chat_id, kind)
        if len(requests) != 1:
            raise PermissionError('No hay una solicitud de credencial compatible')
        request = requests[0]
        if (request['status'] == 'capturing'
                and self.secrets.version(request['env_key']) == request['expected_version'] + 1):
            receipt_id = 'secret_' + uuid.uuid4().hex
            self.port.complete(request['request_id'], receipt_id)
            return receipt_id
        self.port.capture(request['request_id'], user_id, chat_id, kind)
        try:
            self.secrets.put(request['env_key'], value, request['expected_version'])
        except Exception:
            self.port.abandon(request['request_id'])
            raise
        receipt_id = 'secret_' + uuid.uuid4().hex
        self.port.complete(request['request_id'], receipt_id)
        return receipt_id

    def flush(self, deliver, limit=50):
        """Deliver durable receipts; acknowledgement makes restart recovery safe."""
        for event in self.port.outbox(limit):
            deliver(event)
            self.port.acknowledge(event['event_id'])
