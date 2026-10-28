from abc import ABC, abstractmethod


class Runtime(ABC):
    @abstractmethod
    def prepare(self, payload): ...
    @abstractmethod
    def execute(self, payload): ...
    @abstractmethod
    def cancel(self, execution_id): ...
    @abstractmethod
    def status(self, execution_id): ...
    @abstractmethod
    def collect_result(self, execution_id): ...
    @abstractmethod
    def cleanup(self, execution_id): ...

    def submit(self, payload):
        return self.execute(self.prepare(payload))

    def get(self, execution_id):
        return self.status(execution_id)

    @property
    def capabilities(self):
        from app.voice.runtime.contract import default_runtime_capabilities
        return default_runtime_capabilities('docker')

