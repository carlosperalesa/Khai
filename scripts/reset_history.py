"""Offline, selective reset. No transcripts or execution receipts survive the reset.

Run --dry-run first; --apply requires the exact inventoried root and stopped writers.
The retained schema comes from the application's versioned migrations; migration
ledgers are preserved so each owner applies subsequent migrations at startup.
"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
import tempfile

KEEP = {'component_migrations', 'schema_migrations', 'preferences', 'versions', 'skill_specs',
        'skill_origins', 'pixy_skill_approvals', 'pixy_audits', 'service_descriptors',
        'rules', 'dynamic_guidelines', 'hypotheses', 'hypothesis_contexts', 'skills_library'}
HISTORY = {'control_tasks', 'tasks', 'steps', 'conversation_messages', 'conversations',
           'pixy_authorizations', 'voice_outbox', 'voice_inbox', 'recovery_cases',
           'gateway_receipts', 'observations', 'task_observations', 'journal_cycles'}


def checked_root(value):
    raw = Path(value).absolute()
    root = raw.resolve(strict=True)
    if root != raw or root.name != 'data' or root.parent == root:
        raise ValueError('Expected a canonical project data directory without links')
    for p in root.rglob('*'):
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError('Linked path in data directory: ' + str(p.relative_to(root)))
    return root


def schema_reset(source, target):
    with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as old, \
            closing(sqlite3.connect(target)) as new:
        old.row_factory = sqlite3.Row
        schema = old.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'").fetchall()
        virtual = [r for r in schema if r['type'] == 'table' and 'CREATE VIRTUAL TABLE' in r['sql'].upper()]
        shadows = {r['name'] + suffix for r in virtual for suffix in ('_data', '_idx', '_content', '_docsize', '_config')}
        tables = [r for r in schema if r['type'] == 'table' and r not in virtual and r['name'] not in shadows]
        for row in tables:
            new.execute(row['sql'])
        new.execute('CREATE TABLE IF NOT EXISTS retained_learning_stats(kind TEXT, document TEXT NOT NULL)')
        for row in tables:
            name = row['name']
            if name not in KEEP:
                continue
            for record in old.execute('SELECT * FROM "' + name + '"'):
                d = dict(record)
                if name == 'preferences':
                    if any(x in d['key'].lower() for x in ('pending', 'nonce', 'transition', 'authorization', 'last_update', 'last_message')):
                        continue
                    value = json.loads(d['value_json'])
                    if isinstance(value, dict) and d['key'].startswith('default_provider_'):
                        value['source'] = 'retained-approved-preference'
                        d['value_json'] = json.dumps(value, ensure_ascii=False)
                elif name == 'skill_specs':
                    if d['status'] not in {'active', 'superseded'}:
                        continue
                    d['verification_run_id'] = None
                elif name == 'versions':
                    if d['status'] not in {'active', 'archived'}:
                        continue
                elif name == 'pixy_skill_approvals':
                    d['task_id'] = 'retained-artifact-approval'
                elif name in {'hypotheses', 'hypothesis_contexts'}:
                    stats = {k: d[k] for k in ('topic', 'hypothesis_id', 'activations', 'successes', 'failures', 'confidence') if k in d}
                    new.execute('INSERT INTO retained_learning_stats VALUES (?,?)', (name, json.dumps(stats)))
                    for key in ('activations', 'successes', 'failures'):
                        if key in d:
                            d[key] = 0
                    if name == 'hypotheses' and d.get('status') == 'evaluating':
                        d['confidence'] = 0.5
                new.execute('INSERT INTO "' + name + '" VALUES (' + ','.join('?' for _ in d) + ')', tuple(d.values()))
        names = {r['name'] for r in tables}
        if 'episode_notes' in names:
            for row in old.execute('SELECT outcome,stage,COUNT(*) count FROM episode_notes GROUP BY outcome,stage'):
                new.execute('INSERT INTO retained_learning_stats VALUES (?,?)', ('episodes', json.dumps(dict(row))))
        # Drop dangling catalog metadata, never replace old receipts with invented runs.
        if 'versions' in names:
            for name in ('skill_specs', 'skill_origins', 'pixy_skill_approvals'):
                if name in names:
                    new.execute('DELETE FROM "' + name + '" WHERE version_id NOT IN (SELECT version_id FROM versions)')
        for row in virtual:
            new.execute(row['sql'])
        for row in schema:
            if row['type'] in {'index', 'trigger', 'view'} and row['name'] not in shadows:
                new.execute(row['sql'])
        new.commit()
        if new.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or new.execute('PRAGMA foreign_key_check').fetchall():
            raise RuntimeError('Reset database integrity failed')
        counts = {name: new.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in names if name in HISTORY}
        if any(counts.values()):
            raise RuntimeError('Historical state survived reset')
        return counts


def reset(root, apply=False, validate=False):
    root = checked_root(root)
    external_backups = root.parent/'backups'
    if external_backups.exists():
        if external_backups.is_symlink() or external_backups.resolve().parent != root.parent:
            raise ValueError('Unsafe project backup directory')
        for path in external_backups.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(external_backups):
                raise ValueError('Linked project backup entry')
    dbs = [p for p in root.rglob('*') if p.suffix in {'.db', '.sqlite3'} and 'backups' not in p.relative_to(root).parts]
    control = root / 'control.sqlite3'
    if not control.is_file():
        raise ValueError('Control database is missing')
    with closing(sqlite3.connect(control.as_uri() + '?mode=ro', uri=True)) as conn:
        retained = conn.execute("SELECT skill_name,artifact_hash FROM versions WHERE status='active'").fetchall()
    preserve = {root/'voice/secrets.env', root/'mind/identity.yaml', root/'memory/agent.md', root/'mind/agent.md'}
    protected_roots = [root/'certs', root/'voice/dependencies']
    for name, expected in retained:
        folder = root/'hand'/name
        if folder.parent != root/'hand' or not folder.is_dir():
            raise ValueError('Unsafe or missing retained skill')
        from app.voice.runtime.contract import bundle_hash
        if bundle_hash(folder) != expected:
            raise ValueError('Retained skill hash mismatch: ' + name)
        protected_roots.append(folder)
    # Version archives are part of skills, not database backups.
    preserve.update((root/'hand').rglob('*.zip'))
    for p in root.rglob('*'):
        if p.is_file() and any(p.is_relative_to(base) for base in protected_roots):
            preserve.add(p)
    files = [p for p in root.rglob('*') if p.is_file()]
    report = {'root': str(root), 'preserved_files': len(preserve), 'removed_files': len([p for p in files if p not in preserve]),
              'databases': len(dbs), 'skills': len(retained), 'external_backups': external_backups.exists(), 'applied': False}
    if not apply and validate:
        with tempfile.TemporaryDirectory(prefix='khai-reset-validation-') as folder:
            for i, db in enumerate(dbs):
                schema_reset(db, Path(folder)/(str(i)+'.sqlite3'))
        report['validated'] = True
        return report
    if not apply:
        return report
    from app.pixy.control import InstanceLock
    lock = InstanceLock(root/'coordinator.lock')
    lock.acquire()
    prepared = []
    try:
        for db in dbs:
            next_db = db.with_name(db.name + '.reset-new')
            if next_db.exists():
                raise RuntimeError('Unfinished reset staging file; inspect before proceeding')
            schema_reset(db, next_db)
            prepared.append((db, next_db))
        for db, new in prepared:
            for suffix in ('-wal', '-shm', '-journal'):
                Path(str(db) + suffix).unlink(missing_ok=True)
            new.replace(db)
            preserve.add(db)
        with closing(sqlite3.connect(control)) as conn:
            conn.execute('INSERT OR REPLACE INTO preferences(key,value_json) VALUES (?,?)',
                         ('history_reset_cutoff', json.dumps(time.time())))
            conn.commit()
        for path in sorted(files, key=lambda p: len(p.parts), reverse=True):
            if path not in preserve and path.name != 'coordinator.lock':
                path.unlink(missing_ok=True)
        for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if external_backups.is_dir():
            shutil.rmtree(external_backups)
        report['applied'] = True
        return report
    finally:
        lock.release()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--data-dir', required=True)
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--dry-run', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--validate', action='store_true')
    args = p.parse_args()
    print(json.dumps(reset(args.data_dir, args.apply, args.validate), sort_keys=True))


if __name__ == '__main__':
    main()
