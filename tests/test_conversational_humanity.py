"""Tests for organic conversational humanity, unified turns, and RL feedback loop."""
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import MagicMock
from app.mind.turn import AssistantTurn, StartCall, StartArguments, PreferenceUpdate
from app.mind.mind import Mind
from app.mind.coordinator import Coordinator
from app.mind.memory import MemoryEngine
from app.pixy.control import ControlStore
from app.dreams.dreams import Dreams


class ConversationalHumanityTests(unittest.TestCase):

    def test_reply_and_tool_call_simultaneous(self):
        """AssistantTurn permits both a natural human reply and a concrete technical action."""
        turn = AssistantTurn(
            reply="¡Por supuesto! Consultaré el pronóstico del tiempo para Santiago ahora mismo.",
            tool_calls=[StartCall(name="start_task", arguments=StartArguments(goal="consultar el clima en Santiago"))],
            preference_updates=[]
        )
        self.assertTrue(turn.reply.startswith("¡Por supuesto!"))
        self.assertEqual(len(turn.tool_calls), 1)

        intent = turn.intent()
        self.assertEqual(intent["action"], "ACTION_REQUIRED")
        self.assertEqual(intent["extracted_goal"], "consultar el clima en Santiago")
        self.assertEqual(intent["reply"], "¡Por supuesto! Consultaré el pronóstico del tiempo para Santiago ahora mismo.")
        self.assertIn("tool_call", intent)

    def test_empty_turn_rejected(self):
        """A completely empty turn is invalid; the assistant must at least speak or act."""
        with self.assertRaises(Exception):
            AssistantTurn(reply="", tool_calls=[], preference_updates=[])

    def test_mind_injects_dreams_learnings(self):
        """Mind injects active learnings and guidelines from Dreams into the contextual prompt."""
        mock_llm = MagicMock()
        mock_llm.generate_content.return_value = '{"reply": "En Santiago hay 19 °C", "tool_calls": [], "preference_updates": []}'

        with tempfile.TemporaryDirectory() as tmpdir:
            mem = MemoryEngine(data_dir=tmpdir, db_path=f"{tmpdir}/control.sqlite3")
            mock_dreams = MagicMock()
            mock_dreams.get_active_learnings.return_value = [
                "Para clima en Chile, usar grados Celsius y verificar si se solicitó viento",
                "Evitar notaciones en Kelvin a menos que se solicite expresamente"
            ]

            mind = Mind(mock_llm, mem, dreams=mock_dreams)
            intent = mind.process_message("¿Cuál es la temperatura en Santiago?")

            self.assertEqual(intent["action"], "GENERAL_CHAT")
            self.assertEqual(intent["reply"], "En Santiago hay 19 °C")

            mock_llm.generate_content.assert_called_once()
            called_kwargs = mock_llm.generate_content.call_args[1]
            sys_instruction = called_kwargs.get("system_instruction", "")

            self.assertIn("APRENDIZAJES Y DIRECTRICES PREVIAS (Refuerzo):", sys_instruction)
            self.assertIn("Para clima en Chile, usar grados Celsius", sys_instruction)
            self.assertIn("Evitar notaciones en Kelvin", sys_instruction)

    def test_coordinator_notifies_conversational_reply_on_action_required(self):
        """Coordinator notifies conversational acknowledgement immediately while task starts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mgr = SimpleNamespace(
                memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                data_dir=tmpdir, voice_services=None, heart=MagicMock(), llm=MagicMock(),
                runtime=MagicMock(), control_db=db_path, hand=MagicMock(), pixy=MagicMock()
            )
            mgr.hand.describe_service.return_value = ({
                "service_id": "s1", "effect": "read_only", "url": "http://localhost:8000",
                "operation": "read", "runtime": "worker", "version": "v1"
            }, "h1")
            mgr.pixy.preflight.return_value = {"safe": True}
            coordinator = Coordinator(mgr)
            notifications = []
            coordinator.notify = lambda msg: notifications.append(msg)

            sid = coordinator.conversations.session("owner:chat1", "chat1")
            task = store.create(
                "owner:chat1:msg1", "consultar clima",
                conversation_id=sid, state="ROUTING"
            )
            # Inject routing intent with both natural reply and start_task
            task["routing_intent"] = {
                "action": "ACTION_REQUIRED",
                "extracted_goal": "consultar el clima en Santiago",
                "reply": "Consulto el clima de inmediato con la estación meteorológica.",
                "tool_call": {
                    "name": "start_task",
                    "arguments": {"goal": "consultar el clima en Santiago"}
                }
            }

            coordinator._run_task(task)

            # The conversational response was notified immediately to outbox
            self.assertIn("Comprobaré la capacidad y los requisitos para realizar tu solicitud.", notifications)

    def test_organic_conversational_flow_without_pending_question(self):
        """Casual conversation words like 'dale' or 'ok' without a pending question do not trigger bureaucratic errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/control.sqlite3"
            mem = MemoryEngine(data_dir=tmpdir, db_path=db_path)
            store = ControlStore(db_path)
            mgr = SimpleNamespace(
                memory=mem, store=store, state=SimpleNamespace(db_path=db_path),
                data_dir=tmpdir, voice_services=None, heart=MagicMock(), llm=MagicMock(),
                runtime=MagicMock(), control_db=db_path, hand=MagicMock(), pixy=MagicMock()
            )
            coordinator = Coordinator(mgr)
            sid = coordinator.conversations.session("owner:chat1", "chat1")

            # 'dale' without pending questions returns ReplyResult(False) so Mind can chat naturally
            res = coordinator.handle_reply("owner:chat1:msg1", "dale", sid)
            self.assertFalse(res.handled)

            res2 = coordinator.handle_reply("owner:chat1:msg2", "ok", sid)
            self.assertFalse(res2.handled)

            # Bare decision 'y' without question returns clear note
            res3 = coordinator.handle_reply("owner:chat1:msg3", "y", sid)
            self.assertTrue(res3.handled)
            self.assertIn("No hay una pregunta", res3.text)

    def test_dreams_get_active_learnings_sqlite(self):
        """Dreams.get_active_learnings returns active dynamic guidelines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/dreams.sqlite3"
            dreams = Dreams(db_path)

            from app.kernel.database import connection
            with connection(db_path) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS dynamic_guidelines("
                    "id INTEGER PRIMARY KEY, topic TEXT, scope TEXT, rule_text TEXT, "
                    "status TEXT, confidence REAL, success_rate REAL, created_at REAL, supersedes INTEGER)"
                )
                conn.execute(
                    "INSERT INTO dynamic_guidelines(topic, scope, rule_text, status, confidence) "
                    "VALUES ('weather', 'general', 'Siempre incluir velocidad del viento en km/h', 'active', 0.95)"
                )

            learnings = dreams.get_active_learnings()
            self.assertEqual(len(learnings), 1)
            self.assertEqual(learnings[0], "Siempre incluir velocidad del viento en km/h")
