"""Read worker-created files without following leaf symlinks or blocking on FIFOs."""
import os
import stat


def read_regular(path, limit):
    if path.is_symlink():
        raise PermissionError('Symlink denied')
    flags = os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_BINARY',0)
    fd = os.open(path,flags)
    with os.fdopen(fd,'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError('Not a bounded regular file')
        raw = stream.read(limit+1)
        if len(raw)>limit:
            raise ValueError('File exceeds limit')
        return raw
