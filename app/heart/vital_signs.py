from pathlib import Path
import psutil


class VitalSigns:
    def __init__(self, root):
        self.root = Path(root)

    def sample(self):
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(str(self.root.resolve().anchor))
        return {'ram_percent':memory.percent,'disk_percent':disk.percent,'disk_free':disk.free,
                'status':'critical' if memory.percent>=90 or disk.free<1024**3 else 'ok'}
