import unittest
import tempfile
from unittest.mock import MagicMock
from app.mind.coordinator import humanize_worker_output, _humanize_missing_question
from app.mind.memory import MemoryEngine


class HumanInterfaceTests(unittest.TestCase):

    def test_humanize_worker_output_json_single_key(self):
        raw = '{"weather_info": "Paso 1: Weather info: 21 °C en Santiago"}'
        result = humanize_worker_output(raw)
        self.assertEqual(result, "21 °C en Santiago")

    def test_humanize_worker_output_markdown_fenced(self):
        raw = '```json\n{"result": "El pronóstico indica chubascos débiles"}\n```'
        result = humanize_worker_output(raw)
        self.assertEqual(result, "El pronóstico indica chubascos débiles")

    def test_humanize_worker_output_pipeline_prefixes(self):
        raw = "Paso 1: Paso 2: Weather info: 24 °C en Viña del Mar"
        result = humanize_worker_output(raw)
        self.assertEqual(result, "24 °C en Viña del Mar")

    def test_humanize_worker_output_weather_translations(self):
        raw = "El clima actual es de 20 °C con broken clouds y light rain"
        result = humanize_worker_output(raw)
        self.assertEqual(result, "El clima actual es de 20 °C con cielo nuboso y lluvia ligera")

    def test_humanize_worker_output_multikey_dict(self):
        raw = '{"temperatura": "21 °C", "humedad": "60%", "viento": "12 km/h"}'
        result = humanize_worker_output(raw)
        self.assertIn("• Temperatura: 21 °C", result)
        self.assertIn("• Humedad: 60%", result)
        self.assertIn("• Viento: 12 km/h", result)
        self.assertNotIn("{", result)
        self.assertNotIn("}", result)

    def test_humanize_worker_output_with_llm_synthesis(self):
        # Principio de mínimo hardcoding: el LLM sintetiza de acuerdo a la pregunta
        mock_llm = MagicMock()
        mock_llm.generate_content.return_value = "Esta semana destacan las tensiones en el G20 y la crisis en Haití."
        raw_news = '[{"title": "G20", "description": "Tensiones"}, {"title": "Haití", "description": "Crisis"}]'
        user_question = "Hazme un resumen de las noticias más importantes de la semana"
        
        result = humanize_worker_output(raw_news, request=user_question, llm=mock_llm)
        self.assertEqual(result, "Esta semana destacan las tensiones en el G20 y la crisis en Haití.")
        mock_llm.generate_content.assert_called_once()
        prompt_called = mock_llm.generate_content.call_args[0][0]
        self.assertIn("Hazme un resumen de las noticias más importantes de la semana", prompt_called)
        self.assertIn("Formatea y redacta la respuesta en español adaptándote con naturalidad a la pregunta", prompt_called)

    def test_humanize_worker_output_llm_fallback_on_error(self):
        mock_llm = MagicMock()
        mock_llm.generate_content.side_effect = RuntimeError("API error")
        raw = '{"weather_info": "Paso 1: 18 °C en Santiago"}'
        result = humanize_worker_output(raw, request="clima", llm=mock_llm)
        self.assertEqual(result, "18 °C en Santiago")

    def test_humanize_missing_question_single(self):
        q = _humanize_missing_question(['city'])
        self.assertIn("la ciudad", q)
        self.assertNotIn("¿Qué valores corresponden a city?", q)

    def test_humanize_missing_question_multiple(self):
        q = _humanize_missing_question(['city', 'date'])
        self.assertIn("la ciudad", q)
        self.assertIn("la fecha", q)

    def test_user_profile_defaults_and_update(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryEngine(data_dir=tmpdir, db_path=f"{tmpdir}/test_control.sqlite3")
            prof = mem.get_user_profile()
            self.assertEqual(prof["city"], "Santiago")
            self.assertEqual(prof["language"], "es")
            self.assertEqual(prof["units"], "metric")

            updated = mem.set_user_profile(city="Concepción")
            self.assertEqual(updated["city"], "Concepción")
            self.assertEqual(mem.get_user_profile()["city"], "Concepción")

    def test_sync_inline_preferences(self):
        from app.mind.mind import _sync_inline_preferences
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryEngine(data_dir=tmpdir, db_path=f"{tmpdir}/test_control.sqlite3")
            _sync_inline_preferences("Yo soy cáncer, recuérdalo y dame mi horóscopo", mem)
            self.assertEqual(mem.get_user_profile()["sign"], "cáncer")
            self.assertEqual(mem.get_preference("user_sign"), "cáncer")

            _sync_inline_preferences("Me llamo Carlos y vivo en Valparaíso", mem)
            self.assertEqual(mem.get_user_profile()["name"], "Carlos")
            self.assertEqual(mem.get_user_profile()["city"], "Valparaíso")

    def test_pixy_root_governance_personality(self):
        from app.pixy.pixy import PixyMind
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryEngine(data_dir=tmpdir, db_path=f"{tmpdir}/test_control.sqlite3")
            mgr = SimpleNamespace(llm=MagicMock(), telegram=MagicMock(), memory=mem, journal=None, audit_db=None, rules=None, coordinator=None)
            pixy = PixyMind(mgr)
            
            # Propose personality change
            reply = pixy.direct_response("cambiar personalidad a: Eres un asistente socrático y analítico.")
            self.assertIn("Nivel Root - Personalidad", reply)
            self.assertIn("Eres un asistente socrático y analítico.", reply)
            self.assertIsNotNone(pixy.get_pending_root_proposal())

            # Approve change
            confirm = pixy.direct_response("y")
            self.assertIn("Modificación Root aprobada", confirm)
            self.assertIsNone(pixy.get_pending_root_proposal())
            self.assertIn("Eres un asistente socrático y analítico.", mem.get_section("PERSONALITY"))

            # Cancel scenario
            pixy.direct_response("cambiar personalidad a: Nueva personalidad que será cancelada")
            cancel = pixy.direct_response("n")
            self.assertIn("cancelada por el usuario", cancel)
            self.assertIsNone(pixy.get_pending_root_proposal())
            self.assertNotIn("cancelada", mem.get_section("PERSONALITY"))

    def test_pixy_root_governance_guideline(self):
        from app.pixy.pixy import PixyMind
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryEngine(data_dir=tmpdir, db_path=f"{tmpdir}/test_control.sqlite3")
            mgr = SimpleNamespace(llm=MagicMock(), telegram=MagicMock(), memory=mem, journal=None, audit_db=None, rules=None, coordinator=None)
            pixy = PixyMind(mgr)

            reply = pixy.direct_response("regla de comportamiento: Prioriza respuestas en formato conciso.")
            self.assertIn("Nivel Root - Directiva", reply)
            self.assertIn("Prioriza respuestas en formato conciso.", reply)

            confirm = pixy.direct_response("y")
            self.assertIn("Directiva Root aprobada", confirm)
            guidelines = mem.get_dynamic_guidelines()
            self.assertIn("Prioriza respuestas en formato conciso.", guidelines)

    def test_coordinator_set_user_preference(self):
        from app.mind.coordinator import Coordinator
        from app.mind.conversation import ConversationStore
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            conversations = ConversationStore(db_path)
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path), voice_services=None, heart=MagicMock())
            coord = Coordinator(mgr, MagicMock())
            task = {'id': 'task_1', 'source': 'owner:test', 'conversation_id': 'conv_1', 'request': 'guardar preferencia'}
            call = {'name': 'set_user_preference', 'arguments': {'key': 'sign', 'value': 'cáncer'}}
            coord.conversations.begin('owner:test', 'trace-test', 'guardar preferencia', 'owner', 'owner')
            reply = coord.dispatch_tool(task, call)
            self.assertIn("cáncer", reply)
            self.assertEqual(mem.get_user_profile()["sign"], "cáncer")
            self.assertEqual(mem.get_preference("user_sign"), "cáncer")

    def test_read_only_task_skips_input_confirmation(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            active_svcs = {
                'brave': {
                    'operations': {
                        'search': {'effect': 'read', 'version': 'v1'}
                    }
                }
            }
            voice_services = SimpleNamespace(active_services=lambda: active_svcs)
            proxy = SimpleNamespace(submit=MagicMock())
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=voice_services, voice_credentials=SimpleNamespace(status=lambda k: {'available': True}))
            coord = Coordinator(mgr, proxy)

            import hashlib
            art_code = b"print('result')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            with open(art_path, "wb") as f:
                f.write(art_code)

            task = store.create(
                'owner:test_read', 'busca horoscopo',
                state='READY',
                mode='SAFE',
                dependencies=[],
                services=[{'service_id': 'brave', 'operation': 'search', 'version': 'v1'}],
                artifact=art_path,
                artifact_hash=art_hash,
                authorized_hash=art_hash,
                input_confirmation_required=True,
                params={'query': 'horoscopo cancer hoy'}
            )
            coord.advance(task)
            updated = store.get(task['id'])
            self.assertNotEqual(updated['state'], 'WAITING_INPUT_CONFIRMATION')
            self.assertEqual(updated['state'], 'SUBMITTED')



    def test_natural_language_scope_reply_no_amnesia(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock())
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_weather')
            task = store.create(
                'owner:user1', 'va a llover este fin de semana?',
                state='WAITING_SCOPE',
                conversation_id=sid,
                scope_questions=['¿De qué ciudad necesitas el pronóstico?'],
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'scope', task['id'], task['revision'], 'WAITING_SCOPE')
            coord.conversations.delivered(sid, 'deliv_scope_1', '¿De qué ciudad necesitas el pronóstico?', question_id=qid)
            
            # User replies with natural language "santiago" (no UUID, no y/n)
            res = coord.handle_reply('owner:user1', 'santiago', sid)
            self.assertTrue(res.handled)
            updated = store.get(task['id'])
            self.assertEqual(updated['state'], 'RESOLVING_SCOPE')
            self.assertEqual(len(updated['scope_answers']), 1)
            self.assertEqual(updated['scope_answers'][0]['answer'], 'santiago')

    def test_natural_language_approval_responses(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock())
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_approval')
            import hashlib
            from pathlib import Path
            art_code = b"print('run')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            Path(art_path).write_bytes(art_code)

            task = store.create(
                'owner:user1', 'ejecutar accion',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                artifact=art_path,
                artifact_hash=art_hash,
                approval={'token': 'tok1', 'operation': None, 'expires': time.time() + 60, 'artifact_hash': art_hash},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_app_1', '¿Apruebas?', question_id=qid)

            # "dale" should be accepted as affirmative
            res = coord.handle_reply('owner:user1', 'dale', sid)
            self.assertTrue(res.handled)
            print('ACTUAL_RES_TEXT:', repr(res.text), 'HANDLED:', res.handled)
            self.assertEqual(res.text, 'Ejecución autorizada.')

    def test_mixed_reply_approval_revises_goal(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock())
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_mixed')
            task = store.create(
                'owner:user1', 'clima en santiago',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                approval={'token': 'tok2', 'operation': None},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_mixed_1', '¿Apruebas?', question_id=qid)

            # Mixed reply: "sí, y además dame el viento"
            res = coord.handle_reply('owner:user1', 'sí, y además dame el viento', sid)
            self.assertTrue(res.handled)
            updated = store.get(task['id'])
            self.assertEqual(updated['state'], 'RESOLVING_SCOPE')
            self.assertEqual(updated['scope_answers'][-1]['answer'], 'sí, y además dame el viento')

    def test_cognitive_discrepancy_reporting_to_heart(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mock_heart = MagicMock()
            mock_heart.recovery.open_incident_case.return_value = {'case_id': 'case-test-cog-01'}
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=mock_heart)
            coord = Coordinator(mgr, MagicMock())
            task = store.create(
                'owner:user1', 'temperatura actual',
                state='COMPLETED',
                skill_name='clima-santiago-openweather',
                result={'stdout': '289.12 K'}
            )
            
            # Dispatch report_issue
            args = {'issue': 'No me vuelvas a dar grados Kelvin, dámelo en Celsius', 'task_id': task['id']}
            reply = coord.report_cognitive_issue('owner:user1', args, task=task)
            self.assertIn("clima-santiago-openweather", reply)
            self.assertIn("case-test-cog-01", reply)
            mock_heart.recovery.open_incident_case.assert_called_once()
            called_info = mock_heart.recovery.open_incident_case.call_args[0][1]
            self.assertEqual(called_info['cause_code'], 'cognitive_discrepancy')
            self.assertEqual(called_info['skill_name'], 'clima-santiago-openweather')

    def test_action_planner_skill_evolution(self):
        from app.mind.action_planner import ActionPlanner
        from types import SimpleNamespace
        import json
        active_skill = {
            'skill_id': 'clima-santiago-openweather',
            'version_id': 'ver_clima_v1',
            'spec': {
                'skill_id': 'clima-santiago-openweather',
                'description': 'Obtiene la temperatura actual de Santiago',
                'capabilities': ['Network'],
                'input_schema': {'type': 'object', 'properties': {'city': {'type': 'string'}}},
                'output_schema': {'type': 'object', 'properties': {'temp': {'type': 'number'}}}
            }
        }
        catalog = SimpleNamespace(active=lambda: [active_skill])
        # Planner proposes evolving clima-santiago-openweather to add wind
        llm = SimpleNamespace(generate_content=MagicMock(return_value=json.dumps({
            'steps': [{
                'id': 'clima_evolved',
                'use_existing_skill': None,
                'new_skill': {
                    'skill_id': 'clima-santiago-openweather',
                    'description': 'Obtiene temperatura y viento en unidades métricas',
                    'capabilities': ['Network'],
                    'inputs': {'city': 'string'},
                    'outputs': {'temp': 'number', 'wind': 'string'},
                }
            }],
            'inputs': {'city': 'Santiago'}
        })))
        proposal_json = llm.generate_content.return_value
        llm.generate_content.side_effect = [proposal_json, json.dumps({"sufficient": True, "missing": []})]
        planner = ActionPlanner(llm, catalog)
        proposal = planner.propose('Va a haber viento en Santiago?', lambda spec: True)
        self.assertIsNotNone(proposal)
        missing_action = proposal['missing_specs']['clima_evolved']
        self.assertEqual(missing_action['skill_id'], 'clima-santiago-openweather')
        self.assertTrue(missing_action.get('is_evolution'))
        self.assertEqual(missing_action.get('previous_version_id'), 'ver_clima_v1')

    def test_clean_worker_output_no_coletilla(self):
        # Humanized output delivers clean text without (y/n) robotic suffix
        raw = "Temperatura: 22 °C, Soleado"
        result = humanize_worker_output(raw)
        self.assertEqual(result, "Temperatura: 22 °C, Soleado")
        self.assertNotIn("¿Es este el resultado esperado?", result)
        self.assertNotIn("(y/n)", result)


    def test_dynamic_intent_classification_approval(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import hashlib
        import json
        from pathlib import Path
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            class DynamicLLM:
                def __init__(self, intent):
                    self.intent = intent
                    self.calls = 0
                def generate_content(self, prompt):
                    self.calls += 1
                    return json.dumps({"intent": self.intent})

            llm_inst = DynamicLLM("approve")
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock(), llm=llm_inst)
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_dyn_app')
            art_code = b"print('ok')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            Path(art_path).write_bytes(art_code)

            task = store.create(
                'owner:user1', 'ejecutar accion',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                artifact=art_path,
                artifact_hash=art_hash,
                approval={'token': 'tok1', 'operation': None, 'expires': time.time() + 60, 'artifact_hash': art_hash},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_1', '¿Apruebas?', question_id=qid)

            res = coord.handle_reply('owner:user1', 'me parece una excelente idea, dale adelante', sid)
            self.assertTrue(res.handled)
            self.assertEqual(res.text, 'Ejecución autorizada.')
            self.assertGreater(llm_inst.calls, 0)

    def test_dynamic_intent_classification_rejection(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import hashlib
        import json
        from pathlib import Path
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            class DynamicLLM:
                def __init__(self, intent):
                    self.intent = intent
                    self.calls = 0
                def generate_content(self, prompt):
                    self.calls += 1
                    return json.dumps({"intent": self.intent})

            llm_inst = DynamicLLM("reject")
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock(), llm=llm_inst)
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_dyn_rej')
            art_code = b"print('ok')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            Path(art_path).write_bytes(art_code)

            task = store.create(
                'owner:user1', 'ejecutar accion',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                artifact=art_path,
                artifact_hash=art_hash,
                approval={'token': 'tok2', 'operation': None, 'expires': time.time() + 60, 'artifact_hash': art_hash},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_2', '¿Apruebas?', question_id=qid)

            res = coord.handle_reply('owner:user1', 'la verdad es que prefiero que no lo hagas', sid)
            self.assertTrue(res.handled)
            self.assertEqual(res.text, 'Ejecución cancelada.')
            self.assertGreater(llm_inst.calls, 0)

    def test_dynamic_intent_classification_unrelated_leaves_turn(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import hashlib
        import json
        from pathlib import Path
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            class DynamicLLM:
                def __init__(self, intent):
                    self.intent = intent
                    self.calls = 0
                def generate_content(self, prompt):
                    self.calls += 1
                    return json.dumps({"intent": self.intent})

            llm_inst = DynamicLLM("unrelated")
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock(), llm=llm_inst)
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_dyn_unrelated')
            art_code = b"print('ok')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            Path(art_path).write_bytes(art_code)

            task = store.create(
                'owner:user1', 'ejecutar accion',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                artifact=art_path,
                artifact_hash=art_hash,
                approval={'token': 'tok3', 'operation': None, 'expires': time.time() + 60, 'artifact_hash': art_hash},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_3', '¿Apruebas?', question_id=qid)

            res = coord.handle_reply('owner:user1', 'además busca noticias de hoy', sid)
            self.assertFalse(res.handled)

    def test_telegraphic_fast_path_bypasses_llm(self):
        from app.mind.coordinator import Coordinator
        from app.pixy.control import ControlStore
        from types import SimpleNamespace
        import hashlib
        import json
        from pathlib import Path
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/test_control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            class DynamicLLM:
                def __init__(self):
                    self.calls = 0
                def generate_content(self, prompt):
                    self.calls += 1
                    return json.dumps({"intent": "approve"})

            llm_inst = DynamicLLM()
            mgr = SimpleNamespace(memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                                  voice_services=None, heart=MagicMock(), llm=llm_inst)
            coord = Coordinator(mgr, MagicMock())
            sid = coord.conversations.session('owner', 'conv_fast_path')
            art_code = b"print('ok')"
            art_hash = hashlib.sha256(art_code).hexdigest()
            art_path = f"{tmpdir}/art.py"
            Path(art_path).write_bytes(art_code)

            task = store.create(
                'owner:user1', 'ejecutar accion',
                state='WAITING_APPROVAL',
                conversation_id=sid,
                artifact=art_path,
                artifact_hash=art_hash,
                approval={'token': 'tok4', 'operation': None, 'expires': time.time() + 60, 'artifact_hash': art_hash},
                deadline=time.time() + 1800
            )
            qid = coord.conversations.question(sid, 'execution', task['id'], task['revision'], 'WAITING_APPROVAL')
            coord.conversations.delivered(sid, 'deliv_4', '¿Apruebas?', question_id=qid)

            res = coord.handle_reply('owner:user1', 'y', sid)
            self.assertTrue(res.handled)
            self.assertEqual(res.text, 'Ejecución autorizada.')
            self.assertEqual(llm_inst.calls, 0)

    def test_skill_canonical_version_format_and_archive(self):
        from app.hand.hand import Hand
        from app.pixy.pixy import PixyMind
        from app.kernel.events import Events
        from pathlib import Path
        from types import SimpleNamespace
        import json
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            events = Events(root / 'events.sqlite3')
            mock_llm = MagicMock()
            mock_llm.generate_content.return_value = json.dumps({'safe': True, 'requires_approval': False})
            pixy = PixyMind(SimpleNamespace(llm=mock_llm, telegram=MagicMock(), audit_db=root / 'pixy_audit.sqlite3'))
            hand = Hand(root / 'skills', pixy, events, str(root / 'hand.sqlite3'))

            code = '# MODE: SAFE\ndef main():\n    return 42\n'
            manifest = {'purpose': 'compute', 'capabilities': []}

            # Version 1 -> .001
            v1 = hand.create('calc-tool', code, manifest)
            self.assertEqual(v1['version_number'], '.001')
            hand.approve(v1['version_id'], user_approved=True)

            # Version 2 -> .002
            v2 = hand.create('calc-tool', code + '\n# v2', manifest)
            self.assertEqual(v2['version_number'], '.002')
            hand.approve(v2['version_id'], user_approved=True)

            # Verify v1 archive has format calc-tool.001.zip
            v1_row = hand.version(v1['version_id'])
            self.assertEqual(v1_row['status'], 'archived')
            self.assertEqual(v1_row['archive_name'], 'calc-tool.001.zip')
            self.assertTrue((root / 'skills' / 'calc-tool.001.zip').exists())

            # Rollback to v1 creates version .003 with restored_from pointing to v1
            v3 = hand.rollback(v1['version_id'], user_approved=True)
            self.assertEqual(v3['version_number'], '.003')
            self.assertEqual(v3['status'], 'active')
            self.assertEqual(v3['restored_from_version_id'], v1['version_id'])


if __name__ == '__main__':
    unittest.main()
