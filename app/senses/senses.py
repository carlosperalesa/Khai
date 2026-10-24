"""Read-only observations through registered adapters; never shell or Docker socket."""
import json
import time
from app.common.database import connection, migrate


OBSERVERS = ('voice','mind','brain','memory','dreams','heart','hand','pixy','runtime',
             'docker','network','storage','database','webhook','security')


class Senses:
    def __init__(self, path, adapters, legacy_gateway=None):
        self.path, self.adapters, self.legacy_gateway = path,dict(adapters),legacy_gateway
        migrate(path, [['CREATE TABLE IF NOT EXISTS observations(id INTEGER PRIMARY KEY, observer TEXT, created_at REAL, snapshot TEXT)']])

    def listen(self, name):
        if name not in OBSERVERS or name not in self.adapters:
            raise ValueError('Unknown observer')
        try:
            snapshot = self.adapters[name]()
        except Exception as exc:
            snapshot = {'status':'unavailable','error':type(exc).__name__}
        with connection(self.path) as conn:
            conn.execute('INSERT INTO observations(observer,created_at,snapshot) VALUES (?,?,?)',
                         (name,time.time(),json.dumps(snapshot)))
        return snapshot

    def __getattr__(self, name):
        if name.startswith('listen_') and name[7:] in OBSERVERS:
            return lambda: self.listen(name[7:])
        raise AttributeError(name)

    def observe(self, resource_id, reason='User request'):
        if resource_id.startswith('listen_'):
            return json.dumps(self.listen(resource_id[7:]),ensure_ascii=False)
        if self.legacy_gateway:
            return self.legacy_gateway.observe(resource_id,reason)
        raise ValueError('Unknown observation resource')

    def observe_multiple(self, resource_ids, reason='User request'):
        return {rid:self.observe(rid,reason) for rid in resource_ids}
