"""Unit tests for tracing, manifest normalization, and unified Pixy authorizations."""
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path

from app.common.tracing import (
    trace_scope, get_trace_id, set_trace_id, new_trace_id,
    TracingFilter, parse_log_errors
)
from app.mind.coordinator import normalize_manifest
from app.pixy.authorization import AuthorizationStore
from app.common.recovery import plan_hash


class TracingAndManifestTests(unittest.TestCase):
    def test_normalize_manifest_markdown(self):
        raw = "```json\n{\"purpose\": \"Consultar el clima de mañana\", \"capabilities\": [\"Network\"]}\n```"
        norm = normalize_manifest(raw, default_purpose="clima")
        self.assertEqual(norm["purpose"], "Consultar el clima de mañana")
        self.assertEqual(norm["capabilities"], ["Network"])

    def test_normalize_manifest_nested_and_string_capabilities(self):
        raw = "{\"manifest\": {\"purpose\": \"Buscar datos\", \"capabilities\": \"Network, FileSystem\"}}"
        norm = normalize_manifest(raw, default_purpose="buscar")
        self.assertEqual(norm["purpose"], "Buscar datos")
        self.assertIn("Network", norm["capabilities"])
        self.assertIn("FileSystem", norm["capabilities"])

    def test_normalize_manifest_fallback_on_empty(self):
        raw = "texto que no es json"
        norm = normalize_manifest(raw, default_purpose="revisar el clima")
        self.assertEqual(norm["purpose"], "revisar el clima")
        self.assertEqual(norm["capabilities"], ["Network"])  # Inferido por 'clima'

    def test_trace_scope_and_context(self):
        tid1 = new_trace_id("req_1")
        self.assertTrue(tid1.startswith("req_1-"))
        with trace_scope(tid1):
            self.assertEqual(get_trace_id(), tid1)
            tid2 = new_trace_id("sub")
            with trace_scope(tid2):
                self.assertEqual(get_trace_id(), tid2)
            self.assertEqual(get_trace_id(), tid1)
        self.assertEqual(get_trace_id(), "")

    def test_tracing_filter(self):
        record = logging.LogRecord("test", logging.INFO, "test.py", 10, "Hello", (), None)
        f = TracingFilter()
        f.filter(record)
        self.assertEqual(record.trace_id, "[-]")

        tid = "req-test-1234"
        with trace_scope(tid):
            f.filter(record)
            self.assertEqual(record.trace_id, f"[{tid}]")

    def test_parse_log_errors_with_boot_epoch(self):
        log_sample = """2026-10-25 01:00:00,100 ERROR [req-01] app.test Error antiguo
Traceback (most recent call last):
  File "test.py", line 1, in <module>
    raise ValueError("antiguo")
ValueError: antiguo
2026-10-25 01:30:00,200 ERROR [req-02] app.test Error reciente
ValueError: reciente
"""
        boot_time = time.mktime(time.strptime("2026-10-25 01:20:00", "%Y-%m-%d %H:%M:%S"))
        incidents = parse_log_errors(log_sample, boot_time=boot_time)
        self.assertEqual(len(incidents), 2)
        self.assertFalse(incidents[0]["is_active"])  # Antiguo al boot
        self.assertTrue(incidents[1]["is_active"])   # Posterior al boot
        self.assertEqual(incidents[1]["trace_id"], "req-02")


class UnifiedPixyAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "auth.sqlite3"
        self.store = AuthorizationStore(self.db, ttl=10)

    def tearDown(self):
        self.tmp.cleanup()

    def test_issue_verify_consume_by_trace(self):
        trace_id = "req-20261025-a1b2c3"
        plan = {'target': 'core', 'steps': [{'action': 'health_check', 'target': 'core'}],
                'capabilities': ['HealthCheck']}
        h = plan_hash(trace_id, plan)

        # Emitir autorización ligada al trace_id
        auth = self.store.issue(trace_id, ['HealthCheck'], 'core', h, 'user', trace_id=trace_id)
        self.assertEqual(auth['status'], 'pending')
        self.assertEqual(auth['trace_id'], trace_id)

        # Antes de aprobación, verify_by_trace debe retornar None
        self.assertIsNone(self.store.verify_by_trace(trace_id, 'HealthCheck'))

        # Usuario aprueba
        decided = self.store.decide(auth['authorization_id'], True, 'user')
        self.assertEqual(decided['status'], 'approved')

        # Ahora verify_by_trace confirma el permiso
        verified = self.store.verify_by_trace(trace_id, 'HealthCheck')
        self.assertIsNotNone(verified)
        self.assertEqual(verified['authorization_id'], auth['authorization_id'])

        # Consumir permiso
        consumed = self.store.consume_by_trace(trace_id, 'HealthCheck', consumer='hand:test')
        self.assertIsNotNone(consumed)
        self.assertEqual(consumed['status'], 'consumed')

        # Intento de segundo consumo debe retornar None (anti-replay)
        self.assertIsNone(self.store.consume_by_trace(trace_id, 'HealthCheck', consumer='hand:another'))
