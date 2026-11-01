# Auditoría de ciclos de interacción — 31 de octubre de 2026

## Resultado y alcance

**La trazabilidad completa por ramas todavía no está implementada.** Hay recibos y registros durables útiles en los órganos, pero no un árbol común que relacione de forma obligatoria cada interacción, sus suboperaciones y su cierre. Esta auditoría distingue garantías verificadas, defectos reproducidos y diseño propuesto; las pruebas positivas no certifican los huecos como resueltos.

Se revisaron siete caminos: cambio de modelo, chat, creación de skill, gobierno de Pixy, reparación, informe de Pixy y uso de skill. Seis agentes especializados y el coordinador revisaron código y pruebas; no ejecutaron cambios de infraestructura ni reparaciones durante la auditoría. Las pruebas nuevas se incorporaron a las ocho suites canónicas existentes.

## Evidencia de producción

Código activo: `19ce591`. Lectura SQLite por SSH, sin construir otro ModuleManager mientras Core está activo.

- Preferencia: `telegram:1793493376291` → `task_82d5c709e6b7415d919f5a52fd7114af` → `set_default_provider(web_search, brave)` → entrega 1699. Completó en 164,33 s; fuente y fecha persistidas.
- Búsqueda: `telegram:1793493578111` → `task_c8f28047363445d68630e3633b90612b` → `rejection_5c4ca409c97d4ec18448a14f55a28327` → `case-20261031e0f6d1d0` → propuesta de Pixy entregada en mensaje 1701. Dreams registra `failure/routing`, sin inventar un worker.
- El rechazo contiene `name` y `arguments` fuera de `tool_calls` y recupera una orden antigua de crear una skill de Google. No hubo despacho. Modelo `dolphin3:8b`; `done_reason=stop`, 87 tokens de salida, 141,32 s totales. No hay evidencia de truncamiento de salida por presupuesto.
- El JSON, esquema y errores quedaron persistidos y copiados a la evidencia del caso correcto. **Heart aun así propuso revisar una conexión por un timeout antiguo**, sin que ese timeout explique este rechazo. La propuesta no fue aprobada por esta sesión. Tener logs no garantiza que el modelo interprete correctamente su alcance.
- Instantánea agregada durante la auditoría: 2.255 `events`, 11 `control_events`, 18 tareas, 82 turnos e inbox, 163 entregas, 19 casos y 134 transiciones de Heart, 17 autorizaciones, 15 ejecuciones del agente y 97 llamadas de herramientas. Son conteos observados, no un certificado de completitud.
- `event_outbox` estaba vacío porque sus eventos se despacharon al almacén durable `events`. El borrado de esa cola después de emitir no equivale por sí solo a pérdida de auditoría.

## Matriz de caminos

| Camino | Registros actuales | Huecos que impiden garantizar inicio→fin por rama |
|---|---|---|
| Cambio de modelo | `model_transition_events`: `attempt_id`, traza, perfil, fase, tiempos y error | Falta operación raíz para `set_mode`/cascada; nano, stop y rollback no tienen cobertura equivalente. Retención elimina eventos superiores a 500. No persiste operación remota GCP. Cancelación y reconciliación siguen pendientes. |
| Chat | Inbox, turno/sesión, tarea, llamadas del router, outbox e historial | Falta vínculo común de padre/hijo y traza/fuente en outbox. `completed` del inbox no significa entrega final. Coalescencia puede dejar turnos secundarios abiertos. Acuse de Telegram insuficientemente validado y fragmentos parcialmente entregados no se conservan individualmente. |
| Creación de skill | Tarea, `agent_runs`, mensajes, llamadas, versiones, operaciones y revisiones | Excepciones pueden dejar agente `running`; IDs del proveedor pueden sobrescribir otra rama. Primer fallo de generación puede reintentar antes de Heart. Algunas operaciones y rechazos carecen de correlación o registro propio. |
| Solicitud de Pixy | Autorizaciones `auth_`, traza/caso, términos inmutables, actores/fechas, órdenes `order_` y consumo idempotente | Falta ciclo propio que una petición, revisión, decisión y respuesta. Revisiones directas o excepciones pueden quedar sin audit; gobierno de reglas/servicios y expiración carecen de rama/evento explícito. |
| Reparación | Caso, transiciones inmutables, evidencia, plan, autorización, orden, intento y verificación | El evento global lleva `case_id` pero no el enlace padre a tarea/traza. La explicación del modelo puede atribuir otra causa sin evidencia vinculada. No hay cierre/reconciliación uniforme de interrupciones durante diagnóstico. |
| Informe a Pixy | Entrada y entrega de Voice; casos y transiciones de Heart, observaciones de Senses | Estado/informe/reparación pueden desembocar en el mismo `diagnose`, que crea caso y eventualmente autorización. Falta distinguir consulta de propuesta de cambio y devolver referencias estructuradas al turno. El informe no tiene terminal propio por entrega. |
| Uso de skill | Tarea, versión/hash, ejecución, recibos de proxy/Voice, verificación y Dreams | No hay evento de creación de tarea ni historial de cada observación de runtime. Rechazos del mailbox del gateway pueden quedar solo en response.json; `external_request` se emite solo en éxito y sin tarea/run/correlación. Falta árbol común de selección, ejecución y entrega. |

