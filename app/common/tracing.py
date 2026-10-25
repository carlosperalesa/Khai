"""Distributed Tracing and Correlation Context across Khai modules."""
import contextlib
import contextvars
import logging
import re
import secrets
import time
from typing import Optional, List, Dict, Any

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar('current_trace_id', default='')

BOOT_TIME: float = time.time()
BOOT_ID: str = secrets.token_hex(4)

LOG_ERROR_RE = re.compile(r'\b(ERROR|CRITICAL)\b|Traceback \(most recent call last\)')
LOG_TIMESTAMP_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d+)?)')
LOG_TRACE_RE = re.compile(r'\[((?:req|trace|tr|task|case)-[a-zA-Z0-9_-]+)\]')


def new_trace_id(prefix: str = 'req') -> str:
    timestamp = time.strftime('%Y%m%d%H%M%S')
    random_part = secrets.token_hex(3)
    return f"{prefix}-{timestamp}-{random_part}"


def get_trace_id() -> str:
    return _current_trace_id.get() or ''


def set_trace_id(trace_id: str) -> contextvars.Token:
    return _current_trace_id.set(str(trace_id))


def reset_trace_id(token: contextvars.Token) -> None:
    _current_trace_id.reset(token)


@contextlib.contextmanager
def trace_scope(trace_id: Optional[str] = None):
    tid = trace_id or new_trace_id()
    token = set_trace_id(tid)
    try:
        yield tid
    finally:
        reset_trace_id(token)


class TracingFilter(logging.Filter):
    """Logging filter that injects the active trace_id into every log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        tid = get_trace_id()
        record.trace_id = f"[{tid}]" if tid else "[-]"
        return True


def get_boot_epoch() -> Dict[str, Any]:
    return {'boot_id': BOOT_ID, 'boot_time': BOOT_TIME}


def parse_log_errors(text: str, boot_time: Optional[float] = None, target_trace_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Parse log entries, grouping multiline tracebacks and extracting timestamps/trace_ids.
    Ignores entries logged before boot_time if provided.
    """
    incidents = []
    current_incident = None
    boot_threshold = boot_time if boot_time is not None else 0.0

    lines = text.splitlines() if text else []
    for line in lines:
        ts_match = LOG_TIMESTAMP_RE.match(line)
        trace_match = LOG_TRACE_RE.search(line)
        line_trace = trace_match.group(1) if trace_match else None

        line_time = None
        if ts_match:
            raw_ts = ts_match.group(1).split(',')[0]
            try:
                line_time = time.mktime(time.strptime(raw_ts, '%Y-%m-%d %H:%M:%S'))
            except Exception:
                line_time = None

        has_timestamp = bool(ts_match)
        is_error = bool(LOG_ERROR_RE.search(line))

        if has_timestamp and is_error:
            is_active = True
            if line_time is not None and boot_threshold > 0:
                is_active = line_time >= (boot_threshold - 2.0)
            if target_trace_id and line_trace and target_trace_id != line_trace:
                is_active = False

            current_incident = {
                'timestamp': line_time or time.time(),
                'trace_id': line_trace,
                'is_active': is_active,
                'summary': line[-300:],
                'lines': [line],
                'raw': line,
            }
            incidents.append(current_incident)
        elif has_timestamp and not is_error:
            current_incident = None
        else:
            if current_incident:
                current_incident['lines'].append(line)
                current_incident['raw'] += '\n' + line
            elif is_error:
                current_incident = {
                    'timestamp': time.time(),
                    'trace_id': line_trace,
                    'is_active': True,
                    'summary': line[-300:],
                    'lines': [line],
                    'raw': line,
                }
                incidents.append(current_incident)

    return incidents
