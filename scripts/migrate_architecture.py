"""Run with the service stopped: python -m scripts.migrate_architecture."""
import argparse
import json
from pathlib import Path
from app.common.migration import migrate_data
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
        result = migrate_data(root)
        Identity(root/'mind'/'identity.yaml',root/'memory'/'agent.md')
        migrate_secrets(root,root.parent/'secrets.env')
    finally:
        lock.release()
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
