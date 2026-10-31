"""Bounded document extraction inside the worker, never inside the Core."""
import csv
import io
import os
import stat
import zipfile
from pathlib import Path

DEPENDENCIES = ['pypdf==6.21.4', 'openpyxl==3.1.5', 'defusedxml==0.7.1']
MAX_FILE = 8 * 1024 * 1024
MAX_TEXT = 16000


def read_document(relative_path, root=None):
    root = Path(root or os.environ['KHAI_SKILL_DATA']).resolve()
    relative = Path(relative_path)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('Document path must be relative to skill data')
    path = root / relative
    for parent in [path, *path.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError('Document links are forbidden')
    if not path.resolve().is_relative_to(root):
        raise ValueError('Document escapes skill data')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError('Document must be a regular file of at most 8 MiB')
    with path.open('rb') as stream:
        raw = stream.read(MAX_FILE + 1)
    if len(raw) > MAX_FILE:
        raise ValueError('Document exceeds size limit')
    suffix = path.suffix.lower()
    parts, truncated = [], False
    if suffix == '.csv':
        text = raw.decode('utf-8-sig')
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
        except csv.Error:
            dialect = csv.excel
        for index, row in enumerate(csv.reader(io.StringIO(text), dialect)):
            if index >= 500 or sum(map(len, parts)) >= MAX_TEXT:
                truncated = True
                break
            parts.append('\t'.join(cell[:1000] for cell in row[:50]))
            truncated = truncated or len(row) > 50
    elif suffix == '.pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted:
            raise ValueError('Encrypted PDFs are unsupported')
        count = len(reader.pages)
        truncated = count > 30
        for page in reader.pages[:30]:
            if sum(map(len, parts)) >= MAX_TEXT:
                truncated = True
                break
            parts.append(page.extract_text() or '')
    elif suffix == '.xlsx':
        import defusedxml  # Required before openpyxl loads any XML.
        from openpyxl import load_workbook
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if (len(entries) > 2000 or sum(e.file_size for e in entries) > 32 * 1024 * 1024
                    or any(e.file_size > 8 * 1024 * 1024 for e in entries)):
                raise ValueError('Expanded workbook exceeds limits')
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
        try:
            truncated = len(workbook.worksheets) > 10
            for sheet in workbook.worksheets[:10]:
                parts.append('Sheet: ' + sheet.title)
                truncated = truncated or sheet.max_row > 500 or sheet.max_column > 50
                for row in sheet.iter_rows(max_row=min(sheet.max_row, 500), max_col=min(sheet.max_column, 50), values_only=True):
                    if sum(map(len, parts)) >= MAX_TEXT:
                        truncated = True
                        break
                    parts.append('\t'.join('' if v is None else str(v)[:1000] for v in row))
                if sum(map(len, parts)) >= MAX_TEXT:
                    break
        finally:
            workbook.close()
    else:
        raise ValueError('Supported documents: PDF, UTF-8 CSV and XLSX')
    text = '\n'.join(parts)
    return {'path': relative.as_posix(), 'format': suffix[1:], 'text': text[:MAX_TEXT],
            'truncated': truncated or len(text) > MAX_TEXT, 'untrusted': True,
            'needs_ocr': suffix == '.pdf' and not text.strip()}
