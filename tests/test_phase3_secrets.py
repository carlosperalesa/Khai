import tempfile
import unittest
from pathlib import Path

from app.kernel.control_plane import ControlPlane
from app.voice.secrets import SecretCaptureService, Secrets


class SecretCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "control.sqlite3"
        self.secrets = Secrets(self.root)
        self.plane = ControlPlane(self.db)
        self.capture = SecretCaptureService(self.secrets, self.plane.voice_secrets)

    def tearDown(self):
        self.tmp.cleanup()

    def test_capture_is_bound_to_private_identity_and_is_not_persisted_in_control_store(self):
        request = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="GEMINI_API_KEY", provider="gemini",
            purpose="Proveedor LLM", user_id="owner", chat_id="chat",
        )
        receipt = self.capture.capture("/api", "marker-value", "owner", "chat")
        self.assertTrue(receipt.startswith("secret_"))
        self.assertEqual(self.secrets.get("GEMINI_API_KEY"), "marker-value")
        with self.assertRaises(PermissionError):
            self.capture.capture("/api", "other-value", "owner", "chat")
        self.assertNotIn("marker-value", str(self.plane.voice_secrets.outbox()))
        self.assertNotIn("marker-value", request["request_id"])

    def test_ambiguous_or_wrong_identity_does_not_select_a_request(self):
        self.plane.ports.pixy.request_secret(
            kind="password", env_key="SERVICE_PASSWORD", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.plane.ports.pixy.request_secret(
            kind="password", env_key="OTHER_PASSWORD", provider="other",
            purpose="Otro servicio", user_id="owner", chat_id="chat",
        )
        with self.assertRaises(PermissionError):
            self.capture.capture("/pas", "value", "owner", "chat")
        with self.assertRaises(PermissionError):
            self.capture.capture("/pas", "value", "attacker", "chat")

    def test_rotation_uses_expected_file_version(self):
        first = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="OPENAI_API_KEY", provider="openai",
            purpose="Proveedor", user_id="owner", chat_id="chat",
        )
        self.capture.capture("/api", "first", "owner", "chat")
        second = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="OPENAI_API_KEY", provider="openai",
            purpose="Rotación", user_id="owner", chat_id="chat", expected_version=1,
        )
        self.capture.capture("/api", "second", "owner", "chat")
        self.assertEqual(self.secrets.get("OPENAI_API_KEY"), "second")
        self.assertEqual(self.plane.voice_secrets.outbox()[0]["request_id"], first["request_id"])
        self.assertEqual(len(self.plane.voice_secrets.outbox()), 2)

    def test_outbox_receipt_can_be_delivered_and_acknowledged_after_restart(self):
        self.plane.ports.pixy.request_secret(
            kind="username", env_key="SERVICE_USER", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.capture.capture("/us", "carlos", "owner", "chat")
        delivered = []
        self.capture.flush(delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.plane.voice_secrets.outbox(), [])

    def test_restart_reconciles_write_before_receipt_without_resending_value(self):
        request = self.plane.ports.pixy.request_secret(
            kind="api_key", env_key="RECONCILE_API_KEY", provider="service",
            purpose="Servicio", user_id="owner", chat_id="chat",
        )
        self.plane.voice_secrets.capture(request["request_id"], "owner", "chat", "api_key")
        self.secrets.put("RECONCILE_API_KEY", "marker", 0)
        receipt = self.capture.capture("/api", "not-used", "owner", "chat")
        self.assertTrue(receipt.startswith("secret_"))
        self.assertEqual(len(self.plane.voice_secrets.outbox()), 1)


if __name__ == "__main__":
    unittest.main()
