"""Standalone stdlib worker. No Core imports, tokens, databases or host mounts."""
import importlib.util
import json
import os
import runpy
import sys
import types
import time
import uuid
from pathlib import Path


class ApprovalRequiredException(Exception):
    def __init__(self, action, details):
        self.operation = {'action': action, 'details': details}


def execute(config, artifact='/skill/main.py', workspace='/app/data/public'):
    if artifact=='/skill/main.py' and Path('/skill/app/main.py').is_file():
        artifact = '/skill/app/main.py'
        sys.path.insert(0,'/skill/app')
    os.environ['KHAI_SKILL_DATA'] = str(Path(workspace)/'data')
    os.environ['KHAI_SKILL_OTHER'] = '/skill/other'
    if os.name == 'posix':
        import resource
        resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024*1024,16*1024*1024))
        resource.setrlimit(resource.RLIMIT_NOFILE,(128,128))
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
        resource.setrlimit(resource.RLIMIT_NPROC,(128,128))
        cpu_limit = 1800 if config.get('mode')=='AGENT' else 120
        resource.setrlimit(resource.RLIMIT_CPU,(cpu_limit,cpu_limit))
    gateway = types.ModuleType('khai_gateway')
    def request(service, operation, params=None):
        request_id = uuid.uuid4().hex
        base = Path(workspace)/('.voice-'+request_id)
        incoming = Path(str(base)+'.request.json')
        outgoing = Path(str(base)+'.response.json')
        raw = json.dumps({'service':service,'operation':operation,'params':params or {}})
        if len(raw.encode())>16384:
            raise ValueError('Gateway request too large')
        tmp = Path(str(base)+'.tmp')
        tmp.write_text(raw,encoding='utf-8')
        tmp.rename(incoming)
        deadline = min(config['deadline'],time.time()+45)
        while time.time()<deadline:
            if outgoing.exists():
                try:
                    reply = json.loads(outgoing.read_text(encoding='utf-8'))
                except json.JSONDecodeError:
                    time.sleep(.1)
                    continue
                if 'error' in reply:
                    raise RuntimeError('Voice rejected request: '+reply['error'])
                return reply['result']
            time.sleep(.1)
        raise TimeoutError('Voice gateway timed out')
    gateway.request = request
    sys.modules['khai_gateway'] = gateway
    checkpoints = Path(workspace) / '.checkpoints.json'
    class State:
        def read(self):
            return json.loads(checkpoints.read_text()) if checkpoints.exists() else {}
        def is_step_completed(self, task_id, name):
            return name in self.read()
        def get_step_data(self, task_id, name):
            return self.read().get(name)
        def save_step(self, task_id, name, result):
            value = self.read()
            value[name] = result
            tmp = checkpoints.with_suffix('.tmp')
            tmp.write_text(json.dumps(value))
            os.replace(tmp, checkpoints)
    class Telegram:
        def send_message(self, text, **kwargs):
            print(text)
    class BaseSkill:
        def __init__(self, task_id, state_manager, telegram):
            self.task_id, self.state, self.telegram = task_id, state_manager, telegram
        def run_step(self, name, fn, *args, **kwargs):
            if self.state.is_step_completed(self.task_id, name):
                return self.state.get_step_data(self.task_id, name)
            result = fn(*args, **kwargs)
            self.state.save_step(self.task_id, name, result)
            return result
        def require_approval(self, action_type, details):
            operation = {'action': action_type, 'details': details}
            if operation not in config.get('grants', []):
                raise ApprovalRequiredException(action_type, details)
    sys.argv = [artifact, config['request']]
    try:
        if config['mode'] == 'GOD':
            package = types.ModuleType('modules')
            package.__path__ = []
            api = types.ModuleType('modules.skill_forge')
            api.BaseSkill = BaseSkill
            api.ApprovalRequiredException = ApprovalRequiredException
            sys.modules['modules'] = package
            sys.modules['modules.skill_forge'] = api
            app_package = types.ModuleType('app')
            app_package.__path__ = []
            hand_package = types.ModuleType('app.hand')
            hand_package.__path__ = []
            sys.modules['app'] = app_package
            sys.modules['app.hand'] = hand_package
            sys.modules['app.hand.forge'] = api
            spec = importlib.util.spec_from_file_location('dynamic_skill', artifact)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            print(module.DynamicSkill(config['task_id'], State(), Telegram()).execute(config['request']))
        else:
            runpy.run_path(artifact, run_name='__main__')
    except ApprovalRequiredException as exc:
        print('KHAI_APPROVAL:' + json.dumps(exc.operation))
        return 75
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(execute(json.loads(Path('/skill/input.json').read_text())))
