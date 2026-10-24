"""Provider credentials belong to Voice; never exported into worker environments."""
import os
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
    def __init__(self, data_dir):
        path = Path(data_dir)/'voice'/'secrets.env'
        self._values = {**os.environ, **{k:v for k,v in dotenv_values(path).items() if v is not None}}

    def get(self, key, default=''):
        return self._values.get(key, default)

    def items(self):
        return self._values.items()
