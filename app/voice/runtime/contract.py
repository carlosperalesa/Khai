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
    if folder.is_symlink() or {p.name for p in folder.iterdir()} != {'app','data','other'}:
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


def verify_result(result, criterion, workspace):
    """A clean exit is necessary, never sufficient. Criteria must come from the owner."""
    if result.get("status") != "finished" or result.get("returncode") != 0:
        return None, "La ejecución no terminó correctamente"
    if not criterion:
        return None, "Falta un criterio del usuario; requiere /verify"
    if criterion.get("kind") != "file_sha256":
        return None, "Criterio no soportado"
    root = Path(workspace).resolve()
    relative = Path(criterion.get("path", ""))
    if relative.is_absolute() or ".." in relative.parts:
        return False, "Ruta de evidencia inválida"
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        return False, "Artefacto ausente o fuera del workspace"
    if path.stat().st_size > 64 * 1024 * 1024:
        return None, "Artefacto demasiado grande para este verificador"
    actual = digest(path.read_bytes())
    return actual == criterion.get("sha256"), {"path": str(relative), "sha256": actual}
