import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from app.mind.coordinator import Coordinator
from app.pixy.control import ControlStore


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.started = threading.Event()
        self.release = threading.Event()

        def blocked_generate(*_args):
            self.started.set()
            self.release.wait(5)
            return "# MODE: SAFE\nprint('ok')", "SAFE", []

        manager = SimpleNamespace(
            state=SimpleNamespace(db_path=str(root / "control.sqlite3")),
            data_dir=root,
            runtime=Mock(),
            telegram=Mock(),
            memory=Mock(get_active_hypotheses=Mock(return_value=[])),
            hand=Mock(generate=blocked_generate, snapshot=Mock(return_value=(str(root / "a.py"), "a" * 64))),
            rules=Mock(active=Mock(return_value=[])),
            pixy=Mock(preflight=Mock(return_value={"safe": True})),
            mind=Mock(),
            heart=SimpleNamespace(recovery=None),
        )
        self.coordinator = Coordinator(manager)
        self.task = self.coordinator.store.create("scheduler-source", "goal")
        self.coordinator.store.update(
            self.task["id"], state="GENERATING",
            manifest={"purpose": "goal", "capabilities": []},
            deadline=time.time() + 300,
        )

    def tearDown(self):
        self.release.set()
        self.coordinator._llm_executor.shutdown(wait=True, cancel_futures=True)
        self.coordinator._mailbox_executor.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def test_background_tick_does_not_wait_for_llm_job_and_cancel_wins(self):
        started = time.monotonic()
        self.coordinator.tick(background=True)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(self.started.wait(1))

        cancelled = self.coordinator.cancel(self.task["id"], "cancel-command")
        self.assertEqual(cancelled["state"], "CANCELLED")
        self.release.set()

    def test_configured_concurrency_is_bounded(self):
        self.assertEqual(self.coordinator.llm_concurrency, 2)


if __name__ == "__main__":
    unittest.main()
