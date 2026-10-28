"""Initialize the clean control-store architecture with the service stopped."""
import argparse
import json
from pathlib import Path
from app.kernel.events import Events
from app.kernel.control_plane import ControlPlane
from app.mind.identity import Identity
from app.voice.secrets import migrate_secrets
from app.pixy.control import InstanceLock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir',default='data')
    args = parser.parse_args()
    root = Path(args.data_dir).resolve()
    lock = InstanceLock(root/'coordinator.lock')
    try:
        lock.acquire()
        Identity(root/'mind'/'identity.yaml',root/'memory'/'agent.md')
        migrate_secrets(root,root.parent/'secrets.env')
        legacy = [path for path in (
            root/'openclaw.db', root/'logs.sqlite3', root/'pixy'/'logs.sqlite3',
            root/'pixy'/'rules.sqlite3', root/'mind'/'memory.sqlite3',
            root/'heart'/'heart.sqlite3', root/'hand'/'hand.sqlite3',
            root/'dreams'/'dreams.sqlite3') if path.exists()]
        if legacy:
            raise RuntimeError('Corte limpio requerido; aún existen bases legacy: ' +
                               ', '.join(str(path) for path in legacy))
        control = root/'control.sqlite3'
        events = Events(control)
        ControlPlane(control, events)
        result = {'version': 2, 'control_store': str(control),
                  'identity': str(root/'mind'/'identity.yaml'),
                  'secrets': str(root/'voice'/'secrets.env')}
    finally:
        lock.release()
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
