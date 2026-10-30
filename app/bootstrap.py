"""Composition root. Domain ownership is explicit; transports stay in Voice."""
import os
import logging
from pathlib import Path
from app.kernel.database import connection
from app.kernel.events import Events
from app.kernel.control_plane import ControlPlane
from app.mind.identity import Identity
from app.mind.memory import MemoryEngine
from app.mind.state import StateManager
from app.mind.mind import Mind
from app.mind.brain import Brain
from app.mind.skill_selector import LocalEncoder, SkillSelector
from app.mind.action_planner import ActionPlanner
from app.hand.hand import Hand
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.dreams.dreams import Dreams
from app.heart.beat import Beat
from app.senses.senses import Senses, OBSERVERS
from app.pixy.authorization import AuthorizationStore
from app.heart.recovery import Recovery
from app.voice.runtime.recovery_runtime import ProxyRecoveryExecutor
from app.voice.secrets import Secrets, SecretCaptureService, CredentialStatusPort
from app.voice.mailbox import VoiceMailbox
from app.voice.gateway.llm_gateway import LLMManager
from app.mind.brain import Brain, ModelController, DolphinController
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.service_registry import ServiceRegistry, HandServicePort, PixyServicePort, VoiceServicePort
from app.voice.gateway.asset_gateway import AssetGateway
from app.voice.gateway.observation_gateway import CoreEyes
from app.voice.runtime.docker_runtime import DockerRuntime
from app.voice.runtime.local_runtime import LocalRuntime


