from pathlib import Path
import yaml


class Identity:
    def __init__(self, path, legacy=None):
        self.path = Path(path)
        if not self.path.exists():
            if not legacy or not Path(legacy).is_file():
                raise RuntimeError('Identity is missing; supply identity.yaml or the existing agent.md')
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(yaml.safe_dump({'name': 'Khai', 'content': Path(legacy).read_text(encoding='utf-8')},
                                               allow_unicode=True, sort_keys=False), encoding='utf-8')

    def read(self):
        value = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('content'), str):
            raise ValueError('Invalid identity.yaml')
        return value['content']
