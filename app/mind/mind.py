import json
import logging
import re
from typing import Dict
from pydantic import BaseModel, Field, ValidationError
from app.pixy.control import BudgetExceeded

from app.mind.brain import Brain as LLMManager
from app.mind.memory import MemoryEngine

logger = logging.getLogger("Mind")

from typing import Dict, Optional

class PossibleCause(BaseModel):
    cause: str = Field(..., description="La causa raíz probable.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Nivel de confianza estadística (0.0 a 1.0).")
    proposed_fix: str = Field(..., description="Estrategia técnica para solucionar esta causa.")

class FailureAnalysisIntent(BaseModel):
    reasoning_summary: str = Field(..., description="Deducción lógica estructurada ante el error.")
    action: str = Field(..., description="Debe ser: ACTION_REQUIRED o INVESTIGATE.")
    reply: str = Field(default="", description="Respuesta a Telegram si aplica (usualmente vacía en errores).")
    extracted_goal: str = Field(default="", description="Si es ACTION_REQUIRED, el nuevo objetivo con el [MANIFEST] rico (purpose hiper-detallado).")
    investigate_target: str = Field(default="", description="ID del recurso a leer.")
    investigate_reason: str = Field(default="", description="Obligatorio si action=INVESTIGATE. ¿Por qué buscas esta información?")
    error_summary: str = Field(..., description="Resumen del síntoma visible.")
    evidence: str = Field(..., description="Evidencia concreta (ej. log).")
    possible_causes: list[PossibleCause] = Field(..., description="Lista de múltiples hipótesis probables con niveles de confianza y fixes.")

class NormalIntent(BaseModel):
    reasoning_summary: str = Field(..., description="Breve resumen (máx 2 líneas) de tu deducción lógica estructurada.")
    action: str = Field(..., description="Puede ser: HEALTH_CHECK, INVESTIGATE, ACTION_REQUIRED, STATE_MACHINE_INPUT, GENERAL_CHAT")
    reply: str = Field(..., description="La respuesta que Khai le dará al usuario en Telegram")
    extracted_goal: str = Field(default="", description="Si la acción es ACTION_REQUIRED, detalla el objetivo y adjunta un bloque MANIFEST JSON final (purpose hiper-detallado).")
    investigate_target: str = Field(default="", description="Si la acción es INVESTIGATE, indica el ID del recurso a observar (ej. 'core_logs').")
    investigate_reason: str = Field(default="", description="Obligatorio si action=INVESTIGATE. ¿Por qué buscas esta información?")

class MindResponse(BaseModel):
    is_failure_analysis: bool = Field(..., description="True si reaccionas a un fallo (usar failure_analysis_intent), False para operación normal (usar normal_intent).")
    normal_intent: Optional[NormalIntent] = Field(default=None)
    failure_analysis_intent: Optional[FailureAnalysisIntent] = Field(default=None)

