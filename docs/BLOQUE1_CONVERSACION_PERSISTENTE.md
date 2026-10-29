# Bloque 1: conversación persistente sobre el runtime existente

Revisión local del 29 de octubre de 2026. No añade dependencias ni incorpora
LangGraph. Coordinator conserva los objetivos y la ejecución; Pixy conserva
las autorizaciones; Voice conserva transporte, inbox y outbox.

## Sesiones y turnos

La sesión se identifica de forma estable por propietario y chat Telegram. Cada
actualización ordinaria conserva su origen, traza, mensaje respondido y objetivo
asociado. Los comandos privados de credenciales se interceptan antes de registrar
la conversación ordinaria.

Las tablas `conversations`, `conversation_turns`, `conversation_messages` y
`conversation_questions` comparten la SQLite de control existente. El outbox
añade metadatos y los IDs de los mensajes devueltos por Telegram. Son cambios
aditivos locales; no se ha ejecutado esta revisión en Azure.

Los turnos de charla se distinguen mediante `kind=turn`; una llamada para iniciar
trabajo convierte ese registro en `kind=objective`. Una corrección mantiene el
ID del objetivo; una petición independiente inicia otro dentro de la misma sesión.
Los turnos de control conservan su operación y resultado, sin convertirse en otro
objetivo de ejecución.

La memoria corta de la sesión utiliza mensajes recibidos y respuestas realmente
entregadas. Se limita a 16 mensajes y 16.000 caracteres de contexto, con extractos
de hasta 4.000 caracteres por mensaje. El almacenamiento conserva el texto completo.
No se copian mensajes de otros chats al contexto del planificador.

## Una decisión de enrutamiento

Mind devuelve un `AssistantTurn` validado por Pydantic: una respuesta o una sola
llamada estructurada. No se admiten ambos resultados a la vez ni herramientas
arbitrarias. El contrato se transporta como JSON mediante el gateway actual de
Gemini/Dolphin; esta revisión no migra los proveedores a sus APIs nativas de
function calling.

Operaciones internas admitidas:

| Operación | Responsabilidad |
| --- | --- |
| `start_task` | Iniciar un objetivo con su descripción completa, incluso ante «Hazlo». |
| `revise_task` | Corregir un objetivo de la sesión y una revisión concreta. |
| `get_task_status` | Consultar estado, ejecuciones y proveedores aprobados, sin credenciales ni query strings. |
| `cancel_task` | Solicitar cancelación mediante el mecanismo existente. |
| `answer_question` | Vincular una respuesta literal con una pregunta enviada. |
| `get_system_status` | Consultar la integridad que proporciona Heart. |

Los IDs de llamada son estables por origen y los recibos se conservan en el turno.
El modelo propone; el dispatcher comprueba sesión, objetivo, revisión y respuesta.
Un cambio debe reproducir literalmente la instrucción del propietario. El
planificador recibe después el objetivo resuelto y desarrolla las acciones;
no vuelve a clasificar el mensaje como charla.

## Preguntas y decisiones

Las preguntas se vinculan con sujeto, estado, revisión y entrega. Solo se abren
cuando Voice confirma el envío. Una respuesta al mensaje concreto selecciona su
pregunta; un sí/no sin referencia utiliza la última pregunta enviada pendiente,
incluyendo las propuestas de Pixy. Una entrega incierta impide aplicar un sí/no
sin referencia a otra pregunta.

`ReplyResult.handled` distingue un mensaje atendido de uno que necesita
enrutamiento aunque no haya texto que enviar. Así una confirmación o aclaración
no cae después en otro manejador ni crea otro objetivo.

Las decisiones de tarea consumen pregunta, comando y transición dentro de la
misma transacción del control store. Las autorizaciones de recuperación siguen
usando los puertos existentes de Pixy y Heart. Las aprobaciones de artefactos se
responden con sí/no; no se anuncian los comandos retirados `/approve`, `/deny`
ni `/artifact`. Una recuperación propuesta por Heart pregunta por su autorización
concreta, sin transformarla en una aprobación de artefacto.

Si el proceso cae después de guardar un envío como entregado y antes de abrir
su pregunta, el registro se reconcilia desde el outbox. No se reenvía el mensaje.

## Cambios durante la creación

Un cambio anterior a la ejecución conserva el objetivo, registra el texto y su
origen, incrementa `goal_revision`, invalida la confirmación de datos y la
autorización del artefacto, y vuelve a resolver el alcance. Añade el presupuesto
inicial fijo, conservando lo consumido. Una petición de creación todavía no
aprobada sigue necesitando ese consentimiento después de la corrección.

El trabajo en curso queda ligado a la revisión que lo inició. Una generación
que termina después del cambio no puede actualizar la nueva revisión ni pasar
a ejecución; se comprueba también antes de guardar su artefacto. Las revisiones
del objetivo no retroceden al reconstruir el alcance.

La charla y las consultas de estado mantienen la generación activa. Si el
proceso ya está ejecutándose, este bloque no modifica silenciosamente el trabajo:
se conserva la consulta/cancelación existente y, para resultados pendientes de
aceptación, el flujo de corrección con sus recibos.

## Validación

La suite completa del corte pasó **212/212 pruebas** con Python 3.12.10 y las
dependencias existentes. Incluye **22 pruebas nuevas** del flujo conversacional:
sesión tras reapertura, outbox conectado, recuperación del recibo de entrega,
preguntas de tipos distintos, autorizaciones de Pixy, referencias Telegram,
datos obsoletos, entregas inciertas, chat durante la generación, objetivos
independientes, correcciones, presupuestos fijos, descarte de trabajo obsoleto,
consentimiento de creación, aislamiento entre chats y consulta del proveedor.

Las pruebas de composición validan además los contratos de servicios, la
confirmación de entradas, la ejecución y la verificación con dobles de transporte
y runtime. Se corrigió el cierre de workers de prueba para que SQLite no permanezca
abierta cuando se eliminan los directorios temporales en Windows.

## Límites de este bloque

No cierra los otros bloques de la auditoría: cancelación frente a peticiones
externas en curso, verificación de recuperación, consumo de órdenes de Heart,
aprendizaje y transacciones de reglas, transporte/rotación de credenciales,
presupuestos de proveedores, ciclo AGENT ni reintentos inciertos de Telegram.
La aprobación de servicios y el consumo de autorizaciones conservan sus mecanismos
existentes; no se declaran corregidas todas sus ventanas de caída.

No se hizo commit, push, purga, despliegue ni reinicio remoto. La validación con
Telegram y proveedores reales sigue pendiente y debe realizarse después de las
correcciones necesarias de la auditoría y de una activación autorizada.