## Defectos prioritarios reproducidos

### P1 — Pérdida o atribución incorrecta de evidencia

1. `app/hand/agent_loop.py`: `INSERT OR REPLACE` usa `call_id` global entregado por el proveedor. Dos ejecuciones distintas con el mismo ID dejan cero llamadas en la primera y una en la segunda. La clave de auditoría debe ser interna; el ID externo es un atributo.
2. `app/heart/recovery.py`: el caso real de rechazo de formato recibió evidencia correcta pero una explicación de red ajena. Los logs históricos deben quedar separados del fallo objetivo; una hipótesis necesita referencias verificables antes de convertirse en plan.
3. `app/voice/mailbox.py`: `deliver_one` puede marcar `delivered` con respuesta sin `ok` o `message_id`. Si falla un fragmento posterior, pierde los acuses de los anteriores. No confundir envío intentado con entrega confirmada.

### P1 — Ciclos sin cierre o reintento antes del diagnóstico

4. `AgentLoop.run`: `TimeoutError` en `llm.complete` y dos respuestas iguales sin herramienta dejan `status=running` en SQLite. Persistir el terminal real antes de propagar; tras caída del proceso, reconciliar sin inventar éxito/fallo ni repetir efectos inciertos.
5. `Coordinator` en GENERATING: el primer `AgentExhausted`/error de generación puede permanecer GENERATING; Heart se consulta después del segundo. Contradice el principio de diagnóstico ante el primer fallo.
6. `Hand.approve`: el rechazo de Pixy puede ocurrir antes de crear la operación de aprobación. Reproducción: solo queda `create/completed`, sin operación de aprobación rechazada.

### P2 — Enlaces incompletos

7. Cambio de modelo: eventos por candidato, sin raíz común durable ni equivalencia en todas las salidas. Un informe puede elegir el fallo más reciente del perfil en vez de una transición concreta.
8. Chat: `source`, `trace_id`, tarea y entrega se pueden reconstruir parcialmente por tablas, pero no se propagan obligatoriamente. Cierre de enrutamiento y cierre de entrega son hechos distintos.
9. Hand: `edit/reject/archive/delete/restore`, descripción fallida y `finish_one_off` no tienen la misma cobertura que `create/approve/use`.
10. Heart: evidencia se guarda en el caso después de una llamada de diagnóstico potencialmente lenta. Senses conserva su observación y Voice el rechazo, pero falta reconciliar explícitamente el caso interrumpido en `COLLECTING_EVIDENCE`.
11. Pixy: `review()` no registra por sí misma su resultado. Coordinator sí registra revisiones normales en `control_events(review)`; no se deben presentar todas las revisiones como perdidas. `get_by_trace()` retorna solo la autorización más reciente, insuficiente para listar ramas hermanas.
12. Informes Pixy: `_recent_context_summary` llama a `Recovery.all_cases()`, que no existe; la excepción silenciosa oculta casos. Si Senses falla, el caso puede quedar FAILED y la respuesta decir «No se detectaron anomalías activas» por no tener findings. El informe debe comunicar evidencia indisponible y su error.
13. Informes Pixy: no se resuelven referencias explícitas a case/run/trace con el mismo alcance que task_id. `open_case` no llena las columnas `task_id/run_id`, aunque haya vínculo dentro del JSON, por lo que los índices por tarea no recuperan esos informes.
14. Uso de skill: el selector conversacional descarta especificaciones con `dependencies`. El parser con wheels verificados puede ejecutarse en la aceptación administrativa, pero queda fuera de ese camino de reutilización. Las ejecuciones del parser realizadas no certifican su selección conversacional.
15. Gateway: los wrappers reducen errores HTTP a `RuntimeError` y pueden perder status, etapa y causa antes de llegar a Heart. Persistir evidencia técnica segura antes de envolver la excepción, sin cuerpos de proveedor ni credenciales.
16. Dreams: `task_observations` conserva la ejecución real, pero la nota episódica usa `agent_run_id` como `run_id`. Distinguir explícitamente el ciclo del agente del worker para evitar atribuciones ambiguas.

## Contrato propuesto para la corrección

Diseño pendiente de decisión e implementación; no requiere habilitar Cloud Monitoring.

