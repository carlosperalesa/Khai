import sqlite3
from pathlib import Path


class Diagnosis:
    def __init__(self, root):
        self.root = Path(root)

    def inspect(self):
        databases = {}
        for path in self.root.glob('*/*.sqlite3'):
            if path.parent.name=='backups':
                continue
            conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                databases[str(path.relative_to(self.root))] = conn.execute('PRAGMA quick_check').fetchone()[0]
            finally:
                conn.close()
        return {'status':'ok' if all(x=='ok' for x in databases.values()) else 'error','databases':databases}