class ModuleManager:
    def __init__(self, config_dir='config', data_dir='data'):
        self.data_dir = Path(data_dir).resolve()
        self.control_db = self.data_dir/'control.sqlite3'
        self.events = Events(self.control_db)
        self.control = ControlPlane(self.control_db, self.events)
        self.voice_mailbox = VoiceMailbox(self.control_db)
        self.coordinator_control = self.control.ports.coordinator
        self.pixy_control = self.control.ports.pixy
        self.heart_control = self.control.ports.heart
        self.hand_control = self.control.ports.hand
        self.identity = Identity(self.data_dir/'mind'/'identity.yaml',self.data_dir/'memory'/'agent.md')
        self.memory = MemoryEngine(str(self.data_dir/'mind'),self.control_db,self.identity)
        self.state = StateManager(str(self.data_dir),self.control_db)
        self.audit_db = self.control_db
        # Compatibility handle for code outside the composition root. Organs receive scoped ports.
        self.authorizations = self.control.authorizations
        self.recovery_executor = ProxyRecoveryExecutor()
        self.secrets = Secrets(self.data_dir)
        self.voice_credentials = CredentialStatusPort(self.secrets)
        self.secret_port = self.control.voice_secrets
        self.secret_capture = SecretCaptureService(self.secrets, self.secret_port)
        self.telegram = TelegramHandler(self.secrets.get('TELEGRAM_BOT_TOKEN'),self.secrets.get('TELEGRAM_USER_ID'))
        self.model_controller = ModelController(str(self.data_dir), self.secrets)
        self.dolphin = self.model_controller  # Backward compatibility alias
        provider = LLMManager(config_dir, str(self.data_dir/'voice'), self.memory, self.secrets, self.data_dir/'voice'/'voice.sqlite3', dolphin=self.model_controller)
        self.brain = Brain(provider, controller=self.model_controller)
        self.llm = self.brain
        self.mind = Mind(self.brain, self.memory)
        self.pixy = PixyMind(self)
        self.rules = Rules(self.control_db)
        service_registry = ServiceRegistry(self.control_db)
        self.hand_services = HandServicePort(service_registry)
        self.pixy_services = PixyServicePort(service_registry)
        self.voice_services = VoiceServicePort(service_registry)
        self.hand = Hand(self.data_dir/'hand',self.pixy,self.events,recovery_executor=self.recovery_executor,
                         llm=self.llm,db_path=self.control_db,authorization_port=self.hand_control,
                         service_proposals=self.hand_services)
        self.hand.manager = self
        self.gateway = ExternalGateway(Path(config_dir)/'gateways.yaml',self.secrets,self.events,
                                       self.data_dir/'voice'/'voice.sqlite3',registry=self.voice_services,
                                       execution_authority=self.control.voice_execution)
        encoder = None
        backend, model = os.getenv('KHAI_EMBED_BACKEND'), os.getenv('KHAI_EMBED_MODEL')
        if backend and model:
            try:
                encoder = LocalEncoder(backend, model)
            except (ImportError, OSError, ValueError) as exc:
                logging.getLogger(__name__).warning('Embedding local no disponible: %s', type(exc).__name__)
        threshold = os.getenv('KHAI_EMBED_THRESHOLD')
        margin = os.getenv('KHAI_EMBED_MARGIN')
        self.skill_selector = SkillSelector(self.hand.catalog, encoder, self.llm,
            threshold=float(threshold) if threshold and encoder else None,
            min_margin=float(margin) if margin and encoder else None)
        self.action_planner = ActionPlanner(self.llm, self.hand.catalog)
        choice = os.getenv('KHAI_RUNTIME','docker')
        if choice not in {'docker','local'}:
            raise ValueError('KHAI_RUNTIME must be docker or local')
        self.runtime = LocalRuntime(self.data_dir) if choice=='local' else DockerRuntime()
        self.hand.capabilities = self.runtime.capabilities
        self.assets = AssetGateway(self.data_dir.parent/'other',self.data_dir/'voice'/'voice.sqlite3',self.pixy,self.events,self.hand)
        self.dreams = Dreams(self.control_db, self.rules, self.events, memory=self.memory)
        self.heart = Beat(self.data_dir,self.events)
        adapters = {name:lambda name=name:self._domain_snapshot(name) for name in OBSERVERS}
        self.senses = Senses(self.data_dir/'senses'/'senses.sqlite3',adapters,CoreEyes(str(self.data_dir)))
        self.heart.recovery = Recovery(self.control_db,self.senses,self.hand,self.heart_control,self.events)

    def _domain_snapshot(self, name):
        if name in {'database','storage','heart'}:
            return self.heart.run_diagnostic_fast()
        if name in {'runtime','docker'}:
            return {'implementation':type(self.runtime).__name__,'status':'configured'}
        if name=='network':
            with connection(self.events.path) as conn:
                rows = conn.execute("SELECT created_at,metadata FROM events WHERE module='voice' AND event_type='external_request' ORDER BY created_at DESC LIMIT 20").fetchall()
            return {'policy':'Voice-only; public DNS pinned; TLS verified; redirects denied',
                    'services':sorted(self.gateway.services),'recent':[dict(r) for r in rows]}
        if name=='security':
            return {'skill_network':'denied','worker_secrets':'absent','mutable_rules':'advisory_only'}
        if name=='webhook':
            return {'authentication':'owner ID and webhook secret','configured':len(os.getenv('TELEGRAM_WEBHOOK_SECRET',''))>=32}
        if name=='pixy':
            tasks = []
            if hasattr(self, 'coordinator') and self.coordinator:
                tasks = [dict(t) for t in self.coordinator.store.all()[-10:]]
            elif self.state:
                try:
                    import json
                    with connection(self.state.db_path) as conn:
                        rows = conn.execute("SELECT id, source, document FROM control_tasks ORDER BY rowid DESC LIMIT 10").fetchall()
                        tasks = [json.loads(r['document']) for r in rows]
                except Exception:
                    tasks = []
            return {'recent_tasks': tasks}
        with connection(self.events.path) as conn:
            row = conn.execute('SELECT COUNT(*) events,MAX(created_at) last_event FROM events WHERE module=?',(name,)).fetchone()
        return dict(row)
