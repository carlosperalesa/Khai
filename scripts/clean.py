"""Cross-platform cleanup utility for caches, old logs, and temporary artifacts."""
import argparse
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def clean(days=7, include_venv=False, dry_run=False):
    now = time.time()
    cutoff = now - (days * 86400)
    cleaned_items = 0

    print(f"Iniciando limpieza en: {ROOT}")

    # 1. Clean __pycache__ and bytecode
    for path in ROOT.rglob("__pycache__"):
        if not include_venv and any(p in path.parts for p in (".venv", ".tools", ".gcloud")):
            continue
        if path.is_dir():
            print(f"  [Eliminando dir] {path.relative_to(ROOT)}")
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
            cleaned_items += 1

    for ext in ("*.pyc", "*.pyo"):
        for path in ROOT.rglob(ext):
            if not include_venv and any(p in path.parts for p in (".venv", ".tools", ".gcloud")):
                continue
            if path.is_file():
                if not dry_run:
                    path.unlink(missing_ok=True)
                cleaned_items += 1

    # 2. Test temp & test caches
    for target in (".pytest_cache", ".coverage", ".test-temp", "MagicMock", "other/scratch"):
        p = ROOT / target
        if p.exists():
            print(f"  [Eliminando] {target}")
            if not dry_run:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink(missing_ok=True)
            cleaned_items += 1

    # 3. Logs older than specified days
    logs_dir = ROOT / "data" / "logs"
    if logs_dir.is_dir():
        for log_file in logs_dir.glob("*.log"):
            if log_file.is_file():
                try:
                    if log_file.stat().st_mtime < cutoff:
                        print(f"  [Eliminando log antiguo] {log_file.relative_to(ROOT)}")
                        if not dry_run:
                            log_file.unlink(missing_ok=True)
                        cleaned_items += 1
                except OSError:
                    pass

    # 4. Clean virtual environment if explicitly requested
    if include_venv:
        venv_dir = ROOT / ".venv"
        if venv_dir.is_dir():
            print(f"  [Eliminando venv] .venv")
            if not dry_run:
                shutil.rmtree(venv_dir, ignore_errors=True)
            cleaned_items += 1

    action = "Identificados" if dry_run else "Limpiados"
    print(f"Limpieza completada. {action} {cleaned_items} elementos.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Limpieza multiplataforma de caché, logs y temporales.")
    parser.add_argument("--days", type=int, default=7, help="Antigüedad en días para eliminar logs (default: 7)")
    parser.add_argument("--include-venv", action="store_true", help="Eliminar también el directorio .venv")
    parser.add_argument("--dry-run", action="store_true", help="Mostrar lo que se limpiaría sin borrarlo")
    args = parser.parse_args()
    clean(days=args.days, include_venv=args.include_venv, dry_run=args.dry_run)
