"""Semantic skill retrieval with deterministic compatibility and parameter checks."""
import json
import math
import logging

from app.hand.skill_spec import extract_params
from app.pixy.control import BudgetExceeded

logger = logging.getLogger(__name__)


class LocalEncoder:
    """Optional adapter; deployment chooses a measured backend and pinned model."""
    def __init__(self, backend, model_name):
        if backend == 'fastembed':
            from fastembed import TextEmbedding
            self.model = TextEmbedding(model_name=model_name)
            self.backend = backend
        elif backend == 'sentence-transformers':
            from sentence_transformers import SentenceTransformer
            self.model = SentenceTransformer(model_name, device='cpu')
            self.backend = backend
        else:
            raise ValueError('Unsupported embedding backend')

    def encode(self, texts):
        vectors = self.model.embed(texts) if self.backend == 'fastembed' else self.model.encode(texts)
        return [list(map(float, vector)) for vector in vectors]


def validate_params(schema, params, *, allow_missing=False):
    if not isinstance(params, dict) or set(params) - set(schema['properties']):
        return False
    if not allow_missing and set(schema.get('required', [])) - set(params):
        return False
    for name, value in params.items():
        kind = schema['properties'][name]['type']
        if kind == 'string' and not isinstance(value, str):
            return False
        if kind == 'integer' and (type(value) is not int):
            return False
        if kind == 'number' and (type(value) not in {int, float} or not math.isfinite(value)):
            return False
        if kind == 'boolean' and type(value) is not bool:
            return False
        if kind == 'object' and not isinstance(value, dict):
            return False
        if kind == 'array' and not isinstance(value, list):
            return False
    return True


def cosine(left, right):
    if len(left) != len(right) or not left:
        raise ValueError('Embedding dimension mismatch')
    denom = math.sqrt(sum(a*a for a in left) * sum(b*b for b in right))
    return sum(a*b for a, b in zip(left, right)) / denom if denom else 0.0


class SkillSelector:
    def __init__(self, catalog, encoder=None, llm=None, *, threshold=None, min_margin=None):
        self.catalog, self.encoder, self.llm = catalog, encoder, llm
        # Never infer automatic acceptance from an uncalibrated model score.
        self.threshold, self.min_margin = threshold, min_margin

    def select(self, query, compatible, capability=None, provider=None):
        records = [record for record in self.catalog.active() if compatible(record['spec'])]
        if capability:
            records = [record for record in records if record['spec'].get('capability') == capability]
        if provider:
            records = [record for record in records if record['spec'].get('provider') == provider
                       or provider in record['spec'].get('aliases', [])]
        if not records:
            return None
        ranked = records
        if self.encoder is not None:
            try:
                texts = [query] + [record['spec']['description'] + ' ' + ' '.join(
                    record['spec']['use_cases']) for record in records]
                vectors = self.encoder.encode(texts)
                if len(vectors) != len(texts):
                    raise ValueError('Embedding result length mismatch')
                pairs = sorted(((cosine(vectors[0], vector), record)
                                for vector, record in zip(vectors[1:], records)),
                               key=lambda pair: pair[0], reverse=True)
                ranked = [record for _, record in pairs]
                scores = [score for score, _ in pairs]
                confident = (self.threshold is not None and self.min_margin is not None
                             and scores[0] >= self.threshold
                             and (len(scores) == 1 or scores[0] - scores[1] >= self.min_margin))
                if confident:
                    params = extract_params(ranked[0]['spec'], query)
                    if params is not None and validate_params(ranked[0]['spec']['input_schema'], params):
                        return {'version_id': ranked[0]['version_id'], 'skill_id': ranked[0]['skill_id'],
                                'params': params, 'selection': 'local'}
            except Exception as exc:
                logger.warning('Local embedding unavailable: %s', type(exc).__name__)
        if self.llm is None:
            return None
        options = [{'skill_id': record['skill_id'], 'description': record['spec']['description'],
                    'capability': record['spec'].get('capability'),
                    'provider': record['spec'].get('provider'),
                    'use_cases': record['spec']['use_cases'],
                    'input_schema': record['spec']['input_schema']} for record in ranked[:20]]
        prompt = ('Selecciona una capacidad que resuelva la solicitud COMPLETA y extrae los parámetros presentes. '
                  'Si solo cubre una parte de una solicitud con varias acciones, usa null; '
                  'Devuelve JSON {"skill_id": string|null, "params": object}. '
                  'Si faltan parámetros obligatorios, elige la capacidad y omite esos parámetros '
                  'para que Khai los pregunte; nunca inventes valores. Si ninguna sirve, usa null. '
                  'No inventes permisos ni capacidades.\n'
                  'Solicitud: ' + query + '\nOpciones: ' + json.dumps(options, ensure_ascii=False))
        try:
            proposal = json.loads(self.llm.generate_content(prompt, response_format={'type': 'json_object'}))
            match = next((record for record in records if record['skill_id'] == proposal.get('skill_id')), None)
            if not match or not validate_params(match['spec']['input_schema'], proposal.get('params'),
                                                 allow_missing=True):
                return None
            missing = sorted(set(match['spec']['input_schema'].get('required', [])) -
                             set(proposal['params']))
            return {'version_id': match['version_id'], 'skill_id': match['skill_id'],
                    'params': proposal['params'], 'selection': 'llm', 'missing_params': missing}
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning('Skill selection fallback unavailable: %s', type(exc).__name__)
            return None
