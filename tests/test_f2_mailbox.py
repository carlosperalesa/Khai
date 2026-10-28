"""Durability, deduplication and scheduling at Voice's external boundary."""
import tempfile
import threading
import unittest
from pathlib import Path

from app.voice.mailbox import VoiceMailbox


class VoiceMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'control.sqlite3'
        self.mailbox = VoiceMailbox(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_update_is_deduplicated_and_different_body_rejected(self):
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-7', '/make one'), 'pending')
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-other', '/make one'), 'pending')
        with self.assertRaises(ValueError):
            self.mailbox.accept('telegram:7', 'trace-7', '/make two')
        called = []
        self.mailbox.process_one(lambda source, trace, text: called.append((source, text)))
        self.assertEqual(called, [('telegram:7', '/make one')])
        self.assertEqual(self.mailbox.accept('telegram:7', 'trace-7', '/make one'), 'completed')

    def test_urgent_control_runs_while_normal_handler_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        self.mailbox.accept('telegram:1', 'trace-1', '/p diagnóstico lento')
        self.mailbox.accept('telegram:2', 'trace-2', '/cancel task-1', urgent=True)
        def slow(*_):
            entered.set()
            release.wait(2)
        thread = threading.Thread(target=lambda: self.mailbox.process_one(slow))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            actions = []
            self.assertTrue(self.mailbox.process_one(lambda source, *_: actions.append(source), urgent=True))
            self.assertEqual(actions, ['telegram:2'])
        finally:
            release.set()
            thread.join(2)

    def test_uncertain_handler_and_telegram_delivery_are_not_replayed(self):
        self.mailbox.accept('telegram:3', 'trace-3', '/publish asset')
        with self.assertRaises(RuntimeError):
            self.mailbox.process_one(lambda *_: (_ for _ in ()).throw(RuntimeError('lost reply')))
        self.assertFalse(self.mailbox.process_one(lambda *_: self.fail('replayed command')))
        delivery = self.mailbox.enqueue('resultado')
        with self.assertRaises(RuntimeError):
            self.mailbox.deliver_one(lambda _: (_ for _ in ()).throw(RuntimeError('lost receipt')))
        self.assertFalse(self.mailbox.deliver_one(lambda _: self.fail('replayed delivery')))
        self.assertEqual(self.mailbox.pending_counts()['outbox']['unknown'], 1)
        self.assertEqual(self.mailbox.pending_counts()['inbox']['unknown'], 1)
        self.assertTrue(delivery.startswith('delivery-'))


if __name__ == '__main__':
    unittest.main()
