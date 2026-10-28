> Contratos de la revisión local en curso. Los cambios del 26 de octubre de 2026 aún no tienen despliegue verificado. Para el corte de datos, usar [ARCHITECTURE_MIGRATION.md](ARCHITECTURE_MIGRATION.md).

# Arquitectura de Khai

## Flujo y fronteras

```mermaid
flowchart LR
    Telegram --> Ingress[Webhook autenticado]
    Ingress --> Store[(ControlStore / SQLite)]
    Store --> Coordinator
    Coordinator --> Mind
    Coordinator --> Hand
    Hand --> Artifact[Artefacto SHA256]
    Coordinator --> Pixy
    Pixy --> Review[Decisión vinculada al hash]
    Review --> Coordinator
    Coordinator --> Proxy[Docker Proxy autenticado]
    Proxy --> Worker[Workspace privado]
    Worker --> Receipt[Recibo de ejecución]
    Receipt --> Coordinator
    Coordinator --> Verifier[SHA256 esperado o verificación del usuario]
    Verifier --> Ledger[LearningLedger]
    Ledger --> Candidates[Reglas candidatas]
```

Mind es el propietario de la interpretación y el Coordinator del estado de
objetivos. Un bloqueo de proceso impide dos coordinadores sobre el mismo
directorio. El proxy tiene su propia base y un bloqueo independiente; sus
operaciones Docker no se delegan al modelo.

Mind usa Brain para interpretar lenguaje natural, pero valida la salida
estructurada antes de ejecutar acciones. Esa frontera no pretende limitar la
conversación: permite tolerar texto mal formado del proveedor, separar
evidencia de hipótesis y evitar que una respuesta ambigua autorice una
operación. Las respuestas con JSON mal escapado se reparan de forma acotada;
las decisiones que siguen siendo ambiguas se devuelven como explicación y no
se ejecutan silenciosamente.

Las llamadas HTTP del webhook no ejecutan modelos. Los objetivos se persisten antes de responder y el coordinador los avanza en segundo plano. No se utiliza una cola externa: SQLite conserva las fases y un único coordinador las procesa.

## Identidades y almacenamiento

| Identidad | Significado |
| --- | --- |
| `telegram:<update_id>` | Clave de deduplicación de la entrada o comando. |
| `task_<uuid>` | Objetivo raíz, conservado durante reintentos y cambios de hipótesis. |
| `run_<uuid>` | Intento de ejecución; se reutiliza al repetir una petición de envío incierta. |
| SHA256 | Identidad del código revisado y autorizado. |
| Token de aprobación | Identifica una decisión pendiente, con hash y vencimiento. |

`data/control.sqlite3` contiene el estado transaccional del Core. Los órganos acceden mediante puertos acotados y repositorios lógicos; compartir el archivo no comparte autoridad. `KHAI_DB_PATH` permite fijar una ruta absoluta común en herramientas aisladas. La composición normal fija la ruta desde `DATA_DIR`.

Las decisiones de Pixy y las órdenes de Hand se vinculan por `authorization_id`, `order_id`, operación, sujeto, alcance y hash. Los IDs localizan recibos; no son permisos portátiles. `trace_id` solo correlaciona. Véanse [CONTROL_PLANE.md](CONTROL_PLANE.md) y los [ADRs](adr/).

`data/proxy.sqlite3` guarda el payload, su fingerprint y el recibo de cada ejecución. El código vive en `data/artifacts/<sha>.py`; los inputs del worker, en `data/execution_inputs/<run>.json`; los outputs y checkpoints, en `data/workspaces/<task>/<sha>/`.

Los modelos de tareas se guardan como documentos JSON en SQLite. Las transiciones y los comandos de aprobación/verificación usan transacciones. El estado se consulta nuevamente antes de avanzar una fase; una tarea cancelada no vuelve a quedar lista.

## Estados del objetivo

