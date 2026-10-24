"""Consistent offline backups; restoration always writes to a new directory."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
from app.common.database import backup
from app.pixy.control import InstanceLock


def snapshot(root, output):
    root, output = Path(root).resolve(),Path(output).resolve()
    if output.exists():
        raise ValueError('Backup destination already exists')
    if output.is_relative_to(root) and not output.is_relative_to(root/'backups'):
        raise ValueError('Nested snapshots must be under data/backups')
    lock = InstanceLock(root/'coordinator.lock')
    try:
        lock.acquire()
        output.mkdir(parents=True)
        files = []
        for path in root.rglob('*'):
            relative = path.relative_to(root)
            if relative.parts[0]=='backups' or path.is_symlink() or not path.is_file():
                continue
            if path.name.endswith(('-wal','-shm','.lock')):
                continue
            target = output/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            if path.suffix in {'.db','.sqlite3'}:
                backup(path,target)
            else:
                shutil.copy2(path,target)
            files.append(relative.as_posix())
        (output/'backup-manifest.json').write_text(json.dumps({'files':files},indent=2),encoding='utf-8')
        return output
    finally:
        lock.release()


def restore(source, destination):
    source,destination = Path(source).resolve(),Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source):
        raise ValueError('Restore requires a new destination outside the backup')
    manifest = json.loads((source/'backup-manifest.json').read_text(encoding='utf-8'))
    for relative in manifest['files']:
        path = source/relative
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('Unsafe backup entry')
        target = destination/relative
        if not target.resolve().is_relative_to(destination):
            raise ValueError('Unsafe restore path')
    destination.mkdir(parents=True)
    for relative in manifest['files']:
        path,target = source/relative,destination/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        if path.suffix in {'.db','.sqlite3'}:
            backup(path,target)
        else:
            shutil.copy2(path,target)
    return destination


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('--data-dir',default='data')
    parser.add_argument('--source')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.action=='restore':
        if not args.source or not args.output:
            parser.error('restore requires --source and --output')
        result = restore(args.source,args.output)
    else:
        output = args.output or str(Path(args.data_dir)/'backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        result = snapshot(args.data_dir,output)
    print(result)


if __name__=='__main__':
    main()