class Mind:
    """
    Khai: el enrutador central de la mente.
    Analiza intenciones, filtra comandos y se comunica directamente con el usuario.
    """
    
    def __init__(self, llm: LLMManager, memory: MemoryEngine):
        self.llm = llm
        self.memory = memory
        
    def parse_static_commands(self, message: str) -> Optional[Dict[str, str]]:
        """Absorbe comandos estáticos (costo cero tokens) liberando al Orchestrator."""
        msg_lower = message.lower().strip()
        
        if msg_lower in ["health", "/health", "status", "/status"]:
            return {"action": "HEALTH_CHECK", "reply": ""}
        if msg_lower in ["fastbeat", "/fastbeat"]:
            return {"action": "FASTBEAT", "reply": ""}
        if msg_lower in ["slowbeat", "/slowbeat", "diagnosticar", "diagnostico"]:
            return {"action": "SLOWBEAT", "reply": ""}
        if msg_lower.startswith("/logs "):
            container = message.replace("/logs ", "").strip()
            return {"action": "LOGS", "reply": "", "target": container}
        if msg_lower in ["/ps", "contenedores", "dockers"]:
            return {"action": "PS", "reply": ""}
        if msg_lower in ["limpiar", "/limpiar", "cleanup", "/cleanup"]:
            return {"action": "CLEANUP", "reply": ""}
        if msg_lower in ["hola", "buenas", "buen dia", "buenos dias", "buenos días", "buenas tardes", "buenas noches", "hey", "que tal", "qué tal"]:
            return {"action": "GENERAL_CHAT", "reply": "Hola ¿En qué trabajamos hoy?"}
            
        return None
        
    def process_message(self, message: str) -> Dict[str, str]:
        """
        Analiza el mensaje del usuario y devuelve una acción y una respuesta.
        """
        # Validar si es un comando estático de costo 0
        static_intent = self.parse_static_commands(message)
        if static_intent:
            return static_intent
            
        # Guardar el mensaje del usuario en memoria
        self.memory.append_long_term("user_chat", message)
        
        # Recuperar las últimas interacciones para el contexto
        chat_history = self.memory.get_chat_history(limit=100)

        personality_prompt = self.memory.get_system_prompt()
        schema = MindResponse.model_json_schema()
        
        prompt = f"""
{personality_prompt}

Estás actuando como el Router Central de Mind.

=== HISTORIAL RECIENTE DE CONVERSACIÓN ===
{chat_history}
==========================================

Recibes el siguiente NUEVO mensaje del usuario (o un evento de sistema):
"{message}"

Debes clasificar la intención de este mensaje en una de estas 5 categorías:
1. HEALTH_CHECK: Pide revisar el estado del servidor o sistemas.
2. INVESTIGATE: Una skill ha fallado por un problema ambiental (ej. timeout, error de red) o necesitas evidencia externa que no posees.
3. ACTION_REQUIRED: Pide crear/ejecutar una skill, o debes corregir un error basándote en tu Failure Analysis.
4. STATE_MACHINE_INPUT: Está dando una respuesta corta a una pregunta anterior.
5. GENERAL_CHAT: Preguntas teóricas, charla general.

Tu respuesta DEBE ser EXCLUSIVAMENTE un objeto JSON válido que respete este JSON Schema:
{json.dumps(schema, indent=2)}

REGLAS PARA EL JSON:
- DEBES setear 'is_failure_analysis'. Si el mensaje indica que hubo un ERROR en una tarea, usa 'failure_analysis_intent'. De lo contrario, usa 'normal_intent'.
- En 'failure_analysis_intent', OBLIGATORIAMENTE debes llenar 'possible_causes' con MÚLTIPLES hipótesis de la causa del fallo con diferentes niveles de confianza (ej. API caída 0.7, URL mal 0.2).
- INVESTIGATE: Si decides investigar, debes proporcionar 'investigate_target' Y OBLIGATORIAMENTE 'investigate_reason' (¿qué buscas y por qué?).
- MANIFEST RICO: Si la acción es ACTION_REQUIRED, 'extracted_goal' DEBE terminar con un bloque [MANIFEST] en formato JSON. IMPORTANTE: El campo 'purpose' del manifest DEBE SER MUY DETALLADO (ej. "Calculadora de divisas que consulta a una API externa REST"). No uses propósitos cortos o Pixy lo rechazará.
- CONCISIÓN: Sé directo y conciso en 'reply' (máximo 1 o 2 oraciones). Cero relleno robótico, discursos o sermones.
- PROHIBIDO EL USO DE EMOTICONES O EMOJIS: Nunca uses emoticones ni emojis en 'reply' ni en ninguna parte del JSON.
- ¡Devuelve el raw JSON válido directamente!
"""
        try:
            result_text = self.llm.generate_content(prompt, response_format={"type": "json_object"})
            
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
                
            response = self._validate_response(result_text.strip())
            
            intent = response.failure_analysis_intent if response.is_failure_analysis else response.normal_intent
            if intent and intent.action == 'GENERAL_CHAT' and not response.is_failure_analysis:
                conversation_prompt = (
                    f"{personality_prompt}\n\n"
                    "Responde en español al mensaje del propietario como Khai. "
                    "Sé conciso. No afirmes haber ejecutado herramientas, enviado mensajes "
                    "ni consultado datos externos. Si se requiere una acción, dilo claramente.\n\n"
                    f"Historial reciente:\n{self.memory.get_chat_history(limit=10)}\n\n"
                    f"Mensaje: {message}\nRespuesta:")
                try:
                    intent.reply = self.llm.generate_content(
                        conversation_prompt, purpose='conversation')
                except Exception as exc:
                    logger.warning('Respuesta conversacional de respaldo (%s)', type(exc).__name__)
            
            # Guardar pensamiento interno
            if intent and intent.reasoning_summary:
                self.memory.append_long_term("system_msg", f"[Khai Reasoning]: {intent.reasoning_summary}")
            
            # Guardar la respuesta generada
            if intent and intent.reply:
                self.memory.append_long_term("khai_chat", intent.reply)
                
            if not intent:
                return {"action": "GENERAL_CHAT", "reply": "Error interno: Falló la selección de intent."}
            
            # Formatear el dict clásico
            out_dict = {
                "internal_monologue": intent.reasoning_summary,
                "action": intent.action,
                "reply": intent.reply,
                "extracted_goal": intent.extracted_goal,
                "investigate_target": intent.investigate_target,
                "investigate_reason": intent.investigate_reason,
            }
            
            if response.is_failure_analysis and response.failure_analysis_intent:
                out_dict["failure_analysis"] = {
                    "error_summary": response.failure_analysis_intent.error_summary,
                    "evidence": response.failure_analysis_intent.evidence,
                    "possible_causes": [pc.model_dump() for pc in response.failure_analysis_intent.possible_causes]
                }
            else:
                out_dict["failure_analysis"] = None
                
            return out_dict
            
        except BudgetExceeded:
            raise
        except ValidationError as ve:
            logger.error("Error de validación estructurada en Mind: %s", ve)
            return {"action": "GENERAL_CHAT", "reply": "No pude interpretar la respuesta del proveedor. Reformularé la solicitud antes de fallar."}
        except Exception as e:
            logger.error("Error procesando mensaje en Mind: %s", e)
            return {"action": "GENERAL_CHAT", "reply": f"Error de IA: {str(e)}"}

    @staticmethod
    def _validate_response(raw: str) -> MindResponse:
        try:
            return MindResponse.model_validate_json(raw)
        except ValidationError as original:
            repaired = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', raw)
            if repaired == raw:
                raise original
            return MindResponse.model_validate_json(repaired)