- Conservar los IDs de dominio existentes y añadir un ciclo con ID interno propio, `root_id`, `parent_id`, `kind`, `source`, `trace_id`, revisión e intento. Los IDs correlacionan; no conceden autoridad.
- Registrar eventos inmutables y ordenados de inicio, etapas, espera de decisión, resultado y cierre. Registrar error tipado y referencias a evidencias acotadas; nunca secretos ni prompts completos en el journal común.
- Una interacción puede tener ramas separadas de modelo, enrutamiento, creación/uso, revisión de Pixy, reparación e informe. Las entregas tienen su propia rama y acuses por fragmento. Un mensaje de aprobación es otra interacción enlazada al ciclo que decide.
- No marcar una operación larga terminada porque el webhook fue aceptado o porque el inbox terminó de procesar. La tarea y la entrega conservan estados propios. Un padre terminado no acredita que todas las ramas hijas hayan terminado.
- Conservar `waiting` mientras exista una decisión pendiente; al reiniciar, registrar interrupción/estado incierto y reconciliar con los recibos. No retransmitir ni repetir una VM/worker por ausencia de un evento de cierre.
- El diagnóstico debe consultar por ID y revisión exactos; los errores de otra rama son contexto histórico explícito y no pueden presentarse como causa comprobada. Registrar también cada llamada de modelo y su resultado de validación.
- Preferencia técnica: ampliar el almacenamiento SQLite/Events existente con relaciones y eventos transaccionales, sin introducir un servicio externo de observabilidad. La alternativa es mantener almacenes por órgano y construir un índice común; requiere igualmente cerrar los huecos anteriores.
- Cuando estado y evento residen en la misma base, confirmarlos en una sola transacción. Para bases separadas, usar una salida durable e idempotente: una escritura posterior en otra conexión/base no constituye por sí sola una garantía atómica. Conservar el ID original del evento al exportarlo y mostrar retrasos de publicación.

### Ejemplo ilustrativo de las alternativas

La orden «busca información sobre OpenClaw» podría producir este árbol. Los IDs siguientes son ilustrativos, no ejecuciones reales:

```text
I-100 interacción
├─ C-101 chat / interpretación
├─ S-102 uso de skill
│  ├─ P-103 revisión de permisos
│  └─ W-104 consulta web → fallo HTTP 429
├─ R-105 reparación del fallo de W-104
│  └─ P-106 decisión, si hace falta autoridad
└─ D-107 entrega de respuesta → acuse Telegram
```

**Diario común:** cada evento aporta `cycle_id`, padre y raíz a un historial consultable de forma uniforme. Heart pide I-100 y ve las ramas, sus errores y las esperas abiertas. Las tablas de recibos de cada órgano se conservan como evidencia específica.

**Diarios separados con índice:** Voice mantiene W-104/D-107, Pixy mantiene P-103/P-106 y Heart mantiene R-105. El índice conserva enlaces para reconstruir I-100. Hay que detectar y reconciliar enlaces pendientes o ausentes entre índice y recibos, además de corregir los huecos actuales.

La recomendación es el diario común sobre SQLite por simplicidad de consulta y detección de ramas sin cierre. Ambas alternativas conservan la autoridad de Pixy y la frontera de secretos de Voice. La elección sigue pendiente: el propietario pidió este ejemplo antes de decidir.

## Pruebas

Se reutilizan las suites canónicas. Las nuevas pruebas verifican garantías existentes sin afirmar que el contrato propuesto ya se cumple.

| Suite | Regresiones añadidas en esta auditoría | Resultado parcial |
|---|---|---|
| `tests/test_brain.py` | Persistencia de cascada y aislamiento concurrente de trazas/intent IDs | 22 aprobadas |
| `tests/test_mind.py` | Reconstrucción entrada→turno→tarea→entrega; reinicio/deduplicación; timeout de entrega sin falso historial ni replay | 29 aprobadas |
| `tests/test_hand.py` | Historial error→corrección→finish; operaciones distintas y correlación; rechazo conserva artefacto y motivo | 56 aprobadas |
| `tests/test_heart.py` | Dos casos independientes, reinicio, denegación y transiciones inmutables sin ejecutar Hand | Nueva prueba y suite conjunta aprobadas |
| `tests/test_pixy.py` | Decisiones hermanas separadas, reinicio y orden idempotente ligada a autorización y consumidor | 31 pruebas y 13 subpruebas aprobadas |
| `tests/test_kernel.py` | Informes separados con traza/caso/entrega; duplicado y entrega incierta sin repetir diagnóstico ni envío | 92 pruebas y 5 subpruebas aprobadas |
| `tests/test_voice.py` | Recibos separados/reinicio, caída de transporte sin replay, recibo→verificación→Dreams y entrega con metadata suministrada | 36 pruebas y 7 subpruebas aprobadas |

La prueba de metadata de Voice verifica conservación de los IDs suministrados al mailbox; no prueba que Coordinator ya los suministre todos. Validación conjunta final: `python -m pytest -q` → **303 pruebas y 25 subpruebas aprobadas en 191,73 s**. La auditoría añadió 16 pruebas; `git diff --check` sin errores. No se modificó código de ejecución como parte de esta auditoría.

Para certificar la corrección futura harán falta pruebas adicionales de invariantes: toda rama tiene inicio y cierre o espera explícita; padres y trazas no se cruzan; ninguna colisión externa sobrescribe evidencia; los recibos sobreviven a reinicio; no hay éxito sin acuse/postcondición; la reparación usa exclusivamente la rama objetivo. Acompañarlas con recorridos sintéticos en producción, identificados como tales.
