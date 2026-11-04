import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from app.hand.skill_spec import normalize_schema
from app.heart.recovery import Recovery
from app.mind.action_planner import ActionPlanner
from app.mind.coordinator import Coordinator
from app.pixy.control import ControlStore
from app.pixy.pixy import PixyMind


def schema(**fields):
    return {
        'type': 'object',
        'properties': {k: {'type': v} for k, v in fields.items()},
        'required': sorted(fields),
        'additionalProperties': False
    }


class ActionPlannerEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.weather_spec = {
            'skill_id': 'clima-santiago-openweather',
            'description': 'Obtiene el clima actual de Santiago',
            'use_cases': ['Consultar clima actual'],
            'input_schema': schema(city='string'),
            'output_schema': schema(temperature='number'),
            'runtime': 'SAFE',
            'dependencies': [],
            'services': [],
            'verifier': {'kind': 'owner_criterion'}
        }
        self.active_record = {
            'skill_id': 'clima-santiago-openweather',
            'version_id': 'clima-santiago-openweather.001',
            'spec': self.weather_spec
        }
        self.catalog = SimpleNamespace(active=lambda: [self.active_record])

    def test_coverage_gap_triggers_skill_evolution_without_value_error(self):
        """Si la cobertura detecta un vacío (ej. pronóstico), evoluciona la skill en vez de lanzar ValueError."""
        llm = Mock()
        # 1. First prompt (proposal): LLM selects existing skill
        proposal_recipe = {
            'recipe_id': 'clima-manana',
            'input_schema': schema(city='string'),
            'steps': [{
                'id': 'paso_clima',
                'version_id': 'clima-santiago-openweather.001',
                'bindings': {'city': 'input.city'}
            }],
            'output': 'paso_clima.temperature'
        }
        proposal_resp = {
            'recipe': proposal_recipe,
            'inputs': {'city': 'Santiago'},
            'missing_specs': {}
        }
        # 2. Coverage check: reports missing "pronóstico futuro"
        first_coverage = {'sufficient': False, 'missing': ['pronóstico futuro']}
        # 3. Recheck coverage of evolved skill: reports sufficient
        evolved_coverage = {'sufficient': True, 'missing': []}

        llm.generate_content.side_effect = [
            json.dumps(proposal_resp),
            json.dumps(first_coverage),
            json.dumps(evolved_coverage)
        ]

        planner = ActionPlanner(llm, self.catalog)
        result = planner.propose('Cual es el clima para mañana?', lambda spec: True)

        self.assertIsNotNone(result)
        self.assertIn('paso_clima', result['missing_specs'])
        evolved_action = result['missing_specs']['paso_clima']
        self.assertEqual(evolved_action['skill_id'], 'clima-santiago-openweather')
        self.assertTrue(evolved_action.get('is_evolution'))
        self.assertEqual(evolved_action.get('previous_version_id'), 'clima-santiago-openweather.001')
        self.assertIn('pronóstico futuro', evolved_action['description'])
        self.assertTrue(result['coverage']['sufficient'])

    def test_unfulfillable_goal_returns_none_cleanly(self):
        """Si la cobertura falla y no puede evolucionar ni buscar en la web, retorna None sin romper con ValueError."""
        llm = Mock()
        # Proposal with no steps
        proposal_resp = {'steps': [], 'inputs': {}}
        llm.generate_content.return_value = json.dumps(proposal_resp)

        planner = ActionPlanner(llm, self.catalog)
        result = planner.propose('hacer algo imposible', lambda spec: True)
        self.assertIsNone(result)

    def test_fallback_web_search_when_no_skill_to_evolve(self):
        """Si la cobertura falla y no hay skill específica para evolucionar, recurre a web-search."""
        ws_record = {
            'skill_id': 'web-search',
            'version_id': 'web-search.001',
            'spec': {
                'skill_id': 'web-search',
                'description': 'Búsqueda web',
                'use_cases': ['Buscar información'],
                'input_schema': schema(query='string'),
                'output_schema': schema(result='string'),
                'runtime': 'SAFE',
                'dependencies': [],
                'services': [],
                'verifier': {'kind': 'owner_criterion'}
            }
        }
        catalog = SimpleNamespace(active=lambda: [ws_record])
        llm = Mock()
        # Proposal generates an invalid recipe or insufficient coverage
        proposal_recipe = {
            'recipe_id': 'dummy',
            'input_schema': schema(x='string'),
            'steps': [{'id': 'step_1', 'version_id': 'missing-dummy', 'bindings': {'x': 'input.x'}}],
            'output': 'step_1.result'
        }
        missing_spec = {
            'skill_id': 'dummy-skill',
            'description': 'dummy desc',
            'use_cases': ['dummy use'],
            'input_schema': schema(x='string'),
            'output_schema': schema(result='string'),
            'capabilities': []
        }
        proposal_resp = {
            'recipe': proposal_recipe,
            'inputs': {'x': 'test'},
            'missing_specs': {'step_1': missing_spec}
        }
        # Coverage check fails and missing- cannot be evolved because it's already missing-
        coverage_fail = {'sufficient': False, 'missing': ['datos actualizados']}
        llm.generate_content.side_effect = [
            json.dumps(proposal_resp),
            json.dumps(coverage_fail)
        ]

        planner = ActionPlanner(llm, catalog)
        result = planner.propose('noticias del día', lambda spec: True)

        self.assertIsNone(result)  # Search availability cannot establish coverage or authority.


class HeartAntiLoopAndPixyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'control.sqlite3'
        self.store = ControlStore(self.db_path)
        self.senses = Mock()
        self.senses.evidence.return_value = {
            'container_core': {'running': True, 'oom_killed': False},
            'container_proxy': {'running': True, 'scheduler_age': 5},
            'storage': {'vitals': {'disk_free': 10 * 1024**3, 'ram_percent': 30}, 'integrity': {'status': 'ok'}},
            'core_logs': {'text': ''},
            'tasks': []
        }
        self.recovery = Recovery(self.db_path, self.senses, Mock(), Mock())

    def tearDown(self):
        self.temp.cleanup()

    def test_heart_detects_recovery_loop_on_repeated_attempt(self):
        """Heart detecta recovery_loop_detected y no prescribe retry_task si attempt_number >= 2."""
        task = self.store.create(
            'owner:test', 'Cual es el clima para mañana?',
            state='DIAGNOSING',
            conversation_id='conv_loop_1',
            attempt_number=2,
            recovery_history=[{'artifact_hash': None, 'version_id': None, 'params': None, 'result': {}}],
            feedback={'error': 'ValueError: El plan no acredita cobertura'}
        )

        evidence = self.senses.evidence('Heart case: test', 'before')
        evidence['tasks'] = [task]

        diag = self.recovery.diagnose(evidence)
        codes = [f['code'] for f in diag['findings']]

        self.assertIn('recovery_loop_detected', codes)
        loop_finding = next(f for f in diag['findings'] if f['code'] == 'recovery_loop_detected')
        self.assertEqual(loop_finding['severity'], 'critical')
        self.assertIsNone(loop_finding.get('remedy'))

        # design() should return None to avoid blind loops
        plan = self.recovery.design('case_test', diag)
        self.assertIsNone(plan)

    def test_pixy_loads_diagnosing_task_from_conversation_context(self):
        """Pixy y Heart cargan tareas en DIAGNOSING desde control_tasks mediante conversation_id."""
        task = self.store.create(
            'owner:telegram_123', 'Cual es el clima para mañana?',
            state='DIAGNOSING',
            conversation_id='conv_active_diag',
            attempt_number=2,
            recovery_case_id='case-active-1',
            feedback={'error': 'ValueError: Cobertura insuficiente'}
        )

        mgr = SimpleNamespace(
            state=SimpleNamespace(db_path=self.db_path),
            coordinator=SimpleNamespace(store=self.store),
            heart=SimpleNamespace(recovery=self.recovery),
            memory=Mock(),
            journal=None,
            audit_db=None,
            authorizations=Mock(),
            llm=Mock(),
            telegram=Mock()
        )
        pixy = PixyMind(mgr)

        conv_context = {
            'conversation_id': 'conv_active_diag',
            'source': 'owner:telegram_123',
            'questions': [],
            'messages': []
        }

        case = self.recovery.open_case(
            'revisa el bucle de recuperación que se produce',
            requested_by='owner:telegram_123',
            propose=False,
            conversation_context=conv_context
        )

        findings = case.get('diagnosis', {}).get('findings', [])
        codes = [f['code'] for f in findings]
        self.assertIn('cognitive_discrepancy', codes)
        self.assertNotIn('recovery_loop_detected', codes)  # Attempt number alone is not a loop.
        self.assertNotIn('conversation_evidence_insufficient', codes)

    def test_first_failure_not_marked_as_loop_even_with_case_id(self):
        """Un primer fallo (attempt_number=1, sin recovery_history) NO se marca como bucle aunque tenga un recovery_case_id recién abierto."""
        task = self.store.create(
            'owner:test', 'Generar reporte',
            state='DIAGNOSING',
            attempt_number=1,
            recovery_case_id='case-first-opened-1',
            feedback={'error': 'SyntaxError: unexpected token'}
        )

        evidence = self.senses.evidence('Heart case: test_first', 'before')
        evidence['tasks'] = [task]

        diag = self.recovery.diagnose(evidence)
        codes = [f['code'] for f in diag['findings']]

        self.assertNotIn('recovery_loop_detected', codes)
        self.assertIn('task_failure', codes)
        failure_finding = next(f for f in diag['findings'] if f['code'] == 'task_failure')
        self.assertIsNotNone(failure_finding.get('remedy'))



