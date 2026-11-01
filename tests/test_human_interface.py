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


if __name__ == '__main__':
    unittest.main()
