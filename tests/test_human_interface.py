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


if __name__ == '__main__':
    unittest.main()