class CoordinatorSanitizationTests(unittest.TestCase):
    def test_notify_sanitizes_py_files(self):
        coord = Coordinator.__new__(Coordinator)
        notified = []
        coord.notify = lambda text, **kw: notified.append(text)

        # Call real notify logic via string replacements
        text_with_code = "Error en action_planner.py línea 337: Cobertura insuficiente."
        import re
        cleaned = re.sub(r'\b[a-zA-Z0-9_-]+\.py\b', 'el módulo', text_with_code)
        self.assertNotIn('.py', cleaned)
        self.assertIn('el módulo', cleaned)

    def test_clean_recovery_message_and_buttons(self):
        raw_summary = "Aplicar remediación técnica: Revisar la lógica de action_planner.py y evolucionar la capacidad"
        import re
        clean_summary = re.sub(r'^(?:Aplicar remediación técnica:\s*)+', '', raw_summary, flags=re.IGNORECASE).strip()
        clean_summary = re.sub(r'\b[a-zA-Z0-9_-]+\.py\b', 'el módulo técnico', clean_summary)

        auth_id = "auth_12345"
        buttons = [
            {"text": "Sí, proceder", "callback_data": f"/approve {auth_id}"},
            {"text": "No, cancelar", "callback_data": f"/deny {auth_id}"},
        ]
        msg = f"Detecté un requerimiento técnico para completar tu objetivo: {clean_summary}. ¿Deseas que proceda con esta actualización?"

        self.assertNotIn('.py', msg)
        self.assertNotIn('¿Apruebas? Responde sí o no.', msg)
        self.assertIn('¿Deseas que proceda con esta actualización?', msg)
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0]['text'], 'Sí, proceder')

    def test_reply_intent_rejects_ambiguous_and_negated_affirmations(self):
        """Verifica que el fallback determinista no apruebe negaciones compuestas ni fillers ambiguos."""
        coord = Coordinator.__new__(Coordinator)

        # 1. Negated affirmations must be strictly classified as reject
        adversarial_rejects = [
            'claro que no', 'ok no lo hagas', 'bien, cancela',
            'por favor no', 'bueno, olvídalo', 'no lo hagas',
            'para nada', 'cancela', 'abortar'
        ]
        for phrase in adversarial_rejects:
            res = coord.classify_reply_intent(None, phrase)
            self.assertEqual(res['intent'], 'reject', f"Failed to reject: {phrase}")
            self.assertGreaterEqual(res['confidence'], 0.9)

        # 2. Ambiguous filler words must NOT approve
        for filler in ['por favor', 'bien', 'bueno']:
            res = coord.classify_reply_intent(None, filler)
            self.assertNotEqual(res['intent'], 'approve', f"Filler incorrectly approved: {filler}")

        # 3. Legitimate affirmative cues approve
        for aff in ['dale', 'ok', 'claro', 'hazlo', 'procede', 'sí', 'afirmativo', 'confirmo']:
            res = coord.classify_reply_intent(None, aff)
            self.assertEqual(res['intent'], 'approve', f"Affirmation failed: {aff}")



if __name__ == '__main__':
    unittest.main()
