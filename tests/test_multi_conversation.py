import unittest
from unittest.mock import MagicMock, Mock, patch

from tests.test_control import Fixture
from app.mind.conversation import current_turn
from app.voice.voice import command, process_ordinary, urgent_command
from app.voice.help import HELP_BUTTONS, HELP_TEXT


class TestMultiConversation(Fixture):
    def setUp(self):
        super().setUp()
        self.owner = 'owner_123'
        self.chat = self.owner
        self.manager.telegram.user_id = self.owner
        self.sid = self.c.conversations.session(self.owner, self.chat)

    def tearDown(self):
        self.c._llm_executor.shutdown(wait=True)
        self.c._mailbox_executor.shutdown(wait=True)
        super().tearDown()

    def test_urgent_command_classification(self):
        """Conversation commands must be classified as urgent to avoid queue delays."""
        for cmd in ['/c', '/chats', '/new', '/clear', '/delete']:
            self.assertTrue(urgent_command(cmd), f"{cmd} should be urgent")
            self.assertTrue(urgent_command(f"{cmd} 2"), f"{cmd} with args should be urgent")
        self.assertFalse(urgent_command('/archive'))

    def test_help_buttons_and_text(self):
        self.assertEqual([button['label'] for button in HELP_BUTTONS[0]],
                         ['Mode low', 'Mode medium', 'Mode high'])
        self.assertEqual(HELP_BUTTONS[1][0]['label'], 'Ver modo actual')
        self.assertEqual(HELP_BUTTONS[2][0]['label'], 'Mis Chats')
        self.assertNotIn('/archive', HELP_TEXT)
        self.assertIn('/delete [número]', HELP_TEXT)

    def test_new_and_list_conversations_with_buttons(self):
        """Test creating multiple conversations and listing with interactive inline buttons."""
        # Initial conversation
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Chat Principal')
        self.assertEqual(active[0]['is_current'], 1)

        # Create a second conversation
        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Análisis de rendimiento')
        self.assertIsNotNone(c2)
        self.assertEqual(c2['title'], 'Análisis de rendimiento')
        self.assertEqual(c2['is_current'], 1)

        # Check list: should have 2 active conversations, c2 is current
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 2)
        self.assertEqual(active[0]['id'], c2['id'])
        self.assertEqual(active[0]['is_current'], 1)
        self.assertEqual(active[1]['is_current'], 0)

        # Invoke /chats via command
        res = command(self.manager, 'telegram:msg1', '/chats')
        self.assertIsInstance(res, dict)
        self.assertIn('text', res)
        self.assertIn('buttons', res)

        # Check text format (zero emojis)
        text = res['text']
        self.assertIn('Conversaciones activas:', text)
        self.assertIn('1. [Actual] Análisis de rendimiento', text)
        self.assertIn('2. Chat Principal', text)

        # Check buttons matrix
        buttons = res['buttons']
        self.assertEqual(len(buttons), 3)  # row 1 (chat 1), row 2 (chat 2), row 3 (actions)
        self.assertEqual(buttons[0][0]['value'], '/c 1')
        self.assertIn('[Actual]', buttons[0][0]['label'])
        self.assertEqual(buttons[1][0]['value'], '/c 2')
        self.assertEqual(buttons[2][0]['value'], '/new')
        self.assertEqual(buttons[2][1]['value'], '/clear')
        self.assertEqual(buttons[2][2]['value'], '/delete')

    def test_switch_conversation(self):
        """Test switching conversations via /c <index> and /c <name>."""
        c1_id = self.sid
        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Docker logs')
        
        # Currently on c2
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c2['id'])

        # Switch to conversation 2 (which is c1)
        res = command(self.manager, 'telegram:msg2', '/c 2')
        self.assertIn('Chat activo: Chat Principal', res)
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c1_id)

        # Switch by title substring
        res = command(self.manager, 'telegram:msg3', '/c docker')
        self.assertIn('Chat activo: Docker logs', res)
        self.assertEqual(self.c.conversations.session(self.owner, self.chat), c2['id'])

        # Switch to non-existent
        res = command(self.manager, 'telegram:msg4', '/c fantasma')
        self.assertIn('No se encontró la conversación', res)

    def test_switching_does_not_renumber_conversations(self):
        """Stable numeric commands must keep pointing at the listed conversation."""
        second = self.c.conversations.new_conversation(self.owner, self.chat, title='Segundo')
        before = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual([c['title'] for c in before], ['Segundo', 'Chat Principal'])

        self.c.conversations.switch_conversation(self.owner, self.chat, '2')
        after = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual([c['id'] for c in after], [c['id'] for c in before])
        self.assertEqual(after[0]['id'], second['id'])

    def test_clear_and_delete_current_conversation(self):
        """Test /clear and immediate deletion of the current chat."""
        # /clear archives current and creates fresh one
        res = command(self.manager, 'telegram:msg5', '/clear')
        self.assertIn('Conversación reiniciada.', res)

        # List should show only the fresh one as active
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Nuevo chat')

        deleted_id = active[0]['id']
        res = command(self.manager, 'telegram:msg6', '/delete')
        self.assertEqual(res['text'], 'Chat eliminado: Nuevo chat')
        self.assertIsNone(self.c.conversations.get_conversation(deleted_id))
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['title'], 'Chat Principal')

    def test_delete_conversation_by_number_only(self):
        self.c.conversations.new_conversation(self.owner, self.chat, title='Proyecto largo')
        active = self.c.conversations.list_conversations(self.owner, self.chat)
        target = active[1]
        res = command(self.manager, 'telegram:delete-number', '/delete 2')
        self.assertEqual(res['text'], f"Chat eliminado: {target['title']}")
        self.assertIsNone(self.c.conversations.get_conversation(target['id']))
        self.assertEqual(command(self.manager, 'telegram:delete-name', '/delete Proyecto largo'),
                         'Uso: /delete [número]')

    def test_delete_rejects_conversation_with_active_task(self):
        current = self.c.conversations.list_conversations(self.owner, self.chat)[0]
        self.store.create('telegram:active-delete', 'trabajo',
                          conversation_id=current['id'], state='RUNNING')
        result = command(self.manager, 'telegram:delete-active', '/delete')
        self.assertIn('tarea activa', result)
        self.assertIsNotNone(self.c.conversations.get_conversation(current['id']))

    def test_delete_response_uses_replacement_conversation(self):
        doomed = self.c.conversations.new_conversation(
            self.owner, self.chat, title='Temporal')
        source = 'telegram:delete-routing'
        self.c.conversations.begin(source, 'trace-delete', '/delete', self.owner, self.chat)
        captured = {}

        def capture(text, buttons=None):
            captured.update(text=text, turn=dict(current_turn.get() or {}))

        with patch.object(self.c, 'notify', side_effect=capture):
            process_ordinary(self.manager, source, 'trace-delete', '/delete')

        active = self.c.conversations.list_conversations(self.owner, self.chat)[0]
        self.assertIsNone(self.c.conversations.get_conversation(doomed['id']))
        self.assertIsNone(self.c.conversations.turn(source))
        self.assertEqual(captured['text'], 'Chat eliminado: Temporal')
        self.assertEqual(captured['turn']['conversation_id'], active['id'])
        self.assertEqual(captured['turn']['title'], active['title'])

    def test_message_history_isolation(self):
        """Verify message history does not leak between distinct conversation sessions."""
        # Session 1: Send user message and assistant reply
        turn1 = self.c.conversations.begin('telegram:t1', 'trace1', 'Mensaje secreto en sesion 1', self.owner, self.chat)
        s1_id = turn1['conversation_id']
        self.c.conversations.delivered(s1_id, 'deliv-1', 'Respuesta secreta en sesion 1')

        history1 = self.c.conversations.history(s1_id)
        self.assertEqual(len(history1), 2)
        self.assertEqual(history1[0]['content'], 'Mensaje secreto en sesion 1')
        self.assertEqual(history1[1]['content'], 'Respuesta secreta en sesion 1')

        # Create Session 2
        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Sesion 2')
        s2_id = c2['id']

        # Session 2 history must be completely empty
        history2 = self.c.conversations.history(s2_id)
        self.assertEqual(len(history2), 0)

        # Send message in Session 2
        turn2 = self.c.conversations.begin('telegram:t2', 'trace2', 'Hola sesion 2', self.owner, self.chat)
        self.assertEqual(turn2['conversation_id'], s2_id)
        self.c.conversations.delivered(s2_id, 'deliv-2', 'Respuesta sesion 2')

        # Session 2 history has only session 2 messages
        history2 = self.c.conversations.history(s2_id)
        self.assertEqual(len(history2), 2)
        self.assertEqual(history2[0]['content'], 'Hola sesion 2')
        self.assertEqual(history2[1]['content'], 'Respuesta sesion 2')

        # Session 1 history is intact and isolated
        history1 = self.c.conversations.history(s1_id)
        self.assertEqual(len(history1), 2)
        self.assertEqual(history1[0]['content'], 'Mensaje secreto en sesion 1')

    def test_supergroup_forum_topic_binding(self):
        """Verify Telegram Supergroup topics (thread_id) get isolated sessions and routing."""
        # Topic 100
        turn_t1 = self.c.conversations.begin('telegram:topic1', 'tr-top1', 'Pregunta en topico 100', self.owner, self.chat, thread_id=100)
        s_top100 = turn_t1['conversation_id']
        self.assertEqual(turn_t1['thread_id'], 100)

        # Topic 200
        turn_t2 = self.c.conversations.begin('telegram:topic2', 'tr-top2', 'Pregunta en topico 200', self.owner, self.chat, thread_id=200)
        s_top200 = turn_t2['conversation_id']
        self.assertEqual(turn_t2['thread_id'], 200)

        # Sessions must be distinct
        self.assertNotEqual(s_top100, s_top200)

        # Check conversation store entries
        conv100 = self.c.conversations.get_conversation(s_top100)
        conv200 = self.c.conversations.get_conversation(s_top200)
        self.assertEqual(conv100['thread_id'], 100)
        self.assertEqual(conv200['thread_id'], 200)

        # Notify routing includes thread_id
        token = current_turn.set(turn_t1)
        try:
            self.manager.telegram.send_message.reset_mock()
            self.c.notify('Mensaje para topico 100')
            # Wait for background mailbox delivery
            if self.c._mailbox_job:
                self.c._mailbox_job.result(timeout=5)
            self.manager.telegram.send_message.assert_called()
            kwargs = self.manager.telegram.send_message.call_args.kwargs
            self.assertEqual(kwargs.get('message_thread_id'), 100)
        finally:
            current_turn.reset(token)

    def test_auto_naming_from_first_message(self):
        """Initial generic conversation should auto-name from first non-command user message."""
        c = self.c.conversations.new_conversation(self.owner, self.chat, title='Nuevo chat')
        sid = c['id']

        # User sends their first prompt in this conversation
        self.c.conversations.begin('telegram:naming_test', 'tr_name', '¿Cómo configuro Dolphin en Azure?', self.owner, self.chat)
        
        updated = self.c.conversations.get_conversation(sid)
        self.assertEqual(updated['title'], '¿Cómo configuro Dolphin en Azure?')

    def test_auto_naming_from_conversacion_n(self):
        """Conversations titled 'Conversación N' from /new should auto-name on first user message."""
        c = self.c.conversations.new_conversation(self.owner, self.chat)
        sid = c['id']
        self.assertTrue(c['title'].startswith('Conversación '))

        self.c.conversations.begin('telegram:auto_name2', 'tr_name2', 'Explica los permisos de WireGuard', self.owner, self.chat)
        updated = self.c.conversations.get_conversation(sid)
        self.assertEqual(updated['title'], 'Explica los permisos de WireGuard')

    def test_rename_conversation(self):
        """Test /rename command for current conversation and by index."""
        # Rename current conversation
        res = command(self.manager, 'telegram:ren1', '/rename Proyecto Alpha')
        self.assertIn('Conversación renombrada a: Proyecto Alpha', res)

        # Create a second conversation
        c2 = self.c.conversations.new_conversation(self.owner, self.chat, title='Beta')
        
        # Rename conversation 2 (which is Alpha) by index
        res = command(self.manager, 'telegram:ren2', '/rename 2 Proyecto Omega')
        self.assertIn('Conversación renombrada a: Proyecto Omega', res)

        active = self.c.conversations.list_conversations(self.owner, self.chat)
        self.assertEqual(active[1]['title'], 'Proyecto Omega')
