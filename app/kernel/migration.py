"""Offline, restartable split of the legacy DB. Originals are never deleted."""
import json
import os
import sqlite3
import time
from pathlib import Path
from app.kernel.database import backup, connection, migrate

DOMAINS = {
    'mind/memory.sqlite3': {'preferences', 'long_term_events', 'hypotheses', 'hypothesis_contexts'},
    'pixy/logs.sqlite3': {'tasks', 'steps', 'audits', 'control_tasks', 'control_events', 'control_commands'},
    'pixy/rules.sqlite3': {'dynamic_guidelines', 'legacy_dynamic_guidelines', 'rule_proposals', 'rule_uses'},
    'dreams/dreams.sqlite3': {'control_feedback'},
    'voice/voice.sqlite3': {'provider_status'},
    'heart/heart.sqlite3': set(),
    'hand/hand.sqlite3': {'skills_library'},
    'senses/senses.sqlite3': set(),
}


def migrate_data(root):
    root = Path(root).resolve()
    # Runtime technical receipts are owned by Voice; preserve exact execution IDs.
    old_runtime, new_runtime = root / 'proxy.sqlite3', root / 'voice' / 'docker-runtime.sqlite3'
    if old_runtime.exists() and not new_runtime.exists():
        backup(old_runtime, new_runtime)
    marker = root / 'architecture-v1.json'
    if marker.exists():
        return json.loads(marker.read_text())
    legacy = Path(os.getenv('KHAI_DB_PATH') or root / 'openclaw.db')
    snapshot = root / 'backups' / 'before-domain-split.sqlite3'
    if legacy.exists() and not snapshot.exists():
        backup(legacy, snapshot)
    for relative, owned in DOMAINS.items():
        target = root / relative
        with connection(target):
            pass
        if not snapshot.exists():
            continue
        src = sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            tables = dict(src.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            with connection(target) as dst:
                dst.execute('CREATE TABLE IF NOT EXISTS legacy_imports(source TEXT PRIMARY KEY)')
                if dst.execute('SELECT 1 FROM legacy_imports WHERE source=?', (str(snapshot),)).fetchone():
                    continue
                for name in sorted(owned & tables.keys()):
                    dst.execute(tables[name].replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1))
                    rows = src.execute('SELECT * FROM "' + name + '"').fetchall()
                    if rows:
                        dst.executemany('INSERT OR IGNORE INTO "' + name + '" VALUES (' + ','.join('?' * len(rows[0])) + ')', rows)
                    if name == 'control_tasks':
                        for row in dst.execute('SELECT id,document FROM control_tasks').fetchall():
                            task = json.loads(row['document'])
                            if task.get('state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'EXHAUSTED', 'UNKNOWN', 'UNVERIFIED'}:
                                task.update(state='UNKNOWN', error='Migrated legacy task: inspect before any new execution')
                                dst.execute('UPDATE control_tasks SET document=? WHERE id=?', (json.dumps(task), row['id']))
                dst.execute('INSERT INTO legacy_imports VALUES (?)', (str(snapshot),))
        finally:
            src.close()
    result = {'version': 1, 'created_at': time.time(), 'legacy_backup': str(snapshot), 'domains': list(DOMAINS)}
    temp = marker.with_suffix('.tmp')
    temp.write_text(json.dumps(result, indent=2), encoding='utf-8')
    temp.replace(marker)
    return result
