"""Single conversational router; tool calls are validated before Core dispatch."""
import json
import logging
from app.mind.turn import AssistantTurn
from app.pixy.control import BudgetExceeded

logger = logging.getLogger("Mind")


class Mind:
    def __init__(self, llm, memory):
        self.llm, self.memory = llm, memory

    def process_message(self, message, task_context=None, persist=True,
                        conversation_history=None, questions=None):
        history = (conversation_history if conversation_history is not None
                   else self.memory.get_chat_history(limit=12))
        prompt = (
            self.memory.get_system_prompt()
            + "\nEres Mind, el unico router conversacional de Khai. "
            "Devuelve un AssistantTurn JSON: reply para conversar o UNA tool_call para actuar. "
            "No publiques promesas de acciones en reply. Una consulta de datos externos actuales "
            "requiere start_task, aunque este formulada como pregunta. Recupera el objetivo "
            "completo del contexto ante instrucciones como 'Hazlo'. "
            "Un saludo conserva los trabajos existentes y no los reanuda ni declara su exito. "
            "Una peticion independiente usa start_task. Un cambio al trabajo actual usa "
            "revise_task con su task_id y goal_revision exactos. Nunca inventes IDs. "
            "El campo change debe copiar literalmente el mensaje nuevo del propietario. "
            "Para consultar o cancelar un objetivo usa get_task_status o cancel_task. "
            "Si el objetivo de una referencia es ambiguo, pregunta con reply antes de actuar. "
            "answer_question solo selecciona una pregunta realmente entregada y su respuesta "
            "debe copiar literalmente el mensaje del propietario; no inventes aprobaciones. "
            "Para salud interna usa get_system_status. Las credenciales solo ingresan por "
            "los comandos privados de Voice, nunca por parametros de herramientas. "
            "El historial no acredita ejecucion; el estado y los recibos son la fuente de verdad. "
            "No uses emojis. Responde a saludos en una o dos oraciones."
            + "\nEsquema: " + json.dumps(AssistantTurn.model_json_schema(), ensure_ascii=False)
            + "\nConversacion: " + json.dumps(history, ensure_ascii=False)
            + "\nObjetivos: " + json.dumps(task_context or [], ensure_ascii=False)
            + "\nPreguntas entregadas pendientes: " + json.dumps(questions or [], ensure_ascii=False)
            + "\nMensaje nuevo: " + json.dumps(message, ensure_ascii=False)
        )
        try:
            raw = self.llm.generate_content(prompt, response_format={"type": "json_object"})
            turn = self._validate_response(raw)
            intent = turn.intent()
            if persist:
                self.memory.append_long_term("user_chat", message)
                if turn.reply:
                    self.memory.append_long_term("khai_chat", turn.reply)
            return intent
        except BudgetExceeded:
            raise
        except Exception as exc:
            logger.warning('No se pudo interpretar el turno (%s)', type(exc).__name__)
            if "Todos los LLM están ocupados" in str(exc):
                return {'action': 'ROUTING_ERROR', 'reply': '', 'error': 'AllLLMsBusy'}
            return {'action': 'ROUTING_ERROR', 'reply': 'No pude interpretar el mensaje; no ejecute ninguna accion.',
                    'error': type(exc).__name__}

    @staticmethod
    def _validate_response(raw):
        cleaned = raw.strip()
        if cleaned.startswith('```'):
            cleaned = '\n'.join(cleaned.splitlines()[1:])
            if cleaned.rstrip().endswith('```'):
                cleaned = cleaned.rstrip()[:-3]
        return AssistantTurn.model_validate_json(cleaned.strip())
