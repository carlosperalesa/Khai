"""Owner-scoped Telegram history maintenance, executed before offline state reset."""
import json
import sqlite3
import time
import requests
from pathlib import Path


def candidates(db, owner):
    owner = str(owner)
    conn = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    items = {}
    try:
        for r in conn.execute('''SELECT c.chat_id,t.message_id,m.created_at FROM conversation_turns t
                JOIN conversations c ON c.id=t.conversation_id
                JOIN conversation_messages m ON m.source=t.source
                WHERE c.owner_id=? AND c.chat_id=? AND t.message_id IS NOT NULL''', (owner, owner)):
            items[(owner, r['message_id'])] = r['created_at']
        for r in conn.execute('SELECT metadata,message_ids,sent_at,created_at FROM voice_outbox'):
            metadata = json.loads(r['metadata'])
            if str(metadata.get('chat_id') or owner) != owner:
                continue
            for mid in json.loads(r['message_ids']):
                items[(owner, mid)] = r['sent_at'] or r['created_at']
    finally:
        conn.close()
    return items


def delete_history(telegram, items, cutoff, *, send=None, sleep=time.sleep):
    """Batch acceptance is not an individual deletion receipt; never claim it is."""
    send = send or requests.post
    eligible = {}
    report = dict(identified=len(items), outside_window=0, unknown_timestamp=0, excluded_scope=0, invalid_identifiers=0, requested=0,
                  batches_accepted=0, batches_rejected=0, errors=[])
    for (chat, mid), timestamp in items.items():
        if str(chat) != str(telegram.user_id):
            report['excluded_scope'] += 1
        elif type(mid) is not int or mid <= 0:
            report['invalid_identifiers'] += 1
        elif timestamp is None:
            report['unknown_timestamp'] += 1
        elif not 0 <= cutoff - timestamp < 48 * 3600:
            report['outside_window'] += 1
        elif str(chat) == str(telegram.user_id):
            eligible.setdefault(str(chat), set()).add(mid)
    for chat, ids in eligible.items():
        ids = sorted(ids)
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            report['requested'] += len(batch)
            result = {}
            for attempt in range(3):
                try:
                    response = send(telegram.base_url + '/deleteMessages',
                                    json={'chat_id': chat, 'message_ids': batch}, timeout=30)
                    result = response.json()
                    wait = result.get('parameters', {}).get('retry_after')
                    if result.get('error_code') == 429 and type(wait) is int and 0 < wait <= 30 and attempt < 2:
                        sleep(wait)
                        continue
                except (requests.RequestException, ValueError):
                    result = {'error_code': 'transport_uncertain'}
                break
            if result.get('ok') is True:
                report['batches_accepted'] += 1
            else:
                report['batches_rejected'] += 1
                report['errors'].append(result.get('error_code', 'invalid_response'))
    report['individual_deletions_verified'] = False
    return report
