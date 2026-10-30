"""Wire contract and deterministic verification, independent from model output."""
import hashlib
import json
import os
import re
from pathlib import Path


def digest(data):
    return hashlib.sha256(data).hexdigest()


def bundle_hash(folder):
    folder = Path(folder)
    if folder.is_symlink() or {p.name for p in folder.iterdir()} != {'SKILL.md','app','data','other'}:
        raise ValueError('Invalid skill bundle')
    values,total = [],0
    for path in sorted(folder.rglob('*')):
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError('Bundle links are forbidden')
        if path.is_file():
            total += path.stat().st_size
            if total>256*1024*1024:
                raise ValueError('Bundle too large')
            values.append((path.relative_to(folder).as_posix(),digest(path.read_bytes())))
    return digest(json.dumps(values,separators=(',',':')).encode())


def proxy_url(endpoint):
    base = os.getenv("PROXY_URL", "http://docker_proxy:8001").rstrip("/")
    if base.endswith("/run-skill"):
        base = base[:-10]
    return base + "/" + endpoint.lstrip("/")


def proxy_headers():
    token = os.getenv("PROXY_TOKEN", "")
    if len(token) < 32:
        raise RuntimeError("PROXY_TOKEN debe contener al menos 32 caracteres")
    return {"Authorization": "Bearer " + token}


def manifest_from_request(request):
    if "[MANIFEST]" not in request:
        return {}
    try:
        value, _ = json.JSONDecoder().raw_decode(request.split("[MANIFEST]", 1)[1].lstrip())
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def skill_metadata(code):
    first = code.splitlines()[0].strip().upper() if code.strip() else ""
    modes = {"# MODE: SAFE": "SAFE", "# MODE: AGENT": "AGENT", "# MODE: GOD": "GOD"}
    if first not in modes:
        raise ValueError("La primera línea debe declarar MODE: SAFE, AGENT o GOD")
    deps = []
    for line in code.splitlines()[:5]:
        if line.startswith("# PIP:"):
            deps = line[6:].split()
    for dep in deps:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:==[A-Za-z0-9][A-Za-z0-9_.+-]*)?", dep):
            raise ValueError("Dependencia inválida: usar nombre o nombre==versión")
    return modes[first], deps


class RuntimeCapabilities:
    """Declared, provider-independent execution and isolation guarantees."""

    def __init__(
        self,
        runtime="docker",
        version="1.0",
        modes=("SAFE", "AGENT", "GOD"),
        allowed_dependencies=True,
        filesystem=None,
        gateway_api=None,
        limits=None,
    ):
        if runtime not in {"docker", "local"}:
            raise ValueError("Runtime debe ser 'docker' o 'local'")
        self.runtime = runtime
        self.version = str(version)
        self.modes = tuple(modes)
        self.allowed_dependencies = bool(allowed_dependencies)
        self.filesystem = filesystem or {
            "readonly_root": True,
            "workspace_rw": True,
            "public_output": "/app/data/public",
        }
        self.gateway_api = gateway_api or {
            "supported": True,
            "module": "khai_gateway",
            "function": "request",
            "network_mode": "none",
        }
        self.limits = limits or {
            "cpu": 1.0,
            "memory_mb": 512,
            "pids": 128,
            "timeout_seconds": 120,
        }

    def validate_code(self, mode, dependencies):
        if mode not in self.modes:
            raise ValueError(f"Modo {mode} no soportado por este runtime (soportados: {self.modes})")
        if dependencies and not self.allowed_dependencies:
            raise ValueError("Instalación de dependencias dinámicas no permitida por este runtime")

    def to_dict(self):
        return {
            "runtime": self.runtime,
            "version": self.version,
            "modes": list(self.modes),
            "allowed_dependencies": self.allowed_dependencies,
            "filesystem": dict(self.filesystem),
            "gateway_api": dict(self.gateway_api),
            "limits": dict(self.limits),
        }


def default_runtime_capabilities(runtime="docker"):
    if runtime == "local":
        return RuntimeCapabilities(
            runtime="local",
            version="1.0",
            modes=("SAFE", "AGENT", "GOD"),
            allowed_dependencies=False,
            filesystem={
                "readonly_root": True,
                "workspace_rw": True,
                "public_output": "/app/data/public",
            },
            gateway_api={
                "supported": True,
                "module": "khai_gateway",
                "function": "request",
                "network_mode": "none",
            },
            limits={
                "cpu": 1.0,
                "memory_mb": 512,
                "pids": 128,
                "timeout_seconds": 120,
            },
        )
    return RuntimeCapabilities(
        runtime="docker",
        version="1.0",
        modes=("SAFE", "AGENT", "GOD"),
        allowed_dependencies=True,
    )

