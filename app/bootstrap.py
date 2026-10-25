"""Composition root. Domain ownership is explicit; transports stay in Voice."""
import os
from pathlib import Path
from app.common.database import connection
from app.common.events import Events
from app.common.migration import migrate_data
from app.mind.identity import Identity
from app.mind.memory import MemoryEngine
from app.mind.state import StateManager
from app.mind.mind import Mind
from app.mind.brain import Brain
from app.hand.forge import SkillForge
from app.hand.hand import Hand
from app.pixy.pixy import PixyMind
from app.pixy.rules import Rules
from app.dreams.dreams import Dreams
from app.heart.beat import Beat
from app.senses.senses import Senses, OBSERVERS
from app.voice.secrets import Secrets
from app.voice.gateway.llm_gateway import LLMManager
from app.voice.gateway.telegram_gateway import TelegramHandler
from app.voice.gateway.external_gateway import ExternalGateway
from app.voice.gateway.asset_gateway import AssetGateway
from app.voice.gateway.observation_gateway import CoreEyes
from app.voice.runtime.docker_runtime import DockerRuntime
from app.voice.runtime.local_runtime import LocalRuntime


class ModuleManager:
    def __init__(self, config_dir='config', data_dir='data'):
        self.data_dir = Path(data_dir).resolve()
        migrate_data(self.data_dir)
        self.events = Events(self.data_dir/'logs.sqlite3')
        self.identity = Identity(self.data_dir/'mind'/'identity.yaml',self.data_dir/'memory'/'agent.md')
        self.memory = MemoryEngine(str(self.data_dir/'mind'),self.data_dir/'mind'/'memory.sqlite3',self.identity)
        self.state = StateManager(str(self.data_dir/'pixy'),self.data_dir/'pixy'/'logs.sqlite3')
        self.audit_db = self.data_dir/'pixy'/'logs.sqlite3'
        self.secrets = Secrets(self.data_dir)
        self.telegram = TelegramHandler(self.secrets.get('TELEGRAM_BOT_TOKEN'),self.secrets.get('TELEGRAM_USER_ID'))
        provider = LLMManager(config_dir,str(self.data_dir/'voice'),self.memory,self.secrets,self.data_dir/'voice'/'voice.sqlite3')
        self.llm = Brain(provider)
        self.mind = Mind(self.llm,self.memory)
        self.pixy = PixyMind(self)
        self.rules = Rules(self.data_dir/'pixy'/'rules.sqlite3')
        self.hand = Hand(self.data_dir/'hand',self.pixy,self.events)
        self.forge = SkillForge(self.llm,str(self.data_dir/'hand'))
        self.forge.manager = self
        self.gateway = ExternalGateway(Path(config_dir)/'gateways.yaml',self.secrets,self.events,self.data_dir/'voice'/'voice.sqlite3')
        choice = os.getenv('KHAI_RUNTIME','docker')
        if choice not in {'docker','local'}:
            raise ValueError('KHAI_RUNTIME must be docker or local')
        self.runtime = LocalRuntime(self.data_dir) if choice=='local' else DockerRuntime()
        self.assets = AssetGateway(self.data_dir.parent/'other',self.data_dir/'voice'/'voice.sqlite3',self.pixy,self.events,self.hand)
        self.dreams = Dreams(self.data_dir/'dreams'/'dreams.sqlite3',self.rules,self.events)
        self.heart = Beat(self.data_dir,self.events)
        adapters = {name:lambda name=name:self._domain_snapshot(name) for name in OBSERVERS}
        self.senses = Senses(self.data_dir/'senses'/'senses.sqlite3',adapters,CoreEyes(str(self.data_dir)))

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
        with connection(self.events.path) as conn:
            row = conn.execute('SELECT COUNT(*) events,MAX(created_at) last_event FROM events WHERE module=?',(name,)).fetchone()
        return dict(row)
