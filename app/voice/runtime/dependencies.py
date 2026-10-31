"""Voice resolves wheels; the proxy verifies a sealed cache and installs offline."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

TARGET = 'cp310-manylinux2014-x86_64'
PIN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*==[A-Za-z0-9][A-Za-z0-9_.+\-]*')
MAX_BYTES = 128 * 1024 * 1024


class DependencyPreparationError(RuntimeError):
    """Preparation failed before runtime dispatch; no worker was submitted."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dependency_plan(roots):
    if (not isinstance(roots, list) or len(roots) > 30
            or any(not isinstance(r, str) or not PIN.fullmatch(r) for r in roots)):
        raise ValueError('Dependencies require exact name==version pins; URLs and source builds are forbidden')
    names = [re.sub('[-_.]+', '-', r.split('==')[0]).lower() for r in roots]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate dependency names')
    return {'roots': sorted(roots), 'target': TARGET, 'only_binary': True,
            'index': 'https://pypi.org/simple', 'max_bytes': MAX_BYTES}


def requirements(files):
    return ''.join(f"{f['name']}=={f['version']} --hash=sha256:{f['sha256']}\n"
                   for f in files).encode()


def verify_cache(root, lock_id, roots):
    """No network, no imports of downloaded code, no paths supplied by a worker."""
    if not isinstance(lock_id, str) or not re.fullmatch('[a-f0-9]{64}', lock_id):
        raise ValueError('Missing or invalid dependency lock')
    folder = Path(root) / lock_id
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Dependency cache missing')
    manifest_path = folder / 'manifest.json'
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 128000:
        raise ValueError('Invalid dependency manifest')
    manifest = json.loads(manifest_path.read_bytes())
    if sha(canonical(manifest)) != lock_id or manifest['plan'] != dependency_plan(roots):
        raise ValueError('Dependency lock does not match approved roots')
    files = manifest['files']
    if not files or len(files) > 100:
        raise ValueError('Invalid wheel count')
    expected = {'manifest.json', 'requirements.lock'}
    total = 0
    for f in files:
        name = f['filename']
        if (not re.fullmatch(r'[A-Za-z0-9_.+\-]+\.whl', name)
                or name in expected or not PIN.fullmatch(f"{f['name']}=={f['version']}")):
            raise ValueError('Invalid wheel record')
        expected.add(name)
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Wheel missing or linked')
        total += path.stat().st_size
        if total > MAX_BYTES or sha(path.read_bytes()) != f['sha256']:
            raise ValueError('Wheel hash mismatch or cache too large')
    lock = folder / 'requirements.lock'
    if (set(p.name for p in folder.iterdir()) != expected or lock.is_symlink()
            or lock.read_bytes() != requirements(files)):
        raise ValueError('Dependency cache contains unverified content')
    return folder, manifest


def build_context(root, lock_id, roots, destination):
    folder, manifest = verify_cache(root, lock_id, roots)
    dockerfile = (b'FROM python:3.10-slim\nCOPY wheels /wheels\n'
                  b'RUN python -m venv /opt/venv && /opt/venv/bin/pip install '
                  b'--no-index --find-links=/wheels --require-hashes --only-binary=:all: '
                  b'-r /wheels/requirements.lock && rm -rf /wheels\n'
                  b'ENV PATH="/opt/venv/bin:$PATH"\n')
    import io
    with tarfile.open(destination, 'w') as archive:
        item = tarfile.TarInfo('Dockerfile'); item.size = len(dockerfile)
        archive.addfile(item, io.BytesIO(dockerfile))
        for name in ['requirements.lock'] + [f['filename'] for f in manifest['files']]:
            archive.add(folder / name, arcname='wheels/' + name, recursive=False)


class WheelCache:
    def __init__(self, data_dir, runner=None, release_reader=None):
        self.root = Path(data_dir) / 'voice' / 'dependencies'
        self.runner = runner or subprocess.run
        self.release_reader = release_reader or self._release

    @staticmethod
    def _release(name, version):
        # Both segments were validated as package pins. No arbitrary URLs or credentials.
        import requests
        with requests.get(f'https://pypi.org/pypi/{name}/{version}/json', timeout=15,
                          allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('PyPI release metadata unavailable')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError('PyPI metadata too large')
            return json.loads(data)['urls']

    def prepare(self, roots):
        plan = dependency_plan(roots)
        if not roots:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        from app.pixy.control import InstanceLock
        guard = InstanceLock(self.root / 'prepare.lock')
        guard.acquire()
        try:
            index = self.root / (sha(canonical(plan)) + '.index')
            if index.exists():
                lock_id = index.read_text().strip()
                verify_cache(self.root, lock_id, roots)
                return lock_id
            with tempfile.TemporaryDirectory(prefix='prepare-', dir=self.root) as temp:
                folder = Path(temp)
                args = [sys.executable, '-m', 'pip', '--isolated', 'download', '--only-binary=:all:',
                        '--index-url', plan['index'], '--disable-pip-version-check', '--no-cache-dir',
                        '--timeout', '15', '--retries', '0', '--platform', 'manylinux2014_x86_64',
                        '--platform', 'manylinux_2_17_x86_64', '--implementation', 'cp',
                        '--python-version', '310', '--abi', 'cp310', '--abi', 'abi3', '--abi', 'none',
                        '--dest', str(folder), *plan['roots']]
                env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP') if k in os.environ}
                result = self.runner(args, capture_output=True, env=env, timeout=120)
                if result.returncode:
                    raise ValueError('PyPI wheel resolution failed; no source builds or provider fallback attempted')
                paths = sorted(folder.iterdir())
                if not paths or len(paths) > 100 or sum(p.stat().st_size for p in paths) > MAX_BYTES:
                    raise ValueError('Dependency download exceeded cache limits')
                files = []
                for path in paths:
                    # Wheel names encode distribution and version in their first two fields.
                    if path.is_symlink() or not re.fullmatch(r'[A-Za-z0-9_.+\-]+\.whl', path.name):
                        raise ValueError('Only regular wheel files are allowed')
                    name, version = path.name.split('-')[:2]
                    if not PIN.fullmatch(name + '==' + version):
                        raise ValueError('Invalid wheel identity')
                    digest = sha(path.read_bytes())
                    releases = self.release_reader(name, version)
                    if not any(r['filename'] == path.name and r['digests']['sha256'] == digest
                               and not r.get('yanked') for r in releases):
                        raise ValueError('Wheel does not match a non-yanked official PyPI release')
                    files.append({'filename': path.name, 'name': name, 'version': version, 'sha256': digest})
                manifest = {'plan': plan, 'files': files}
                lock_id = sha(canonical(manifest))
                (folder / 'manifest.json').write_bytes(canonical(manifest))
                (folder / 'requirements.lock').write_bytes(requirements(files))
                if (self.root / lock_id).exists():
                    verify_cache(self.root, lock_id, roots)
                else:
                    shutil.copytree(folder, self.root / lock_id)
                verify_cache(self.root, lock_id, roots)
                temporary = index.with_suffix('.tmp')
                temporary.write_text(lock_id)
                os.replace(temporary, index)
                return lock_id
        finally:
            guard.release()
