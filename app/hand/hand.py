import ast
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from app.kernel.database import connection, migrate
from app.heart.catalog import FINAL_STEP, canonical, plan_hash, validate_case_id, validate_plan
from app.pixy.control import InstanceLock
from app.pixy.control import BudgetExceeded
from app.voice.runtime.contract import digest, skill_metadata, default_runtime_capabilities
from app.hand.catalog import SkillCatalog
from app.hand.skill_spec import normalize_schema, validate_skill_spec
from app.hand.agent_loop import AgentLoop, AgentExhausted
from app.voice.gateway.service_registry import validate_descriptor

RECOVERY_STEP_TIMEOUT = 300


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class Hand:
    def __init__(self, root, pixy, events=None, recovery_executor=None, llm=None,
                 db_path=None, authorization_port=None, service_proposals=None, capabilities=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = Path(db_path).resolve() if db_path else self.root/'hand.sqlite3'
        self.pixy, self.events, self.llm = pixy, events, llm
        self.authorization_port = authorization_port
        self.service_proposals = service_proposals
        self.recovery_executor = recovery_executor
        self.capabilities = capabilities
        self.recovery_root = self.root/'heart'
        self._mutex = threading.RLock()
        migrate(self.db, [[
            '''CREATE TABLE IF NOT EXISTS versions(version_id TEXT PRIMARY KEY, skill_name TEXT NOT NULL,
               version_number TEXT NOT NULL, status TEXT NOT NULL, artifact_hash TEXT, archive_name TEXT,
               archive_hash TEXT, archive_size INTEGER, created_at REAL, activated_at REAL, archived_at REAL,
               previous_version_id TEXT, pixy_audit_id TEXT, restored_from_version_id TEXT,
               manifest TEXT NOT NULL, UNIQUE(skill_name,version_number))''',
            '''CREATE TABLE IF NOT EXISTS operations(operation_id TEXT PRIMARY KEY, operation_type TEXT,
               skill_name TEXT, source_version_id TEXT, target_version_id TEXT, status TEXT,
               correlation_id TEXT, reason TEXT, created_at REAL, completed_at REAL, error TEXT)''',
            'CREATE UNIQUE INDEX IF NOT EXISTS active_skill ON versions(skill_name) WHERE status="active"',
            '''CREATE TRIGGER IF NOT EXISTS operations_no_delete BEFORE DELETE ON operations
               BEGIN SELECT RAISE(ABORT,'immutable operation history'); END''',
            '''CREATE TRIGGER IF NOT EXISTS operations_completed BEFORE UPDATE ON operations
               WHEN OLD.completed_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'completed operation is immutable'); END''',
        ]], component='hand-library')
        migrate(self.db, [[
            '''CREATE TABLE IF NOT EXISTS recovery_skills(case_id TEXT PRIMARY KEY, authorization_id TEXT NOT NULL,
               plan_hash TEXT NOT NULL, skill_hash TEXT NOT NULL, status TEXT NOT NULL,
               created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS recovery_steps(case_id TEXT NOT NULL, step_index INTEGER NOT NULL,
               action TEXT NOT NULL, target TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
               receipt TEXT, submitted_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(case_id,step_index))''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_skills_no_delete BEFORE DELETE ON recovery_skills
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
            '''CREATE TRIGGER IF NOT EXISTS recovery_steps_no_delete BEFORE DELETE ON recovery_steps
               BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END''',
        ]], component='hand-recovery')
        self.catalog = SkillCatalog(self.db)
        self.agent = AgentLoop(self.db, self.llm) if self.llm is not None else None
        self.last_agent_run_id = None
        self.recover()

    def recover(self):
        """Reconcile a crash between directory rename and the SQLite activation."""
        with self.locked():
            with connection(self.db) as conn:
                rows = [dict(r) for r in conn.execute('SELECT * FROM versions WHERE status IN ("approved","active")')]
                candidates = [dict(r) for r in conn.execute(
                    'SELECT * FROM versions WHERE status IN ("draft","one_off")')]
            for v in candidates:
                draft = self.root/('.'+v['skill_name'])
                retained = self.root/('.one-off-'+v['version_id'])
                if v['status'] == 'draft' and retained.exists() and not draft.exists():
                    if self.tree_hash(retained) != v['artifact_hash']:
                        raise RuntimeError('One-off artifact changed during recovery')
                    with connection(self.db) as conn:
                        conn.execute('UPDATE versions SET status="one_off" WHERE version_id=?',
                                     (v['version_id'],))
                elif v['status'] == 'one_off' and draft.exists() and not retained.exists():
                    if self.tree_hash(draft) != v['artifact_hash']:
                        raise RuntimeError('One-off draft changed during recovery')
                    draft.rename(retained)
            for v in rows:
                folder = self.root/v['skill_name']
                draft = self.root/('.'+v['skill_name'])
                old = self.root/('.previous-'+v['version_id'])
                if v['status']=='approved':
                    if folder.exists() and self.tree_hash(folder)==v['artifact_hash'] and not draft.exists():
                        with connection(self.db) as conn:
                            conn.execute('UPDATE versions SET status="archived" WHERE skill_name=? AND status="active"', (v['skill_name'],))
                            conn.execute('UPDATE versions SET status="active",activated_at=? WHERE version_id=?',(time.time(),v['version_id']))
                            conn.execute('UPDATE operations SET status="completed",completed_at=? WHERE target_version_id=? AND completed_at IS NULL',
                                         (time.time(),v['version_id']))
                    elif not folder.exists() and old.exists() and draft.exists():
                        old.rename(folder)
                        continue
                    else:
                        continue
                if old.exists():
                    prior = self.version(v['previous_version_id'])
                    if not prior['archive_name']:
                        raise RuntimeError('Recovery requires a verified previous archive')
                    h,size = self._verify_zip(self.root/prior['archive_name'],prior['skill_name'])
                    if (h,size)!=(prior['archive_hash'],prior['archive_size']):
                        raise RuntimeError('Recovery archive has changed; old files retained')
                    self._remove(old)

    @contextmanager
    def locked(self):
        with self._mutex:
            lock = InstanceLock(self.root/'hand.lock')
            try:
                lock.acquire()
                yield
            finally:
                lock.release()

    def generate(self, task, guidelines, hypothesis=None, capabilities=None):
        if not self.llm:
            raise RuntimeError('Hand requiere un proveedor LLM para generar código')
        caps = capabilities or self.capabilities or default_runtime_capabilities()
        modes_str = ", ".join(f"# MODE: {m}" for m in caps.modes)
        gateway_mod = caps.gateway_api.get("module", "khai_gateway")
        gateway_fn = caps.gateway_api.get("function", "request")
        deps_instruction = (
            "Segunda línea opcional: # PIP: paquete==version."
            if caps.allowed_dependencies
            else "No declares dependencias externas # PIP (no permitidas en este runtime)."
        )
        public_dir = caps.filesystem.get("public_output", "/app/data/public")
        prompt = f'''Eres Hand, constructor autónomo de capacidades. Investiga cuando falte información,
usa los errores de herramientas para corregir el artefacto y termina únicamente llamando finish_code.
Escribe Python autónomo para el objetivo: {task['request']}
La primera línea debe ser una de {modes_str}.
{deps_instruction} No uses shell ni instrucciones Docker.
El entorno está aislado en contenedor; no uses subprocess, ctypes, multiprocessing ni librerías de escape del sistema.
El worker no tiene red directa (network_mode: {caps.gateway_api.get('network_mode', 'none')}). Si el manifiesto declara 'Network', usa exclusivamente
`from {gateway_mod} import {gateway_fn}` y `{gateway_fn}(service, operation, params)` con los
servicios y operaciones aprobados que se indican abajo. Nunca uses urllib.request,
requests, httpx ni sockets para salir directamente.
Cuando se reutilice la skill, los parámetros tipados estarán en
`from khai_input import params`. Diseña el código para usar esos parámetros y
validarlos; el texto original seguirá disponible en sys.argv[1].
El script debe manejar posibles errores e imprimir el resultado claro y formateado en stdout, o escribir en {public_dir}.
Si hay un contrato de acción planificada, implementa solamente esa acción. Lee sus entradas de
`khai_input.params` y emite exactamente un objeto JSON en stdout que cumpla su output_schema.
No incorpores otras acciones del objetivo compuesto dentro de esta skill.
NO uses emoticones ni emojis en las salidas impresas ni en el código.
Los servicios AGENT escuchan en 8080 dentro del contenedor; no se publican puertos.
GOD permite la API BaseSkill desde app.hand.base_skill; tampoco da permisos al host.
No uses BaseSkill en SAFE. No inventes aprobaciones o resultados verificados.
Facultades del runtime: {json.dumps(caps.to_dict(), ensure_ascii=False)}
Manifiesto: {json.dumps(task.get('manifest', {}), ensure_ascii=False)}
Servicios aprobados para este objetivo: {json.dumps(task.get('services', []), ensure_ascii=False)}
Reglas aplicables: {json.dumps(guidelines, ensure_ascii=False)}
Hipótesis a probar: {hypothesis or ''}
Resultado/rechazo anterior: {str(task.get('feedback', ''))}
Alcance confirmado por el propietario: {json.dumps((task.get('scope_revisions') or [{}])[-1], ensure_ascii=False)}
Contrato de acción planificada: {json.dumps(task.get('planned_skill_spec') or {}, ensure_ascii=False)}
Código anterior: {str(task.get('code', ''))}
Notas de episodios relevantes: {json.dumps(self._episode_context(task['request']), ensure_ascii=False)}
'''
        result, run_id = self.agent.run(task.get('id'), 'Construye la skill solicitada.', prompt, [
            {'name': 'web_search', 'description': 'Investiga documentación pública y devuelve fuentes.',
             'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                            'required': ['query'], 'additionalProperties': False},
             'handler': lambda args: self.llm.web_search(args['query'])},
            {'name': 'finish_code', 'description': 'Entrega el código Python final para validación.',
             'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}},
                            'required': ['code'], 'additionalProperties': False},
             'handler': lambda args: self._finish_code(args, caps)},
        ])
        self.last_agent_run_id = run_id
        return result['code'], result['mode'], result['dependencies']

    def _episode_context(self, query):
        memory = getattr(getattr(self, 'manager', None), 'memory', None)
        if memory is not None and hasattr(memory, 'search_episode_notes'):
            return memory.search_episode_notes(query, limit=5)
        return []

    @staticmethod
    def _finish_code(args, caps):
        code = str(args.get('code') or '').strip()
        if code.startswith('```'):
            code = '\n'.join(code.splitlines()[1:])
            if code.rstrip().endswith('```'):
                code = code.rstrip()[:-3]
        ast.parse(code)
        mode, deps = skill_metadata(code)
        caps.validate_code(mode, deps)
        return {'finished': True, 'value': {'code': code, 'mode': mode, 'dependencies': deps}}


    def snapshot(self, code):
        root = self.root.parent / 'artifacts'
        root.mkdir(parents=True, exist_ok=True)
        raw = code.encode('utf-8')
        sha_val = digest(raw)
        path = root / (sha_val + '.py')
        try:
            with path.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if digest(path.read_bytes()) != sha_val:
                raise ValueError('Artefacto existente alterado')
        return str(path.resolve()), sha_val

    def describe(self, task, name, mode, dependencies):
        """Ask for reusable metadata; failure leaves the code as a one-off artifact."""
        planned = task.get('planned_skill_spec')
        if planned:
            if mode == 'AGENT':
                raise ValueError('Una acción de receta debe terminar y entregar un resultado')
            if planned['skill_id'] != name:
                raise ValueError('Planned skill identity changed')
            spec = {key: planned[key] for key in ('skill_id', 'description', 'use_cases',
                                                  'input_schema', 'output_schema')}
            spec.update(runtime=mode, dependencies=dependencies, services=task.get('services', []),
                        verifier={'kind': 'owner_criterion'})
            return validate_skill_spec(spec)
        if not self.llm:
            return None
        services = task.get('services', [])
        template = {
            "description": "Descripción clara de la capacidad reutilizable",
            "use_cases": ["Caso de uso representativo"],
            "input_schema": {"parametro": "string"},
            "output_schema": {"resultado": "string"}
        }
        prompt = ("Describe esta capacidad reutilizable siguiendo esta plantilla JSON:\n"
                  + json.dumps(template, ensure_ascii=False) + "\n"
                  "Puedes incluir extraction_templates opcionales con slots "
                  "{campo:number}, {campo:integer}, {campo:currency}, {campo:city} o {campo:text}. "
                  "Separa parámetros de la solicitud concreta. No incluyas claves ni valores secretos. "
                  "Objetivo: " + str(task['request']) + "\n"
                  "Servicio disponible: " + json.dumps(services, ensure_ascii=False))
        try:
            raw = self.llm.generate_content(prompt, response_format={'type': 'json_object'})
            proposed = json.loads(raw) if isinstance(raw, str) else raw
            in_s = proposed.get('input_schema') or proposed.get('inputs') or {}
            out_s = proposed.get('output_schema') or proposed.get('outputs') or {'result': 'string'}
            if 'additionalProperties' not in in_s:
                in_s = normalize_schema(in_s, default_field='query')
            if 'additionalProperties' not in out_s:
                out_s = normalize_schema(out_s, default_field='result')
            desc = str(proposed.get('description') or task.get('request') or name).strip()
            if len(desc) < 10:
                desc = f'Capacidad reutilizable para {desc}.'
            use_cases = proposed.get('use_cases')
            if not isinstance(use_cases, list) or not use_cases:
                use_cases = [desc[:200]]
            spec = {'skill_id': name, 'description': desc[:1000],
                    'use_cases': [str(u).strip()[:300] for u in use_cases if str(u).strip()] or [desc[:200]],
                    'input_schema': in_s,
                    'output_schema': out_s, 'runtime': mode,
                    'dependencies': dependencies, 'services': services,
                    'verifier': {'kind': (task.get('criterion') or {}).get('kind', 'owner_criterion')}}
            if task.get('requested_capability'):
                spec['capability'] = task['requested_capability']
            if task.get('requested_provider'):
                spec['provider'] = task['requested_provider']
            spec['aliases'] = list(task.get('provider_aliases') or [])
            if 'extraction_templates' in proposed:
                spec['extraction_templates'] = proposed['extraction_templates']
            return validate_skill_spec(spec)
        except BudgetExceeded:
            raise
        except Exception:
            return None

    def propose_service(self, descriptor):
        if self.service_proposals is None:
            raise RuntimeError('Hand has no service proposal port')
        return self.service_proposals.propose(descriptor)

    def describe_service(self, task):
        """Research and propose one Voice operation; validation errors feed the loop."""
        if self.llm is None:
            raise RuntimeError('No hay LLM para proponer el servicio externo')
        template = {
            "service_id": "open_meteo",
            "operation": "forecast",
            "version": "v1",
            "url": "https://api.open-meteo.com/v1/forecast",
            "method": "GET",
            "parameters": ["latitude", "longitude", "daily", "timezone"],
            "content_types": ["application/json"],
            "max_bytes": 65536,
            "timeout": 15,
            "per_minute": 30,
            "effect": "read",
            "credential_ref": None,
            "credential_header": "Authorization",
            "credential_prefix": "Bearer ",
            "effect_contract": None
        }
        field_list = ", ".join(sorted(template.keys()))
        prompt = ("Eres Hand. Investiga documentación oficial antes de declarar una operación HTTPS para cumplir el Objetivo. "
                  "No adivines campos ni cambies de proveedor silenciosamente. Usa web_search tantas veces "
                  "como aporte información; cada error de validate_descriptor debe corregirse. Termina solo "
                  "con finish_descriptor. El descriptor DEBE contener exactamente estos 15 campos: " + field_list + ". "
                  "Ejemplo ilustrativo de estructura: " + json.dumps(template, ensure_ascii=False) +
                  "\nObjetivo: " + str(task['request']) +
                  "\nError o evidencia anterior: " + str(task.get('feedback') or 'ninguno') +
                  "\nNotas relevantes: " + json.dumps(self._episode_context(task['request']), ensure_ascii=False))
        result, run_id = self.agent.run(task.get('id'), 'Investiga y declara el servicio necesario.', prompt, [
            {'name': 'web_search', 'description': 'Busca documentación pública con fuentes verificables.',
             'parameters': {'type': 'object', 'properties': {'query': {'type': 'string'}},
                            'required': ['query'], 'additionalProperties': False},
             'handler': lambda args: self.llm.web_search(args['query'])},
            {'name': 'validate_descriptor', 'description': 'Valida un descriptor y devuelve el error exacto.',
             'parameters': {'type': 'object', 'properties': {'descriptor': {'type': 'object'}},
                            'required': ['descriptor'], 'additionalProperties': False},
             'handler': lambda args: self._validate_descriptor_tool(args)},
            {'name': 'finish_descriptor', 'description': 'Entrega el descriptor final; vuelve a validarlo.',
             'parameters': {'type': 'object', 'properties': {'descriptor': {'type': 'object'}},
                            'required': ['descriptor'], 'additionalProperties': False},
             'handler': lambda args: self._finish_descriptor(args)},
        ])
        self.last_agent_run_id = run_id
        digest = self.propose_service(result)
        if hasattr(self.service_proposals, 'get'):
            record = self.service_proposals.get(digest)
            if record and record.get('spec'):
                result = record['spec']
        return result, digest

    @staticmethod
    def _validate_descriptor_tool(args):
        spec = args.get('descriptor')
        validate_descriptor(spec)
        return {'valid': True, 'descriptor': spec}

    @staticmethod
    def _finish_descriptor(args):
        spec = args.get('descriptor')
        validate_descriptor(spec)
        return {'finished': True, 'value': spec}

    def _name(self, name):
        if not isinstance(name,str) or not re.fullmatch('[a-z][a-z0-9-]{0,63}',name):
            raise ValueError('Invalid skill name')
        if name.lower() in {'con','prn','aux','nul','heart'} or re.fullmatch('(com|lpt)[0-9]',name):
            raise ValueError('Reserved skill name')
        return name

    def version(self, version_id):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM versions WHERE version_id=?',(version_id,)).fetchone()
        if not row:
            raise ValueError('Unknown skill version')
        return dict(row)

    def active(self, name):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM versions WHERE skill_name=? AND status="active"',(self._name(name),)).fetchone()
        return dict(row) if row else None

    def _operation(self, kind, name, source=None, target=None, correlation_id=None, reason=''):
        op = uuid.uuid4().hex
        with connection(self.db) as conn:
            conn.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (op,kind,name,source,target,'pending',correlation_id,reason,time.time(),None,None))
        return op

    def _finish(self, op, error=None):
        with connection(self.db) as conn:
            conn.execute('UPDATE operations SET status=?,completed_at=?,error=? WHERE operation_id=?',
                         ('failed' if error else 'completed',time.time(),error,op))

    def _files(self, folder):
        if folder.is_symlink() or {p.name for p in folder.iterdir()} != {'SKILL.md','app','data','other'}:
            raise ValueError('A skill must contain SKILL.md and exactly app/, data/ and other/')
        files = []
        for p in sorted(folder.rglob('*')):
            if p.is_symlink() or not p.resolve().is_relative_to(folder.resolve()):
                raise ValueError('Links are forbidden in skills')
            if p.is_file():
                if p.name.lower() in {'.env','secrets.env','memory.sqlite3','rules.sqlite3','logs.sqlite3','openclaw.db'} or p.suffix.lower() == '.log':
                    raise ValueError('Secrets, databases and logs cannot enter skill archives')
                if p.stat().st_size > 64*1024*1024:
                    raise ValueError('Skill file exceeds size limit')
                files.append(p)
        if sum(p.stat().st_size for p in files) > 256*1024*1024:
            raise ValueError('Skill exceeds archive size limit')
        return files

    def tree_hash(self, folder):
        return sha(json.dumps([(p.relative_to(folder).as_posix(),sha(p.read_bytes()))
                               for p in self._files(folder)],separators=(',',':')).encode())

    def create(self, name, code, manifest, *, correlation_id=None, restored_from=None, version_number=None,
               skill_metadata=None):
        name = self._name(name)
        with self.locked():
            draft = self.root/('.'+name)
            if draft.exists():
                if draft.is_symlink() or draft.resolve().parent != self.root.resolve():
                    raise PermissionError('Symlink in skills root is forbidden')
                shutil.rmtree(draft)
            current = self.active(name)
            with connection(self.db) as conn:
                count = conn.execute('SELECT COUNT(*) FROM versions WHERE skill_name=?',(name,)).fetchone()[0]
            vid = uuid.uuid4().hex
            number = version_number or 'v'+str(count+1)
            op = self._operation('update' if current else 'create',name,current and current['version_id'],vid,correlation_id)
            try:
                draft.mkdir()
                for part in ('app','data','other'):
                    (draft/part).mkdir()
                metadata = skill_metadata or {}
                frontmatter = {
                    'name': name,
                    'capability': metadata.get('capability'),
                    'provider': metadata.get('provider'),
                    'aliases': metadata.get('aliases', []),
                }
                lines = ['---'] + [f'{key}: {json.dumps(value, ensure_ascii=False)}'
                                    for key, value in frontmatter.items() if value not in (None, [], '')]
                lines += ['---', '', '# ' + name, '', str(manifest.get('purpose') or name).strip(), '',
                          'Use this skill only for the declared capability and provider. Return evidence '
                          'from the real execution and never substitute a provider silently.', '']
                (draft/'SKILL.md').write_text('\n'.join(lines), encoding='utf-8')
                (draft/'app'/'main.py').write_bytes(code.encode('utf-8'))
                with connection(self.db) as conn:
                    conn.execute('''INSERT INTO versions(version_id,skill_name,version_number,status,artifact_hash,
                        created_at,previous_version_id,restored_from_version_id,manifest) VALUES (?,?,?,?,?,?,?,?,?)''',
                        (vid,name,number,'draft',self.tree_hash(draft),time.time(),current and current['version_id'],
                         restored_from,json.dumps(manifest)))
                self._finish(op)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise
            return self.version(vid)

    def edit(self, vid, code):
        with self.locked():
            v = self.version(vid)
            if v['status'] != 'draft':
                raise ValueError('Only drafts can be edited')
            op = self._operation('edit',v['skill_name'],vid,vid)
            folder = self.root/('.'+v['skill_name'])
            (folder/'app'/'main.py').write_bytes(code.encode('utf-8'))
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET artifact_hash=? WHERE version_id=?',(self.tree_hash(folder),vid))
            self._finish(op)

    def update(self, name, code, manifest, **kwargs):
        if not self.active(name):
            raise ValueError('Update requires an active skill')
        return self.create(name,code,manifest,**kwargs)

    def reject(self, vid, reason='Rejected'):
        with self.locked():
            v = self.version(vid)
            if v['status'] not in {'draft','approved'}:
                raise ValueError('Only pending versions can be rejected')
            op = self._operation('reject',v['skill_name'],vid,reason=reason)
            folder = self.root/('.'+v['skill_name'])
            # Keep rejected material outside the active skill name for inspection.
            if folder.exists():
                folder.rename(self.root/('.rejected-'+vid))
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET status="rejected" WHERE version_id=?',(vid,))
            self._finish(op)

    def finish_one_off(self, vid):
        """Retain a verified artifact without making it a reusable skill."""
        with self.locked():
            v = self.version(vid)
            if v['status'] != 'draft':
                raise ValueError('Only a candidate can finish as one-off')
            source = self.root/('.'+v['skill_name'])
            if self.tree_hash(source) != v['artifact_hash']:
                raise ValueError('Candidate changed after execution')
            source.rename(self.root/('.one-off-'+vid))
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET status="one_off" WHERE version_id=?', (vid,))
            return self.version(vid)

    def _verify_zip(self, path, name):
        if path.is_symlink() or path.stat().st_size>256*1024*1024:
            raise ValueError('Unsafe or oversized archive')
        with zipfile.ZipFile(path) as z:
            if len(z.infolist())>10000:
                raise ValueError('Too many archive entries')
            names = set()
            total = 0
            for item in z.infolist():
                p = PurePosixPath(item.filename)
                if ('\\' in item.filename or ':' in item.filename or p.is_absolute() or '..' in p.parts
                        or len(p.parts)<2 or p.parts[0] != name
                        or (p.parts[1] not in {'app','data','other','SKILL.md'})
                        or (p.parts[1] == 'SKILL.md' and len(p.parts) != 2)
                        or (item.external_attr >> 16) & 0o170000 == 0o120000 or item.filename in names):
                    raise ValueError('Invalid archive structure')
                names.add(item.filename)
                total += item.file_size
                if total > 256*1024*1024:
                    raise ValueError('Archive too large')
            if name+'/app/main.py' not in names or name+'/SKILL.md' not in names:
                raise ValueError('Archive has no entry point or SKILL.md')
            if z.testzip() is not None:
                raise ValueError('Corrupt archive')
        return sha(path.read_bytes()),path.stat().st_size

    def _archive(self, v):
        folder = self.root/v['skill_name']
        if self.tree_hash(folder) != v['artifact_hash']:
            raise ValueError('Active skill changed outside Hand')
        if v['archive_name']:
            path = self.root/v['archive_name']
            h,size = self._verify_zip(path,v['skill_name'])
            if h != v['archive_hash'] or size != v['archive_size']:
                raise ValueError('Immutable archive has changed')
            return
        n = 1
        while (self.root/f"{v['skill_name']}{n:03d}.zip").exists():
            n += 1
        if n>999:
            raise ValueError('Three-digit archive namespace exhausted; no files removed')
        path = self.root/f"{v['skill_name']}{n:03d}.zip"
        with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
            for part in ('app','data','other'):
                z.writestr(v['skill_name']+'/'+part+'/',b'')
            for p in self._files(folder):
                z.write(p,v['skill_name']+'/'+p.relative_to(folder).as_posix())
        h,size = self._verify_zip(path,v['skill_name'])
        with connection(self.db) as conn:
            conn.execute('UPDATE versions SET archive_name=?,archive_hash=?,archive_size=?,archived_at=? WHERE version_id=?',
                         (path.name,h,size,time.time(),v['version_id']))
        op = self._operation('archive',v['skill_name'],v['version_id'])
        self._finish(op)

    def approve(self, vid, *, user_approved=False, correlation_id=None):
        with self.locked():
            v = self.version(vid)
            if v['status'] == 'active':
                return v
            if v['status'] not in {'draft','approved'}:
                raise ValueError('Version cannot be activated')
            name = v['skill_name']
            draft, active = self.root/('.'+name),self.root/name
            if self.tree_hash(draft) != v['artifact_hash']:
                raise ValueError('Draft changed after review')
            audit = self.pixy.audit_artifact(draft,json.loads(v['manifest']),v['artifact_hash'])
            if not audit['safe']:
                raise PermissionError(audit.get('reason','Artifact not safe'))
            if audit.get('requires_approval') and not user_approved:
                raise PermissionError(audit.get('reason','Approval required'))
            current = self.active(name)
            if active.exists() and not current:
                raise ValueError('Unregistered active directory')
            op = self._operation('approve',name,current and current['version_id'],vid,correlation_id)
            old = self.root/('.previous-'+vid)
            try:
                if current:
                    self._archive(current)
                with connection(self.db) as conn:
                    conn.execute('UPDATE versions SET status="approved",pixy_audit_id=? WHERE version_id=?',
                                 (audit.get('audit_id') or ('audit_' + uuid.uuid4().hex),vid))
                if current:
                    active.rename(old)
                try:
                    draft.rename(active)
                except Exception:
                    if old.exists():
                        old.rename(active)
                    raise
                with connection(self.db) as conn:
                    if current:
                        conn.execute('UPDATE versions SET status="archived" WHERE version_id=?',(current['version_id'],))
                    conn.execute('UPDATE versions SET status="active",activated_at=? WHERE version_id=?',(time.time(),vid))
                if old.exists():
                    self._remove(old)
                self._finish(op)
                if self.events:
                    self.events.emit('hand','activated',skill_name=name,version_id=vid,correlation_id=correlation_id)
                return self.version(vid)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise

    def _remove(self, folder):
        # All deletions are confined to a direct child of the owned root.
        if folder.is_symlink() or folder.resolve().parent != self.root or not folder.is_dir():
            raise ValueError('Unsafe skill deletion')
        shutil.rmtree(folder)

    def archive(self, name):
        with self.locked():
            v = self.active(name)
            if not v:
                raise ValueError('No active skill')
            self._archive(v)
            return self.version(v['version_id'])

    def delete(self, name):
        with self.locked():
            v = self.active(name)
            if not v:
                raise ValueError('No active skill')
            op = self._operation('delete',name,v['version_id'])
            self._archive(v)
            self._remove(self.root/name)
            with connection(self.db) as conn:
                conn.execute('UPDATE versions SET status="archived" WHERE version_id=?',(v['version_id'],))
            self._finish(op)

    def restore(self, vid, *, user_approved=False, rollback=False):
        v = self.version(vid)
        if not v['archive_name']:
            raise ValueError('Version has no archive')
        path = self.root/v['archive_name']
        h,size = self._verify_zip(path,v['skill_name'])
        if (h,size) != (v['archive_hash'],v['archive_size']):
            raise ValueError('Immutable archive changed')
        current = self.active(v['skill_name'])
        number = None
        if rollback and current:
            base = current['version_number'].split('.')[0]
            with connection(self.db) as conn:
                used = {r[0] for r in conn.execute('SELECT version_number FROM versions WHERE skill_name=?',(v['skill_name'],))}
            n = 1
            while base+'.'+str(n) in used:
                n += 1
            number = base+'.'+str(n)
        with zipfile.ZipFile(path) as z:
            code = z.read(v['skill_name']+'/app/main.py').decode('utf-8')
            draft = self.create(v['skill_name'],code,json.loads(v['manifest']),restored_from=vid,version_number=number)
            folder = self.root/('.'+v['skill_name'])
            with self.locked():
                for item in z.infolist():
                    relative = PurePosixPath(item.filename).relative_to(v['skill_name'])
                    dest = folder/str(relative)
                    if item.is_dir():
                        dest.mkdir(parents=True,exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        dest.write_bytes(z.read(item))
                if self.tree_hash(folder) != v['artifact_hash']:
                    raise ValueError('Archive content hash mismatch')
                with connection(self.db) as conn:
                    conn.execute('UPDATE versions SET artifact_hash=? WHERE version_id=?',(v['artifact_hash'],draft['version_id']))
        op = self._operation('rollback' if rollback else 'restore',v['skill_name'],vid,draft['version_id'])
        try:
            result = self.approve(draft['version_id'],user_approved=user_approved)
            self._finish(op)
            return result
        except Exception as exc:
            self._finish(op,type(exc).__name__)
            raise

    def rollback(self, vid, **kwargs):
        return self.restore(vid,rollback=True,**kwargs)

    def use(self, name, correlation_id=None):
        v = self.active(name)
        if not v or self.tree_hash(self.root/name) != v['artifact_hash']:
            raise ValueError('No intact active version')
        op = self._operation('use',name,v['version_id'],correlation_id=correlation_id)
        self._finish(op)
        return v

    def prepare_execution(self, vid, eid, code_hash, *, user_approved=False):
        if not re.fullmatch('run_[a-f0-9]{32}',eid):
            raise ValueError('Invalid execution ID')
        with self.locked():
            v = self.version(vid)
            if v['status'] not in {'active', 'draft'}:
                raise ValueError('Execution requires an active or draft skill')
            source = self.root/v['skill_name'] if v['status'] == 'active' else self.root/('.'+v['skill_name'])
            if self.tree_hash(source)!=v['artifact_hash'] or sha((source/'app'/'main.py').read_bytes())!=code_hash:
                raise ValueError('Execution artifact differs from approved skill')
            if v['status'] == 'draft':
                audit = self.pixy.audit_artifact(source, json.loads(v['manifest']), v['artifact_hash'])
                if not audit['safe'] or (audit.get('requires_approval') and not user_approved):
                    raise PermissionError(audit.get('reason', 'Candidate audit or approval failed'))
            target = self.root.parent/'execution_inputs'/eid/'skill'
            if target.exists():
                if self.tree_hash(target)!=v['artifact_hash']:
                    raise ValueError('Execution snapshot changed')
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copytree(source,target,symlinks=False)
                if self.tree_hash(target)!=v['artifact_hash']:
                    raise ValueError('Skill changed during snapshot')
            return str(target)

    def publication(self, name, asset_id, published=True):
        v = self.active(name)
        if not v:
            raise ValueError('Unknown active skill')
        op = self._operation('publish' if published else 'unpublish',name,v['version_id'],reason=asset_id)
        self._finish(op)

    def publish(self, name, asset_id):
        self.publication(name,asset_id,True)

    def unpublish(self, name, asset_id):
        self.publication(name,asset_id,False)

    # ------------------------------------------------------------------ recovery
    def _recovery_order(self, order):
        """Validate a Heart order. The header carries the verifiable Pixy authorization reference."""
        if not isinstance(order,dict) or set(order) != {'header','plan'}:
            raise ValueError('Orden de recuperación inválida')
        header = order['header']
        required = {'case_id','issued_by','authorization_id','nonce','plan_hash'}
        allowed = required | {'order_id'}
        if not isinstance(header,dict) or not required <= set(header) or not set(header) <= allowed:
            raise ValueError('Encabezado de recuperación inválido')
        if header['issued_by'] != 'heart':
            raise PermissionError('Solo Heart emite órdenes de recuperación')
        case_id = validate_case_id(header['case_id'])
        core = validate_plan(order['plan'])
        expected_plan = set(core)
        if set(order['plan']) != expected_plan:
            raise ValueError('La orden contiene campos fuera del contrato')
        h = plan_hash(case_id,core)
        if h != header['plan_hash']:
            raise PermissionError('El plan no coincide con el hash autorizado')
        return header,case_id,core,h

    def _authorization_terms(self, header, case_id, core, h):
        return (header['authorization_id'],header['nonce'],case_id,core['capabilities'],core['target'],h)

    def remediate_task(self, order, transition):
        """Hand dispatches an exact grant; Coordinator supplies the task transition."""
        header, case_id, core, h = self._recovery_order(order)
        if core['target'] != 'task' or self.authorization_port is None or not header.get('order_id'):
            raise PermissionError('La reparación requiere una orden de tarea de Pixy para Hand')
        return self.authorization_port.dispatch_task_recovery(header, core, transition)

    def _recovery_row(self, case_id):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM recovery_skills WHERE case_id=?',(case_id,)).fetchone()
        return dict(row) if row else None

    def _recovery_document(self, case_id, core, h):
        return canonical({'case_id':case_id,'plan':core,'plan_hash':h}).encode('utf-8')

    def _verify_recovery_skill(self, row, case_id, core, h):
        folder = self.recovery_root/case_id
        if folder.is_symlink() or not folder.is_dir() or folder.resolve().parent != self.recovery_root.resolve():
            raise PermissionError('Skill de recuperación ausente o fuera de data/hand/heart')
        if self.tree_hash(folder) != row['skill_hash']:
            raise PermissionError('La skill de recuperación cambió fuera de Hand')
        if (folder/'app'/'recovery.json').read_bytes() != self._recovery_document(case_id,core,h):
            raise PermissionError('La skill de recuperación no coincide con la orden autorizada')
        return folder

    def create_recovery(self, order):
        header,case_id,core,h = self._recovery_order(order)
        authorizations = self.authorization_port or getattr(self.pixy,'authorizations',None)
        if authorizations is None:
            raise PermissionError('Pixy no tiene un registro de autorizaciones')
        with self.locked():
            row = self._recovery_row(case_id)
            if row:
                if row['plan_hash'] != h or row['authorization_id'] != header['authorization_id']:
                    raise PermissionError('Ya existe otra skill de recuperación para este caso')
                self._verify_recovery_skill(row,case_id,core,h)
                return {'case_id':case_id,'path':'hand/heart/'+case_id,'skill_hash':row['skill_hash'],'status':row['status']}
            authorizations.verify(*self._authorization_terms(header,case_id,core,h))
            self.recovery_root.mkdir(exist_ok=True)
            folder, draft = self.recovery_root/case_id, self.recovery_root/('.'+case_id)
            if folder.exists():
                raise ValueError('Carpeta de recuperación no registrada; se conserva para inspección')
            if draft.exists():
                if draft.is_symlink() or draft.resolve().parent != self.recovery_root.resolve():
                    raise ValueError('Borrador de recuperación inseguro')
                shutil.rmtree(draft)  # Unregistered draft from a crash; never executed.
            op = self._operation('recovery_create','heart/'+case_id,correlation_id=case_id,reason=header['authorization_id'])
            try:
                for part in ('app','data','other'):
                    (draft/part).mkdir(parents=True)
                (draft/'app'/'recovery.json').write_bytes(self._recovery_document(case_id,core,h))
                (draft/'other'/'manifest.json').write_bytes(canonical({
                    'purpose':'recovery','case_id':case_id,'issued_by':'heart','created_by':'hand',
                    'authorization_id':header['authorization_id'],'capabilities':core['capabilities'],
                    'target':core['target']}).encode('utf-8'))
                skill_hash = self.tree_hash(draft)
                draft.rename(folder)
                now = time.time()
                with connection(self.db) as conn:
                    conn.execute('INSERT INTO recovery_skills VALUES (?,?,?,?,?,?,?)',
                                 (case_id,header['authorization_id'],h,skill_hash,'created',now,now))
                self._finish(op)
            except Exception as exc:
                self._finish(op,type(exc).__name__)
                raise
        if self.events:
            self.events.emit('hand','recovery_created',correlation_id=case_id,metadata={'skill_hash':skill_hash})
        return {'case_id':case_id,'path':'hand/heart/'+case_id,'skill_hash':skill_hash,'status':'created'}

    def _set_recovery(self, case_id, status):
        with connection(self.db) as conn:
            conn.execute('UPDATE recovery_skills SET status=?,updated_at=? WHERE case_id=?',(status,time.time(),case_id))

    def _step(self, case_id, index):
        with connection(self.db) as conn:
            row = conn.execute('SELECT * FROM recovery_steps WHERE case_id=? AND step_index=?',(case_id,index)).fetchone()
        if not row:
            return None
        step = dict(row)
        step['receipt'] = json.loads(step['receipt']) if step['receipt'] else None
        return step

    def _save_step(self, case_id, index, status, receipt):
        with connection(self.db) as conn:
            conn.execute('UPDATE recovery_steps SET status=?,receipt=?,updated_at=? WHERE case_id=? AND step_index=?',
                         (status,json.dumps(receipt,ensure_ascii=False),time.time(),case_id,index))
        return self._step(case_id,index)

    def recovery_summary(self, case_id):
        row = self._recovery_row(case_id)
        with connection(self.db) as conn:
            steps = [dict(r) for r in conn.execute('SELECT * FROM recovery_steps WHERE case_id=? ORDER BY step_index',(case_id,))]
        for step in steps:
            step['receipt'] = json.loads(step['receipt']) if step['receipt'] else None
        return {'case_id':case_id,'status':row['status'] if row else 'missing','skill_hash':row and row['skill_hash'],
                'steps':[{'index':s['step_index'],'action':s['action'],'target':s['target'],'action_id':s['action_id'],
                          'status':s['status'],'receipt':s['receipt']} for s in steps]}

    def advance_recovery(self, order):
        """Idempotent: spend the authorization once, then submit/poll each step exactly once."""
        header,case_id,core,h = self._recovery_order(order)
        with self.locked():
            row = self._recovery_row(case_id)
            if not row:
                raise ValueError('Skill de recuperación no creada')
            if row['authorization_id'] != header['authorization_id'] or row['plan_hash'] != h:
                raise PermissionError('La orden no corresponde a la skill registrada')
            if row['status'] in {'completed','failed','unknown'}:
                return self.recovery_summary(case_id)
            self._verify_recovery_skill(row,case_id,core,h)
            if row['status'] in {'created','consuming'}:
                self._set_recovery(case_id,'consuming')
                authorizations = self.authorization_port or getattr(self.pixy,'authorizations',None)
                if authorizations is None:
                    raise PermissionError('Hand no recibió un puerto de autorizaciones')
                if self.authorization_port:
                    if not header.get('order_id'):
                        raise PermissionError('La orden de Heart no contiene order_id')
                    authorizations.claim_order(
                        header['order_id'], *self._authorization_terms(header,case_id,core,h),
                        consumer='hand:'+case_id)
                else:
                    authorizations.consume(*self._authorization_terms(header,case_id,core,h),consumer='hand:'+case_id)
                self._set_recovery(case_id,'executing')
        for index,spec in enumerate(core['steps']):
            step = self._step(case_id,index)
            if step is None:
                action_id = 'rec_'+uuid.uuid4().hex
                now = time.time()
                # Receipt before side effect: a crash resubmits the SAME id, which the executor deduplicates.
                with connection(self.db) as conn:
                    conn.execute('INSERT INTO recovery_steps VALUES (?,?,?,?,?,?,?,?,?)',
                                 (case_id,index,spec['action'],spec['target'],action_id,'submitting',None,now,now))
                step = self._step(case_id,index)
                if self.recovery_executor is None:
                    step = self._save_step(case_id,index,'failed',{'error':'No hay canal de recuperación configurado'})
            if step['status'] not in FINAL_STEP:
                from app.voice.runtime.recovery_runtime import RecoveryRejected
                try:
                    if step['status'] == 'submitting':
                        receipt = self.recovery_executor.submit(step['action_id'],spec['action'],spec['target'])
                    else:
                        receipt = self.recovery_executor.status(step['action_id'])
                    status = receipt.get('status')
                    if status not in FINAL_STEP:
                        status = 'running'
                    step = self._save_step(case_id,index,status,receipt)
                except RecoveryRejected as exc:
                    step = self._save_step(case_id,index,'failed',{'error':str(exc)[:300]})
                except OSError:
                    if time.time()-step['submitted_at'] > RECOVERY_STEP_TIMEOUT:
                        step = self._save_step(case_id,index,'unknown',{'error':'Canal sin respuesta; no se repite'})
            if step['status'] not in FINAL_STEP:
                return self.recovery_summary(case_id) | {'status':'running'}
            if step['status'] != 'completed':
                break  # Later steps never run after a failed or uncertain one.
        statuses = [s['status'] for s in self.recovery_summary(case_id)['steps']]
        final = ('completed' if len(statuses)==len(core['steps']) and set(statuses)=={'completed'}
                 else 'unknown' if 'unknown' in statuses else 'failed')
        self._set_recovery(case_id,final)
        op = self._operation('recovery_execute','heart/'+case_id,correlation_id=case_id,reason=final)
        self._finish(op,None if final=='completed' else final)
        if self.events:
            self.events.emit('hand','recovery_finished',correlation_id=case_id,metadata={'status':final})
        return self.recovery_summary(case_id)