| Estado | Interpretación y siguiente paso |
| --- | --- |
| `ROUTING` | Buscar una versión activa compatible, parametrizarla o interpretar la entrada. Si falta skill, transiciona a `WAITING_CREATION_APPROVAL`. |
| `WAITING_CREATION_APPROVAL` | Preguntar al usuario si autoriza la creación de una nueva skill (`apruebas: y/n`). |
| `GENERATING` | Hand prepara código nuevo o una versión activa seleccionada. Una tarea Network sin servicio declarado propone un descriptor antes de generar. |
| `WAITING_SERVICE_APPROVAL` | Esperar la decisión de Pixy sobre el descriptor versionado propuesto por Hand. |
| `REVIEWING` | Pixy revisa AST y congruencia semántica. |
| `WAITING_APPROVAL` | Esperar un token humano asociado al hash y al plazo para ejecución sensible. |
| `READY` | Autorización válida; preparar el siguiente intento en entorno controlado. |
| `WAITING_SECRET` | Esperar un recibo privado de Voice para la clave referenciada; Coordinator consulta disponibilidad y versión, nunca el valor. |
| `SUBMITTED` | Intento persistido; enviar o repetir el mismo envío idempotente. |
| `RUNNING` | Consultar su recibo; no lanzar un segundo intento. |
| `UNVERIFIED` | Proceso terminado o servicio iniciado sin criterio verificado. |
| `VERIFYING` | Registrar una observación verificada y entregar el resultado obtenido al usuario. |
| `DIAGNOSING` | Elegir una hipótesis alternativa bajo el presupuesto original. |
| `CANCELLING` | Cancelación solicitada; falta confirmar el resultado del proxy. |
| `COMPLETED` | Interacción terminada o resultado de habilidad verificado. La entrega Telegram tiene recibo separado y puede quedar pendiente o incierta. |
| `FAILED` | Fallo terminal sin hipótesis alternativas disponibles. |
| `EXHAUSTED` | Límite de intentos o presupuesto agotado; caso diagnosticable y remediable por Heart. |
| `CANCELLED` | Cancelación confirmada o denegación antes de ejecutar. |
| `UNKNOWN` | No hay evidencia suficiente para repetir con seguridad. |

Una conversación ordinaria también termina en `COMPLETED`, pero no posee un recibo verificado ni alimenta las métricas de habilidades.

## Hitos de Progreso y Filosofía Libre de Emoticones

Khai y Pixy declaran explícitamente sus hitos en lenguaje técnico y sobrio, sin emoticones ni emojis:
1. Consulta de autorización antes de crear herramientas (`WAITING_CREATION_APPROVAL`).
2. Inicio de generación de skill por parte de Hand.
3. Auditoría de seguridad del código por parte de Pixy.
4. Ejecución en contenedor aislado.
5. Verificación de evidencia y entrega del resultado real al usuario.
6. En caso de error, delegación formal a Heart para diagnóstico médico y remediación estructurada (`retry_task`).

## Protocolo Core → Proxy

Todos los endpoints del proxy requieren `Authorization: Bearer <PROXY_TOKEN>`. El token no se entrega al worker.

- `POST /executions`: recibe `execution_id`, `task_id`, `artifact_hash`, `mode`, `dependencies`, `request`, `network`, `grants` y `deadline`.
- `GET /executions/{execution_id}`: consulta el recibo persistido.
- `POST /executions/{execution_id}/cancel`: solicita detener la ejecución. Puede crear una marca de cancelación antes de recibir el envío original.
- `GET /docker-ps`: devuelve ejecuciones conocidas, no control arbitrario del daemon.
- `POST /docker-logs`: consulta recibos de `agent_logs_run_<uuid>`.

El mismo ID con el mismo payload recupera su recibo; otro payload con ese ID se rechaza. Los estados del proxy son `queued`, `starting`, `running`, `started`, `finished`, `failed`, `approval_required`, `timeout`, `cancelled` y `unknown`.

`finished` incluye `returncode`, `stdout`, `stderr` y, cuando corresponde, evidencia del cierre del proceso y OOM. `approval_required` transporta `action` y `details` con salida `75`; no se transforma en una corrección automática del código.

## Aislamiento y permisos

Cada worker monta solamente su código de solo lectura, su input de solo lectura, el runner de solo lectura y un workspace de escritura. Usa raíz de solo lectura, eliminación de capabilities, `no-new-privileges`, límites de procesos, CPU, memoria y logs. La red directa permanece desactivada (`network none`); una capacidad Network solo habilita operaciones de Voice expresamente ligadas a la tarea. En el código local actual se exige aprobación humana para cualquier artefacto Network. Está acordado revisar la política para que las lecturas no pidan aprobación adicional y las escrituras sí. Instalar dependencias también requiere aprobación; la construcción necesita red para descargar paquetes, aunque la habilidad permanezca offline.

