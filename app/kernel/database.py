"""Owned SQLite connections and transactional, versioned migrations."""
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


@contextmanager
def connection(path):
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def migrate(path, migrations, component=None):
    with connection(path) as conn:
        if component:
            conn.execute('CREATE TABLE IF NOT EXISTS component_migrations(component TEXT, version INTEGER, applied_at REAL, PRIMARY KEY(component,version))')
            conn.execute('BEGIN IMMEDIATE')
            for version, statements in enumerate(migrations, 1):
                if conn.execute('SELECT 1 FROM component_migrations WHERE component=? AND version=?', (component, version)).fetchone():
                    continue
                for statement in statements:
                    conn.execute(statement)
                conn.execute('INSERT INTO component_migrations VALUES (?,?,?)', (component, version, time.time()))
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('SQLite integrity check failed: ' + str(path))
            return
        conn.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at REAL)')
        conn.execute('BEGIN IMMEDIATE')
        for version, statements in enumerate(migrations, 1):
            if conn.execute('SELECT 1 FROM schema_migrations WHERE version=?', (version,)).fetchone():
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute('INSERT INTO schema_migrations VALUES (?,?)', (version, time.time()))
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed: ' + str(path))


def backup(source, target):
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity check failed')
    finally:
        src.close()
        dst.close()


def adopt_schema(path, component, tables):
    """Record the existing compatibility schema as the first versioned baseline."""
    if any(not table.replace('_', '').isalnum() for table in tables):
        raise ValueError('Invalid baseline table')
    migrate(path, [['SELECT 1 FROM "' + table + '" LIMIT 0' for table in tables]], component=component)
