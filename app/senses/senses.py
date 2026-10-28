"""Read-only observations through registered adapters; never shell or Docker socket."""
import json
import time
from app.kernel.database import connection, migrate


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

    def _record(self, observer, snapshot):
        with connection(self.path) as conn:
            conn.execute('INSERT INTO observations(observer,created_at,snapshot) VALUES (?,?,?)',
                         (observer,time.time(),json.dumps(snapshot,ensure_ascii=False)))

    def _resource(self, resource_id, reason):
        """Read one whitelisted resource; failures are evidence, never exceptions."""
        try:
            raw = self.observe(resource_id,reason)
        except Exception as exc:
            return {'status':'unavailable','error':type(exc).__name__}
        if isinstance(raw,str) and (raw.startswith('❌') or '[Error]' in raw[:40]):
            return {'status':'unavailable','error':raw[:500]}
        try:
            value = json.loads(raw)
            return value if isinstance(value,dict) else {'status':'ok','value':value}
        except (TypeError,ValueError):
            return {'status':'ok','text':str(raw)[-4000:]}

    def sample(self, resource_ids, reason='Observation request'):
        """Collect only the specific telemetry resources requested by the caller (e.g. Heart).
        Senses never determines the diagnostic scope; it only serves as the sensor adapter."""
        results = {}
        for rid in resource_ids:
            if rid in OBSERVERS and rid in self.adapters:
                results[rid] = self.listen(rid)
            else:
                results[rid] = self._resource(rid, reason)
        self._record('sample', {'reason': reason, 'resources': resource_ids, 'results': results})
        return results

    def evidence(self, reason, phase='before', resources=None):
        """Compatibility wrapper: calls sample with caller-provided resources or basal profile."""
        target_resources = resources or ['storage', 'core_logs', 'container_state_core', 'container_state_proxy']
        readings = self.sample(target_resources, reason)
        readings['phase'] = phase
        readings['collected_at'] = time.time()
        # Aliases for consumers expecting container_core / container_proxy keys
        if 'container_state_core' in readings and 'container_core' not in readings:
            readings['container_core'] = readings['container_state_core']
        if 'container_state_proxy' in readings and 'container_proxy' not in readings:
            readings['container_proxy'] = readings['container_state_proxy']
        return readings