Pixy conserva reglas AST para escapes del sistema y usa una revisión contextual por LLM para juzgar el uso de librerías de red; un import por sí solo no concede acceso. Hand puede proponer un ServiceDescriptor; Pixy lo aprueba manualmente por ahora y Voice aplica operación, versión, DNS/TLS y cuotas. Una credencial ausente abre una captura privada. Voice entrega a Coordinator solo disponibilidad y versión, nunca el valor.

El AST es una comprobación previa, no una prueba general de seguridad de Python. La revisión LLM tampoco reemplaza el aislamiento. Las aprobaciones no conceden nuevas facultades al sistema operativo: autorizan el artefacto y, para BaseSkill, operaciones declaradas dentro del mismo perímetro.

GOD conserva `run_step` y `require_approval` mediante un adaptador local del worker. Sus checkpoints están en el workspace, nunca en la base del Core. Reanudar tras aprobación inicia otra ejecución del mismo artefacto; los checkpoints evitan pasos ya registrados, pero no hacen transaccional una operación externa.

## Recuperación

El Core retoma las fases persistidas. Un envío sin respuesta se repite con el mismo ID. El proxy utiliza contenedores con nombre e identidades verificables y conserva el recibo antes de retirarlos.

Si el contenedor existe se reconcilia; si falta después de un lanzamiento incierto se declara `unknown`. Una caída del daemon conserva el estado activo para consultar nuevamente. Los fallos con posibles efectos externos se detienen en `UNKNOWN`, sin autorrepetición.

Las tareas del formato anterior se conservan y se presentan como `UNKNOWN`; no se reanudan a ciegas.

## Aprendizaje

La identidad conversacional de Khai se carga desde `data/memory/agent.md` y se
combina con las preferencias persistidas por `MemoryEngine`. Khai puede adaptar
su trato a preferencias explícitas y correcciones del usuario, pero no debe
inferir ni conservar datos sensibles sin confirmación. La personalidad no
concede permisos adicionales ni sustituye la máquina de estados.

Pixy comparte el `LLMManager` para sus revisiones, pero no es un agente con
autonomía propia: su tono puede ser juguetón y sus observaciones pueden tener
humor, mientras sus acciones permanecen limitadas por el coordinador, la
constitución y el proxy autenticado.

Los éxitos de habilidades proceden de un archivo cuyo hash esperaba el usuario o de una verificación explícita del usuario sobre un `execution_id`. Un cierre de proceso con error puede registrarse como fallo cuando existe un recibo que lo acredita. Un lanzamiento, una frase de éxito o una reflexión del modelo no generan un éxito aprendido.

Hand almacena SkillSpec con descripción, parámetros tipados, dependencias y referencias a servicios. Solo una candidata verificada entra al catálogo activo. Mind puede reutilizarla con otra redacción y parámetros. El selector de embeddings locales permanece provisional y sin modelo calibrado; usa fallback LLM. Las recetas de composición tienen validación de versiones y tipos, sin ejecutor aún.

`control_feedback` deduplica observaciones. Las métricas registran hipótesis, contexto y reglas utilizadas. El campo histórico `confidence` representa una proporción empírica tras recibir observaciones; no es una probabilidad causal calibrada.

Una hipótesis necesita éxitos en al menos tres objetivos diferentes y proporción de éxito de al menos 0,8 para proponer una regla `candidate`. La activación se solicita con `/rule approve`; la sustitución usa un ID `supersedes` explícito. `/rule rollback` desactiva la regla y restaura la anterior si corresponde. Las reglas utilizadas con al menos tres observaciones y proporción de éxito menor o igual a 0,3 se retiran.

Las reglas históricas sin procedencia en `rule_proposals` se preservan, pero no se inyectan como reglas verificadas en SkillForge. La detección semántica automática de contradicciones y las comparaciones controladas contra una estrategia de referencia siguen pendientes.

## Referencias de diseño

OpenClaw aporta referencias para serialización de sesiones, idempotencia, políticas de ejecución y procedencia. Hermes aporta referencias para separar memoria de hechos, historial y procedimientos, y para revisar cambios de conocimiento. Khai implementa sus propios contratos; no incorpora automáticamente las garantías de esos proyectos.

- [OpenClaw: ciclo de ejecución](https://docs.openclaw.ai/concepts/agent-loop)
- [OpenClaw: aprobaciones](https://docs.openclaw.ai/tools/exec-approvals)
- [OpenClaw: consolidación de memoria](https://docs.openclaw.ai/concepts/dreaming)
- [Hermes: skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- [Hermes: memoria](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
