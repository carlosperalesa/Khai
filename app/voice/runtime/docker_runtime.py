import requests
from app.voice.runtime.runtime import Runtime
from app.voice.runtime.contract import proxy_url, proxy_headers


class DockerRuntime(Runtime):
    def prepare(self, payload):
        if payload.get('network'):
            raise PermissionError('Skills must use Voice Gateway')
        return dict(payload,network=False)

    def execute(self, payload):
        response = requests.post(proxy_url('executions'),json=self.prepare(payload),headers=proxy_headers(),timeout=10)
        response.raise_for_status()
        return response.json()

    def status(self, execution_id):
        response = requests.get(proxy_url('executions/'+execution_id),headers=proxy_headers(),timeout=10)
        response.raise_for_status()
        return response.json()

    def cancel(self, execution_id):
        response = requests.post(proxy_url('executions/'+execution_id+'/cancel'),headers=proxy_headers(),timeout=10)
        response.raise_for_status()
        return response.json()

    def collect_result(self, execution_id):
        return self.status(execution_id)

    def cleanup(self, execution_id):
        result = self.status(execution_id)
        if result['status'] in {'queued','starting','running','started'}:
            return self.cancel(execution_id)
        return result  # Engine persists receipts before removing containers.

    @property
    def capabilities(self):
        from app.voice.runtime.contract import default_runtime_capabilities
        return default_runtime_capabilities('docker')

