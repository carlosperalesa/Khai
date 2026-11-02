# CHECKPOINT - PROYECTO KHAI

## Correcciones del Auditor, Alineación Constitucional y Fuente Única de Verdad (2 de noviembre de 2026) — trabajo autorizado

- **Resolución de Aprobaciones Ambiguas en Coordinator (pp/mind/coordinator.py):**
  1. *Priorización de Negaciones Compuestas:* En classify_reply_intent, las frases y locuciones de rechazo (cancela, bortar, olvídalo, 
o quiero, 
o lo hagas, claro que no, por favor no, para nada) se evalúan en primer lugar con confianza 0.95.
  2. *Erradicación de Falsos Positivos por Muletillas:* Se eliminaron del conjunto afirmativo términos ambiguos o de cortesía ('por favor', 'bien', 'bueno'), clasificándolos como unrelated a menos que acompañen una afirmación explícita.
  3. *Calificación Contextual de 'ok' y 'claro':* No otorgan aprobación si van seguidos inmediatamente por partículas de negación.

- **Alineación de Modelos, Fallback a Nano y Erradicación de Cascada Residual:**
  1. *Mensajería Constitucional Estricta:* llm_gateway.py extrae motivos limpios ('tiempo de espera agotado', 'spot no disponible') y emite exactamente [Modo {mode} falló por: {reason}] [Modo nano conectado] desplegando MODE_SELECTION_BUTTONS.
  2. *Eliminación de Métodos Residuales Engañosos:* Se eliminó mark_fallback_to_low en pp/mind/brain/controller.py.
  3. *Alineación Documental:* Erradicadas todas las menciones a cascadas automáticas en README.md, docs/ARCHITECTURE.md y rain/__init__.py.

- **Refinamiento de Detección Anti-Bucle en Heart (pp/heart/recovery.py):**
  1. *Corrección de Falsos Bucles en Intento 1:* Se eliminó ool(latest_failed.get('recovery_case_id')) de la condición is_loop. La detección de bucle exige estrictamente ttempts >= 2, antecedentes de recuperación (len(recovery_history) >= 1) o fallos repetidos de generación (hand_failures >= 2).

- **Sanitización de Seguridad y Consolidación de Fuente Única de Verdad:**
  1. *Exposición Perimetral Erradicada:* Eliminadas IPs públicas (20.237.211.250), usuarios SSH (carlos@) y hostnames internos en toda la documentación pública (docs/ARCHITECTURE.md, CHECKPOINT.md, etc.), reemplazados por marcadores neutros [AZURE_SERVER_IP], [AZURE_USER] y [AZURE_VM_NAME].
  2. *Archivo de Auditoría Histórica:* Se archivó udit_report.md en docs/historical/audit_report_20261031.md censurando snippets de tokens.
  3. *Plantilla Única de Secretos:* Consolidada una única fuente canónica en config/secrets.env.example, eliminando la copia duplicada en la raíz.
  4. *Erradicación de Conteos Dispersos:* Los conteos fijos de pruebas fueron centralizados exclusivamente en CHECKPOINT.md; README.md y docs/ARCHITECTURE.md delegan en make test.
  5. *Limpieza de Caracteres Corruptos:* Eliminados caracteres de control (, ) y tildes corruptas (?) en CHECKPOINT.md, docs/ARCHITECTURE.md y docs/VALIDATION.md.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Se añadieron pruebas adversariales en 	ests/test_action_planner_evolution.py (rechazo de negaciones compuestas y fillers) y en 	ests/test_model_contract.py (formato constitucional del mensaje de fallo).
  - Estado canónico certificado: **14 suites, 417 pruebas unitarias y 39 subpruebas pasando al 100% (0 errores, 0 fallos)** mediante make test (pytest -q).

---

## Evolución Cognitiva de Skills, Erradicación de Bucles Ciegos y Diagnóstico Transparente — trabajo autorizado

- **Evolución Dinámica de Skills ante Brechas de Cobertura (ActionPlanner en app/mind/action_planner.py):**
  1. *Erradicación del ValueError no controlado:* Cuando la verificación de cobertura detecta que un objetivo requiere capacidades ampliadas (ej. "pronóstico futuro" frente a "clima actual"), se elimina el `raise ValueError(...)` que simulaba una falla de sintaxis y causaba caídas del turno en cascada.
  2. *Protocolo Autónomo de Evolución (_evolve_step_for_coverage):* ActionPlanner transforma automáticamente el paso existente en una evolución candidata (`missing-<step_id>`, `is_evolution: True`, `previous_version_id: vid`), enriqueciendo la descripción, casos de uso y contratos con los requisitos no cubiertos (sin hardcoding de parámetros o nombres de ciudades).
  3. *Reverificación Cognitiva y Fallback Limpio:* Se vuelve a verificar la cobertura del plan evolucionado con el LLM. Si cubre la meta, se devuelve el plan con evolución para que Hand genere la nueva versión superadora; si no puede evolucionar, recurre a `web-search` si está en el catálogo, o retorna `None` limpiamente sin lanzar excepciones no controladas ni interrumpir el flujo.

- **Detección Anti-Bucle y Diagnóstico Activo en Heart (Recovery en app/heart/recovery.py):**
  1. *Inclusión de Tareas en Diagnóstico:* `failed_tasks` en `Recovery.diagnose` ahora incluye tareas en estado `DIAGNOSING`, evitando que tareas detenidas por excepciones queden invisibles ante auditorías de Heart.
  2. *Detección de Bucle de Recuperación (recovery_loop_detected):* Si una tarea presenta `attempt_number >= 2`, antecedentes de recuperación (`recovery_history`) o fallos repetidos, Heart emite un hallazgo crítico `recovery_loop_detected` con `remedy=None`. Al ser crítico sin remedio, `Recovery.design()` devuelve `None`, impidiendo que el sistema recete reintentos ciegos (`retry_task`) sobre el mismo código no funcional.
  3. *Distinción de Discrepancias Cognitivas:* Errores vinculados a cobertura o capacidades faltantes prescriben evolución de la capacidad (`evolve_skill:<name>`) en vez de reintentos idénticos.

- **Visibilidad Diagnóstica en Pixy y Contexto Conversacional (PixyMind / Recovery):**
  1. *Carga Contextual Completa de Tareas:* `Recovery.open_case()` ahora recupera todas las tareas pertenecientes a la conversación activa desde `control_tasks` (`json_extract(document, '$.conversation_id') = ?` o `source = ?`), garantizando que consultas de supervisión (como `/p revisa el bucle de recuperación...`) encuentren de inmediato la tarea objeto de consulta.
  2. *Erradicación de Falsos Negativos:* Al encontrar la tarea en `DIAGNOSING`, Pixy y Heart ya no emiten el falso negativo "La salud de infraestructura no verifica la denuncia conversacional; evidencia insuficiente", explicando con total transparencia el estado real de la tarea y el bucle detectado.

- **Humanización y Erradicación de Fugas de Código en Coordinator (app/mind/coordinator.py):**
  1. *Sanitización de Nombres de Archivos Internos:* Se eliminan referencias a archivos internos del sistema (`.py`) en las salidas hacia el usuario, reemplazándolos por descripciones técnicas comprensibles ("el módulo técnico").
  2. *Erradicación de Coletillas Mecánicas:* Se sustituye la fórmula robótica `"Propuesta de recuperación: ... ¿Apruebas? Responde sí o no."` por un mensaje natural y empático: `"Detecté un requerimiento técnico para completar tu objetivo: {clean_summary}. ¿Deseas que proceda con esta actualización?"`.
  3. *Botones Interactivos de Aprobación/Rechazo:* Se incorporan botones inline estructurados (`Sí, proceder` / `No, cancelar` con `callback_data` atómico a la autorización), soportando tanto selección táctil como respuestas conversacionales en lenguaje natural.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Se añade la nueva suite `tests/test_action_planner_evolution.py` (7 pruebas de evolución dinámica de skills, anti-bucles de Heart, visibilidad de tareas en DIAGNOSING por Pixy y sanitización de mensajes).
  - Suite canónica completa: **415 pruebas y 39 subpruebas aprobadas al 100% (0 errores, 0 fallos)** en 81.18s.

## Simplificación Cognitiva, Turnos Conversacionales Unificados y Aprendizaje Continuo (RL-like) — trabajo autorizado

- **Turnos Conversacionales Unificados (AssistantTurn en app/mind/turn.py):**
  1. *Eliminación de la Dicotomía Rígida one_outcome:* Se erradica la restricción excluyente que obligaba al modelo a elegir estrictamente entre hablar o actuar. Khai ahora puede emitir una respuesta humana y empática (reply) y al mismo tiempo convocar una herramienta técnica (tool_calls: [start_task, ...]) en el mismo turno sin error de validación.
  2. *Preservación de la Respuesta Humana en el Intent:* AssistantTurn.intent() conserva el reply textual generado, permitiendo que Coordinator notifique de inmediato al usuario explicando lo que hará mientras se inicia el trabajo en segundo plano, erradicando la sensación de robot mudo o terminal congelada.

- **Ciclo de Aprendizaje en Contexto (Datos -> Inferencia Algorítmica -> Refuerzo -> Aprendizaje) (Dreams / Mind):**
  1. *Recuperación de Lecciones Validadas (Dreams.get_active_learnings):* Se implementa la extracción dinámica de directrices activas y notas de refuerzo cognitivo desde SQLite (dynamic_guidelines) y MemoryEngine.
  2. *Inyección en el Prompt Contextual de Mind:* Las directrices aprendidas se inyectan dinámicamente en el prompt de sistema (APRENDIZAJES Y DIRECTRICES PREVIAS (Refuerzo)), adaptando el comportamiento de cualquier modelo de inferencia (Gemini Nano, Qwen High) sin reescribir código ni crear dependencias frágiles.
  3. *Retroalimentación Negativa por Cobertura Insuficiente:* Se conecta Coordinator._check_goal_result (assess_result) con Dreams.record_failure, registrando incidentes de calidad como experiencia negativa para ajuste de hipótesis.
  4. *Conexión en Bootstrap:* ModuleManager enlaza self.mind.dreams = self.dreams durante el arranque de subsistemas.

- **Desmantelamiento de Burocracia Heurística en Coordinator (app/mind/coordinator.py):**
  1. *Despacho Inmediato de Confirmación Conversacional:* Al transicionar a ACTION_REQUIRED o TOOL_CALL, si el turno incluye texto (intent['reply']), self.notify lo entrega de inmediato al outbox de la conversación.
  2. *Diálogo Cotidiano sin Preguntas Pendientes:* Expresiones naturales del usuario como "ok", "dale", "gracias" en ausencia de preguntas pendientes fluyen libremente hacia Mind como diálogo conversacional normal (ReplyResult(False)), eliminando respuestas burocráticas erróneas ("No hay una pregunta enviada pendiente").

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Se añade la nueva suite tests/test_conversational_humanity.py (6 pruebas de turnos unificados, recuperación de lecciones de Dreams, notificaciones inmediatas y diálogo fluido).
  - Suite canónica completa: **408 pruebas y 39 subpruebas aprobadas al 100% (0 errores, 0 fallos)** en 84.06s.

## Consolidación de Integridad de Flujos, Evaluación de Cobertura de Metas y Sincronización en Producción — trabajo autorizado

- **Árbol Durable Conversación -> Solicitud -> Operación (ConversationStore / Coordinator):**
  1. *Identificadores Durables y Procedencia:* Toda solicitud conserva inequívocamente su conversation_id, mensaje iniciador y estado. Las solicitudes hijas y transiciones de modelo no pierden su origen conversacional.
  2. *Protección contra Ambigüedades en Topics de Telegram:* Coordinator distingue la raíz de un supergrupo/tema de Telegram (thread_id), impidiendo que el mensaje base del hilo sea tomado como respuesta afirmativa a decisiones pendientes ajenas.
  3. *Botones Interactivos Inline en Decisiones:* Se inyectan botones interactivos (Sí / No vinculados al question_id) para aprobaciones (WAITING_CREATION_APPROVAL, WAITING_APPROVAL, WAITING_SERVICE_APPROVAL), eliminando ambigüedades en respuestas textuales breves.
  4. *Invalidación Transaccional de Preguntas Obsoletas:* Al cancelar, expirar o transicionar una tarea, sus preguntas abiertas asociadas en conversation_questions se marcan atómicamente como stale, evitando que respuestas tardías reactiven flujos extinguidos.
  5. *Aislamiento y Contexto Compartido:* La recuperación de contexto histórico entre conversaciones (shared_context) delimita claramente procedencia y se inyecta como referencia informativa, prohibiendo la propagación implícita de autorizaciones.

- **Evaluación Autónoma de Cobertura de Metas (app/mind/quality.py / _check_goal_result):**
  1. *Superación de Verificación Puramente Operativa:* Terminar un proceso técnico no acredita el cumplimiento del objetivo. assess_result evalúa determinista y semánticamente la evidencia entregada frente al objetivo completo (verificando fechas, unidades, error embebido, idioma y alcances solicitados).
  2. *Detección de Discrepancias:* Condiciones climáticas actuales no acreditan un pronóstico futuro; temperatura no satisface una petición de viento; noticias sin fecha no acreditan actualidad.
  3. *Enrutamiento a Heart:* Ante una cobertura insuficiente, _check_goal_result detiene la entrega de éxito, abre automáticamente un incidente cognitivo en Heart (cognitive_fault / cognitive_discrepancy) y notifica al usuario explicando qué faltó antes de reintentar.
  4. *Erradicación de Coletillas Mecánicas:* Se elimina la pregunta estática ¿Es correcto el resultado completo? en present_recipe, permitiendo que la interacción sea completamente fluida y solo consulte al usuario de forma orgánica cuando sea estrictamente necesario.

- **Transiciones Durables de Modo y Resiliencia (TransitionManager / VoiceMailbox):**
  1. *Desacoplamiento de Nano respecto a la Infraestructura:* La disponibilidad de Modo Nano se confirma y notifica de inmediato al usuario sin esperar la detención o limpieza asíncrona de VMs Spot en GCE.
  2. *Notificaciones Persistentes:* Los avisos de cambio de modelo ([Modo nano conectado], etc.) se registran de forma transaccional y durable en voice_outbox asociados a la conversación de origen.
  3. *Recuperación ante Reinicios y Demoras:* poll_notifications() detecta transiciones interrumpidas por reinicio o demoras anormales (>120 s), emitiendo avisos orientativos y permitiendo cancelación segura con /stop.

- **Eliminación y Cancelación Segura de Conversaciones (/delete):**
  1. Al invocar /delete, se cancelan atómicamente las tareas y transiciones activas de la conversación, preservando los recibos de los efectos externos ya completados y suprimiendo entregas tardías hacia conversaciones eliminadas.

- **Resolución de Desfase en Servidor de Producción (Azure):**
  1. Se detectó y resolvió el desfase en memoria de app/hand/hand.py y app/mind/coordinator.py dentro del contenedor Docker khai_core en Azure ([AZURE_SERVER_IP]). Al estar el código horneado en la imagen, se procedió a reconstruir el contenedor (docker compose up -d --build bot), verificando la coincidencia al 100% de los hashes SHA-256 con el repositorio Git.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Suite canónica completa: **402 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**, incorporando la nueva suite tests/test_flow_integrity.py (13 pruebas de integridad y regresión) y corrigiendo las aserciones de 	est_hand.py, 	est_voice.py y 	est_kernel.py.

## Clasificación Dinámica de Respuestas por LLM, Versionado Canónico de Skills (.001) y Política de Fallback — trabajo autorizado

- **Clasificación Dinámica de Respuestas Contextual (`classify_reply_intent` / `Coordinator`):**
  1. *Superación de Vocabularios Estáticos:* En concordancia con el principio de mínimo hardcoding, se erradican los conjuntos estáticos fijos de palabras clave en `Coordinator.handle_reply` y `Coordinator.answer_question`. La intención se clasifica dinámicamente mediante `Coordinator.classify_reply_intent` apoyado en el LLM (`generate_content`), evaluando semánticamente la respuesta del usuario en relación directa con la pregunta pendiente (`q_type`, texto de la pregunta, contexto) y determinando si es `approve`, `reject`, `modify` o `unrelated`.
  2. *Fast-Path Telegráfico:* Se preserva un atajo determinista ultra-rápido para respuestas telegráficas inequívocas (`y`, `n`, `si`, `sí`, `no`, `ok`, `cancel`, `1`, `2`), evitando latencia innecesaria de inferencia.
  3. *Enrutamiento Inmediato de Nuevas Metas:* Respuestas que inician con verbos o conectores directivos de nueva tarea (`además `, `ademas `, `crea `, `crear `, `busca `, `buscar `, `ejecuta `, `ejecutar `) se clasifican directamente como `unrelated` sin consumir cuotas de LLM, permitiendo a Mind procesar la nueva tarea sin congelar el flujo.

- **Protocolo de Versionado Canónico de Skills de 3 Dígitos (`Hand`):**
  1. *Nomenclatura Estricta con Punto:* Se implementa el estándar canónico `<skill_name>.001`, `<skill_name>.002`, etc., sustituyendo versiones arbitrarias (`v1`, `v2`).
  2. *Creación Inicial e Incrementos:* `Hand.create` asigna `.001` a la versión inicial y `.002`, `.003` a evoluciones sucesivas.
  3. *Protocolo de Archivo:* Las versiones anteriores se empaquetan en `history/<skill_name>.<version>.zip`.
  4. *Rollback Canónico no Destructivo:* La restauración (`Hand.restore(rollback=True)`) no destruye la secuencia histórica; desempaqueta el archivo seleccionado y genera el siguiente correlativo secuencial (ej. si existía `.002`, el rollback genera `.003` como versión activa con el contenido histórico restaurado).

- **Actualización Constitucional de Política de Inferencia y Fallback (Regla 5 de GEMINI.md):**
  - Ante la caída, fallo o rechazo de un perfil acelerado (ej. falta de disponibilidad Spot), Khai conmuta inmediatamente a **Modo Nano** (Gemini) garantizando la continuidad instantánea de la interacción sin bloquear al usuario, y despliega la botonera interactiva (`MODE_SELECTION_BUTTONS`) ofreciendo las opciones de modelo disponibles.

- **Auditoría e Investigación de Incidente en Servidor de Producción (Azure):**
  - Registro de incidente analizado en `control.sqlite3` (`recovery_cases` caso `case-20261102d3bb703b`):
    Intento de cambio a Modo High (`qwen2.5-32b-abliterated` en `g2-standard-8` con GPU NVIDIA L4 en GCE) arrojó `RuntimeError` por falta de stock Spot en la zona (`ZONE_RESOURCE_POOL_EXHAUSTED` / `spot no disponible`).
  - La conversación preservó el canal operativo en Nano; Pixy recuperó la traza `req_63174677-20261102002306-33957b` y el disco `dolphin-boot-balanced-20261031-b`.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Suite canónica completa: **389 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**, incorporando nuevas pruebas en `tests/test_human_interface.py` para clasificación semántica de intenciones (`approve`, `reject`, `modify`, `unrelated`), fast-path y el nuevo versionado canónico `.001`/`.002` con empaquetado zip.

## Erradicación de Amnesia, Protocolo de Evolución de Skills y Autopsias Cognitivas (Mind -> Heart) — trabajo autorizado

- **Erradicación de Amnesia en el Hilo Conversacional (`handle_reply` / `Coordinator`):**
  1. *Desbloqueo de Respuestas en Lenguaje Natural:* Se erradica la restricción que limitaba las respuestas a palabras binarias o UUIDs en `Coordinator.handle_reply`. Si existe una pregunta de alcance (`scope`) pendiente en la conversación, respuestas de texto libre (ej. "Santiago", "en Valparaíso", "celsius") son dirigidas directamente a `accept_scope_reply` sin interpretarse como turnos nuevos independientes ni congelar la tarea original en `WAITING_SCOPE`.
  2. *Flexibilización de Aprobaciones:* Se expande el vocabulario de confirmación aceptando afirmaciones naturales (`dale`, `ok`, `acepto`, `claro`, `procede`, `adelante`, `afirmativo`) y negativas (`cancela`, `rechazo`, `abortar`, `olvídalo`).
  3. *Revisión Dinámica ante Respuestas Mixtas o Clarificaciones:* Si el usuario responde a una propuesta de ejecución o creación con una clarificación, modificación o respuesta mixta (ej. "sí, y además dame el viento", "sabes? ahora quiero el clima de Valparaíso"), `Coordinator.answer_question` no rechaza mecánicamente con "responde sí o no", sino que invoca `revise_goal`, transicionando el objetivo a `RESOLVING_SCOPE` para que `requirements.resolve` re-planifique y actualice los requerimientos y parámetros en caliente sin romper el hilo conversacional.

- **Protocolo de Evolución de Skills (`ActionPlanner` / `Hand` / `Catalog`):**
  1. *Superación de Versiones Estáticas:* Las habilidades dejan de ser artefactos estáticos inmutables. Cuando una solicitud requiere datos adicionales que la versión actual de la habilidad no proporciona (ej. viento o pronóstico extendido en `clima-santiago-openweather`), o tras una corrección de usuario, `ActionPlanner` detecta la insuficiencia y declara la evolución en `new_skill` con el mismo `skill_id`.
  2. *Soporte de Evolución en `_convert_template_steps`:* Se permite que un paso faltante coincida con un `skill_id` ya activo, marcándolo como `is_evolution = True` y registrando `previous_version_id`.
  3. *Versionado Canónico:* `Hand.create` detecta la versión activa existente y genera automáticamente la versión siguiente (`v2`, `v3`) con tipo de operación `update`. Al validarse en contenedor Docker seguro y promoverse por Pixy, `SkillCatalog.promote` marca la versión anterior como `superseded` y activa la nueva versión.

- **Puente Mind -> Heart (Autopsias Cognitivas y Discrepancias Semánticas):**
  1. *Detección de Quejas y Correcciones del Propietario:* Se implementa la herramienta `report_issue(issue, task_id, skill_name)` en `turn.py` y `Coordinator.report_cognitive_issue`. Además, `Mind` detecta deterministamente patrones correctivos del usuario (ej. "No me vuelvas a dar grados Kelvin", "Te equivocaste", "El resultado está mal") para abrir de inmediato un caso de incidente cognitivo en Heart.
  2. *Taxonomía de Causa en Heart (`classify_cause`):* Se introduce el código de causa `cognitive_discrepancy` en `app/heart/recovery.py`. Heart diagnostica la insatisfacción semántica y genera un plan de remediación técnica dirigido a evolucionar la habilidad (`evolve_skill:<skill_id>`).
  3. *Aprendizaje en Dreams:* Toda corrección del usuario se registra como experiencia fallida en el historial episódico de `Dreams` para evitar la recurrencia de errores en turnos posteriores.

- **Erradicación de Coletillas Robóticas:**
  - Se eliminan de `Coordinator` los sufijos mecánicos de auditoría (`¿Es este el resultado esperado? (y/n)`). Khai entrega resultados limpios, fluidos y humanos directamente al usuario.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Se añaden 6 nuevas pruebas en `tests/test_human_interface.py` que validan:
    a) Respuesta natural a preguntas de scope sin amnesia.
    b) Aprobación conversacional flexible.
    c) Revisión y adaptación de objetivos ante respuestas mixtas.
    d) Reporte de discrepancias cognitivas hacia Heart.
    e) Evolución de skills activas en `ActionPlanner`.
    f) Entrega de salidas limpias sin coletillas robóticas.
  - Suite canónica completa: **384 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**.

## Gobernanza Administrativa Root (Pixy), Memoria de Preferencias (Mind) y Desburocratización — trabajo autorizado

- **Mind: Herramienta `set_user_preference` y Sincronización Inline de Preferencias:**
  1. *Contrato Pydantic y Router:* Se añade `PreferenceArguments` y `SetPreferenceCall` al discriminador `ToolCall` en `app/mind/turn.py`. Mind instruye en su system prompt la emisión de `set_user_preference(key, value)` ante datos de perfil (`sign`, `name`, `city`, `units`, `language`) o peticiones explícitas de recordar.
  2. *Sincronización Inline Determinista:* Se implementa `_sync_inline_preferences` en `app/mind/mind.py`. Detecta patrones de lenguaje natural (ej. "Yo soy cáncer, recuérdalo y dame mi horóscopo") y persiste de inmediato los atributos en `MemoryEngine.set_user_profile` y `MemoryEngine.set_preference('user_sign', ...)` antes del enrutamiento. Esto garantiza que comandos compuestos no sufran amnesia aunque el turno proceda directamente a planificar una tarea.
  3. *Despachador en Coordinator:* `Coordinator.dispatch_tool` normaliza claves comunes y actualiza tanto el perfil estructurado como las preferencias individuales, respondiendo con confirmación natural al usuario.

- **Desburocratización de Consultas de Solo Lectura:**
  - En `app/mind/coordinator.py` (`state == 'READY'`): Si una tarea emplea exclusivamente servicios con efecto de lectura (`effect == 'read'`, como búsquedas web en Brave o clima) y cuenta con entradas explícitas sin valores sustitutos por defecto inyectados, omite el estado burocrático `WAITING_INPUT_CONFIRMATION` y pasa directamente a ejecución (`SUBMITTED`). Las tareas con efectos de escritura externa (`effect == 'write'`) mantienen la confirmación mandatoria del usuario.

- **Autoridad Administrativa Nivel Root Custodiada por Pixy:**
  - *Separación de Capas:* Distinción formal entre **System** (inmutable en runtime por seguridad: OS, Docker daemon, aislamiento de red, binarios del Core), **Root** (identidad, personalidad en `agent.md`, directivas en `dynamic_guidelines`, custodiadas por Pixy) y **Userland** (skills generadas en sandbox).
  - *Gobernanza Conversacional:* Pixy procesa solicitudes directas (vía `/p` o chat) para cambiar la personalidad (`cambiar personalidad a: ...`) o registrar directivas de comportamiento (`regla de comportamiento: ...`).
  - *Confirmación Binaria Obligatoria:* Toda modificación root queda en estado pendiente (`pixy_pending_root_proposal`) en SQLite y exige confirmación explícita (`y`/`n`). Al responder `y`, Pixy persiste la modificación en `agent.md` (con soporte para tags XML y markdown en `MemoryEngine.update_agent_md`) o en `dynamic_guidelines`, emitiendo confirmación auditable. Al responder `n`, la propuesta se cancela sin alterar el sistema.

- **Corrección en Journal de Voice Mailbox:**
  - En `app/voice/mailbox.py`: Se corrige la invocación `journal.start('delivery')` para que herede limpiamente el `trace_id` de una interacción padre existente (`trace_id=(None if parent else metadata.get('trace_id'))`), erradicando la colisión `ValueError: A child must keep its parent trace; link independent interactions instead`.

- **Suite Canónica de Pruebas Unitarias Aprobada al 100%:**
  - Suite canónica completa: **378 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**, incluyendo 7 nuevas pruebas en `tests/test_human_interface.py` que validan sincronización de preferencias, omisión de confirmación en lecturas y ciclo completo de gobernanza root de Pixy.

## Humanización de la Interfaz, Mind como Wrapper y Localización Métrica — trabajo autorizado

- **Mind como Wrapper Bidireccional y Base de Conocimiento del Usuario:**
  1. *Inbound Wrapper (Contexto y Perfil de Usuario):* `MemoryEngine` (`app/mind/memory.py`) implementa la gestión persistente del perfil de usuario (`DEFAULT_USER_PROFILE`), configurando idioma (`es`), ciudad base (`Santiago`), país (`Chile`), unidades métricas (`metric`) y zona horaria (`America/Santiago`).
  2. *Extracción Inteligente de Parámetros:* `ActionPlanner` (`app/mind/action_planner.py`) recibe el contexto del usuario e infiere automáticamente los inputs requeridos a partir de la orden del usuario o desde sus preferencias base, eliminando preguntas redundantes cuando el usuario ya nombró la entidad o cuando existe un valor por defecto en su perfil.
  3. *Humanización de Preguntas de Requisitos:* `Coordinator` (`app/mind/coordinator.py`) erradica formulaciones técnicas como `¿Qué valores corresponden a city?`, sustituyéndolas por preguntas en lenguaje natural y empático (`¿Podrías indicarme la ciudad para continuar con la consulta?`) y autocompletando datos disponibles desde `MemoryEngine.get_user_profile()`.
  4. *Outbound Wrapper (Síntesis Contextual y Mínimo Hardcoding):* Se implementa `humanize_worker_output` en `Coordinator`:
     - Principio constitucional de mínimo hardcoding: delega en el LLM (`generate_content`) la tarea de adaptar y redactar el resultado de la herramienta de acuerdo a la naturaleza de la pregunta u objetivo del usuario (ej. generar un resumen fluido cuando se piden noticias, en lugar de volcar listas o diccionarios con tags HTML).
     - Aislamiento de contexto de presupuesto: la síntesis de presentación hacia el usuario desacopla temporalmente el contador del worker (`current_budget.set(None)`) para no ser bloqueada por estados de revisión (`UNVERIFIED`).
     - Fallback determinista seguro: si no hay LLM disponible o ante fallos transitorios, desempaqueta automáticamente JSONs (`weather_info`, `result`), suprime prefijos mecánicos (`Paso X:`, `\w+ info:`, `Resultado:`) y formatea limpiamente.
- **Directrices de Localización Métrica para Hand (`app/hand/hand.py`):**
  - Se añade directriz mandatoria en el prompt de generación de código: todo servicio externo debe configurarse con `units='metric'` (grados Celsius) y `lang='es'`, redactando mensajes completos y fluidos para personas, sin prefijos ni emoticones.
- **Suite de Pruebas Unitarias Aprobada al 100%:**
  - Nueva suite canónica `tests/test_human_interface.py` (8 pruebas unitarias) cubriendo deserialización limpia de JSON, eliminación de prefijos mecánicos, traducción climática, viñetas multiclave, humanización de preguntas de scope y persistencia de perfiles en `MemoryEngine`.
  - Suite canónica completa consolidada: **373 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**.

## Continuidad de modelos, contratos y validación de producción — trabajo autorizado

- **Reanudación y despliegue autorizados por el propietario:** Se ejecutan las 5 acciones prioritarias acordadas: consolidar documentación, commit y push a `origin/main`, despliegue en Azure (`[AZURE_USER]@[AZURE_SERVER_IP]`), repetición conversacional de `busca` con Brave en Telegram y verificación de hostkey SSH / telemetría GPU Spot.
- **Optimización de carga y residencia en GPU para Modo High:** Se implementó la aceleración de I/O de disco (Opción 1) mediante `startup-script` en la metadata de la VM Spot en [controller.py](file:///d:/OneDrive/GitHub/dev/Khai/app/mind/brain/controller.py) y [gcloud-ollama.ps1](file:///d:/OneDrive/GitHub/dev/Khai/app/mind/brain/gcloud-ollama.ps1), configurando `blockdev --setra 8192` (8 MB de readahead) para saturar el bus del disco de 100 GB. Se implementó la residencia de modelo (Opción 3) mediante `keep_alive: 30m` en [llm_gateway.py](file:///d:/OneDrive/GitHub/dev/Khai/app/voice/gateway/llm_gateway.py) y `controller.py`, elevando el timeout de precarga a 180 s para absorber la lectura en frío de `qwen2.5-32b-abliterated` (19,85 GB) y asegurar 0 segundos de carga en turnos posteriores mientras la VM esté activa. Se conserva la variante *abliterated* para evitar filtros de censura en tareas técnicas y del sistema. La Opción 2 (ampliar disco) fue descartada para mantener costo extra en $0.
- **Resolución de incidentes de producción (Tool-calling y Fallback reactivo):** Aprobado e implementado el plan de compatibilidad de herramientas y respuesta no bloqueante:
  1. *Fallback reactivo a Nano:* Ante caídas de conexión o fallos de modelos locales, la inferencia no bloquea al usuario esperando arrancar VMs Spot síncronas (35–120 s); responde inmediatamente mediante Nano, emite `[Error en modo <nombre>, pasando a Nano ¿deseas otro modo?]` y ofrece la botonera interactiva con los perfiles disponibles (`MODE_SELECTION_BUTTONS`).
  2. *Homogeneización de `thought_signature`:* Todos los tool-calls del sistema incorporan uniformemente `thought_signature` (con valor base64 wire-format `CgA=` como fallback compatible con Google/Protobuf), evitando que turnos históricos originados en Ollama provoquen `HTTP 400 Bad Request` al continuar la conversación en Gemini.
  3. *Catálogo de modelos verificado:* Comprobados 50 modelos disponibles en Google Cloud API (`gemini-flash-lite-latest`, `gemini-2.5-flash`, `gemini-3.1-flash-lite`).
- **Enmascaramiento y saneamiento de mensajes para el usuario:** Se erradica la exposición de fontanería interna en la interfaz de chat (Telegram):
  1. *Supresión de identificadores crudos:* Se eliminan prefijos y menciones a UUIDs internos (`task_...`, `case-...`, `auth_...`, `run_...`, `transition_...`).
  2. *Eliminación de taxonomías técnicas y metáforas:* Se suprimen términos internos entre paréntesis como `(syntax_or_code)` o `(unknown_contract)`. Se elimina la expresión "plan médico de Heart", sustituyéndola por terminología rigurosa ("propuesta de recuperación").
  3. *Silenciamiento de auditorías internas en el chat:* Los eventos de cierre de casos de Pixy (`Pixy: Caso de recuperación ...`) se redirigen a los logs del sistema (`logger.info`), notificando al usuario únicamente mensajes orientados a la experiencia humana (expiraciones o fallos definitivos).
  4. *Limpieza de comandos de modo:* Las respuestas a `/mode` y `/m?` ocultan los hashes de transición manteniendo la indicación de progreso y opción de cancelación (`/stop`).
- **Precision diagnóstica y normalización canónica en descriptores (Hand / Voice):**
  1. *Error tipado por campo en `validate_descriptor`:* Se especifica el campo infractor (`service_id`, `operation` o `version`), el valor rechazado y el patrón contractual (`^[a-z][a-z0-9_-]{0,63}$`), evitando que Hand caiga en `AgentExhausted` por corregir el campo erróneo ante versiones no alfanuméricas (ej. `2.5`).
  2. *Normalización canónica de nulos en lectura:* `validate_descriptor` asigna por defecto `effect_contract: None` en operaciones de efecto `read`, y `credential_ref: None` con cabeceras estándar si no se declaran credenciales, previniendo fallos espurios de esquema cuando el LLM serializa JSON omitiendo valores nulos.
  3. *Instrucción de aislamiento de credenciales en `describe_service`:* Se orienta a Hand para declarar secretos exclusivamente en `credential_ref` y `credential_header`, impidiendo el filtrado de API keys en parámetros o URLs.
- La suite completa canónica consolidada aprobó localmente: **363 pruebas y 39 subpruebas aprobadas (0 errores, 0 fallos)**. Incluye pruebas de serialización de firmas, botonera reactiva y enmascaramiento de identificadores en `tests/test_model_contract.py`.
- `dd00b6d` desplegado en Core/proxy; HTTPS 200 validado y cuatro quick_check ok. Las 43 pruebas de Brain/contrato pasaron también dentro de la imagen aislada sin red ni datos/secretos de producción.
- Continuidad real: `/mode low`, fuente `telegram:1793499059227`, transición `transition_7c24cd28985c4c839c4ad626b73aaa64`, completada. Durante su precarga, el chat `telegram:1793499080747` terminó con Gemini `gemini-3.5-flash-lite` en 0,785 s de inferencia, entregado en mensaje 1709. Ramas separadas y acuses persistidos.
- La búsqueda `telegram:1793499157785`, tarea `task_04735224fdd64cfab3dbadcce5b3c8bd`, agotó 180 s de inferencia CPU y usó Nano con fallback registrado. El JSON de Nano extrajo la consulta actual correctamente, pero no acredita el contrato de low. La receta posterior falló antes del worker: `input.count` opcional no estaba suministrado. No se repitió sin cambios.
- Pixy recibió primero un informe (`telegram:1793499429488`) y luego una orden explícita de reparación (`telegram:1793499581411`). Heart abrió `case-20261031b9e7bfd0`, pero propuso recrear web-search pese al pin existente: propuesta incorrecta, rechazada mediante `n` a la pregunta entregada 1716 (`telegram:1793499745187`). No se autorizó esa reparación.
- Correcciones en validación: bindings opcionales ausentes se omiten, conservando defaults de la skill; bindings requeridos o resultados previos ausentes fallan explícitamente. Excepciones genéricas de coordinación pasan a Heart con evidencia tipada, sin replay de efectos inciertos ni bucles si falla el diagnóstico. Heart reconoce la evidencia acotada del binding y no inventa una skill ausente ni una reparación de red; el caso histórico sin etapa conserva incertidumbre. Mind evita duplicar el esquema completo en el prompt de Ollama; el esquema nativo y la validación siguen completos. TypeError tras un transporte no provoca un segundo envío.
- Medición nueva sin Monitoring ni ampliación de plazo: low 72,068 s total / 32,645 s precarga; mid (`telegram:1793499535847`) 141,445 / 35,792 s, conectado con residencia GPU completa; high (`telegram:1793499776616`) 201,089 / 90,128 s, ReadTimeout durante tensores. El watchdog también apareció en mid exitoso; no prueba causa fatal. Informe `/opt/khai/data/voice/load-probes-20261101/comparison-low-mid-high.json`. Faltan contadores internos de disco/RAM/GPU porque cambió la hostkey SSH; serial y guestAttributes no permitieron verificarla, no se renovó confianza ni se habilitó ninguna API.
- Cancelación real: fuentes `telegram:1793500058066` y `telegram:1793500058153`; `transition_fd239fc06b3c439bb5d73bf93a0f1a93` quedó cancelled en unos 146 ms, sin operaciones remotas pendientes ni publicación tardía. Nuevo low solicitado `telegram:1793500106708` para la aceptación del arreglo; no constituye un resultado de búsqueda todavía.

- El propietario autorizó corregir low, repetir `busca` real, implementar Nano/cancelación y medir high/mid sin ampliar 90 s ni habilitar Monitoring.
- Mind separa sistema, historial de referencia y mensaje actual; Ollama recibe `AssistantTurn.model_json_schema()` en `format`, temperatura cero y contexto 8192. Se mantienen validación, hash y evidencia de rechazo, sin envolver respuestas mal formadas ni reintentar sin cambios.
- `TransitionManager` usa control.sqlite3 como autoridad de generación/modo efectivo. `/mode` devuelve ID sin esperar GCP, `/mode cancel` y `/stop` invalidan el intento; las respuestas tardías no publican perfiles obsoletos. Nano se prueba antes de retirar un servicio. Si no existe VM activa, una elección local explícita puede arrancar sin Nano. No hay cascada de perfiles desde inferencia.
- Las mutaciones remotas conservan intención y nombre de operación; un recibo ausente bloquea nuevas mutaciones, los nombres pendientes se consultan por GET. Reiniciar marca unknown sin replay. Ver `docs/MODEL_TRANSITIONS.md` para límites.
- Validaciones parciales anteriores: 38 pruebas de Mind/contrato y 35 pruebas más 2 subpruebas de Brain. La validación final posterior está registrada arriba; no sumar los conteos parciales.
- Respaldo previo verificado: `/opt/khai/data/backups/model-continuity-20261101T020711Z`, cuatro bases SQLite con quick_check ok. VM observada TERMINATED; no arrancada durante la preparación. Medición preparada por SSH con clave y hostkey existentes, sin gcloud SSH, agentes ni nuevas APIs.
- Validación consolidada previa: 345 pruebas y 30 subpruebas aprobadas en 332,95 s; luego Brain/contrato, 43 pruebas y 2 subpruebas en 26,52 s. No sumar estos conteos, que preceden a la suite final de 360/39 registrada arriba.

## Diario común aprobado — continuación del 31 de octubre

- **Desplegado y aceptado:** código `385f20d` en Core y proxy, imágenes reconstruidas. `/health` HTTPS 200 con certificado validado; `quick_check` ok en las cuatro bases. Suite final: 329 pruebas y 30 subpruebas aprobadas; última prueba de aislamiento de evidencia también aprobada.
- Imagen desplegada verificada en contenedor desechable sin red ni datos/secretos de producción: 13 pruebas de los siete caminos aprobadas. Modelos, transporte y Telegram usan fixtures en esas pruebas; no acreditan carga real de GPU ni una búsqueda conversacional con low. Informe `/opt/khai/data/voice/common-journal-image-checks-20261031.txt`.
- Aceptación real por webhook: `/help`, fuente `telegram:1793496603779`, raíz `cycle_40ef641869f036a9f94164b31fa5def3`, entrega confirmada 1705. Consulta `/p informe telegram:1793496603779`, fuente `telegram:1793496623524`, raíz `cycle_19300ceb36e0fb725fa6b5dbd91f80d6`, entrega confirmada 1706. Pixy generó su rama `pixy_report` y enlace `reports_on`; todas las ramas de ambas interacciones cerraron completed, sin reparación, autorización ni llamada de inferencia. Evidencia `/opt/khai/data/voice/common-journal-acceptance-20261031.json`.

- El propietario aprobó expresamente el diario común. Implementación: `app/kernel/journal.py`, SQLite de control con ciclos, eventos inmutables y enlaces. Contrato y límites en `docs/COMMON_JOURNAL.md`; ya no está pendiente elegir entre los dos diseños.
- Integración de los siete caminos, referencias hasta entregas y recibos por fragmento; IDs internos de herramientas evitan sobrescribir historia de otro run. Heart conserva evidencia antes de inferir, consulta el intento exacto y no propone una reparación de red para un rechazo de formato sin sustento. Informes por ID son consultas deterministas independientes de reparación.
- La cancelación de transiciones/puente nano y el cambio de contrato nativo de Ollama no forman parte de esta implementación. La auditoría no reconstruye logs antiguos perdidos ni implica reparación completa del fallo conversacional de low.
- Registro previo al despliegue: suites específicas aprobadas; entonces faltaban la suite conjunta y la aceptación en servidor. Ambas quedaron completadas según las evidencias anteriores.
- Primer conjunto completo: 328 pruebas y 30 subpruebas aprobadas en 278,79 s; 7 comprobaciones adicionales aprobaron timeout→Heart, búsqueda exacta de rechazo fuera de la ventana reciente y recuperación con intentos separados. Verificación final del código consolidado en curso (329 pruebas recogidas).
- Respaldo previo a migración: `/opt/khai/data/backups/common-journal-20261101T012151Z`, cuatro bases verificadas. La lectura inicial sin sudo no pudo inspeccionar el WAL del runtime; el respaldo verificado se hizo con el propietario de los archivos y no indicó corrupción.
- Validación consolidada: 329 pruebas y 30 subpruebas aprobadas en 273,40 s. Último endurecimiento de aislamiento: la prueba adicional de rechazo exacto aprobó que el caso descarte respuestas antiguas de otras tareas. Cambios listos para despliegue; aceptación real de la imagen y webhook todavía pendiente.

## Continuación del 31 de octubre: busca, dependencias, parser y disco
- Auditoría de los siete caminos terminada: 16 pruebas nuevas; suite conjunta 303 pruebas y 25 subpruebas aprobadas en 191,73 s, diff sin errores. Informe `docs/INTERACTION_AUDIT.md` con garantías, defectos reproducidos, prioridades y ejemplo de los dos diseños. Sin cambios de código runtime en esta auditoría. El propietario pidió más información antes de elegir el diseño; no asumir que aprobó el diario común.
- Auditoría por caminos autorizada expresamente: seis agentes especializados revisan modelo, chat, creación de skill, gobierno Pixy, informes Pixy y uso de skill; el coordinador revisa reparación y cruces. Informe en `docs/INTERACTION_AUDIT.md`, con pruebas nuevas en suites canónicas. Hay IDs durables por órgano, pero falta un árbol común de ramas y cierres; no declarar completada esa implementación. Elección de diario común Events/SQLite frente a índice sobre diarios separados pendiente del propietario.
- La búsqueda instrumentada rechazó el JSON de low: `rejection_5c4ca409c97d4ec18448a14f55a28327`, tarea `task_c8f28047363445d68630e3633b90612b`, caso automático `case-20261031e0f6d1d0`. Campos `name/arguments` fuera de `tool_calls` y objetivo antiguo del historial; sin worker. Heart/Pixy/Dreams recibieron el caso y la evidencia correcta, pero Heart propuso revisar la red basándose en un timeout ajeno. Esta sesión no aprobó ese plan. Corrección de esquema/contexto consultada y pendiente; no repetir sin cambiar ni declarar reparada la búsqueda.
- `19ce591` desplegado en Core: la repetición instrumentada con low (`telegram:1793493376291`, tarea `task_82d5c709e6b7415d919f5a52fd7114af`) terminó correctamente en 164,33 s. Mind emitió `set_default_provider` válido; la preferencia Brave/web_search quedó persistida con fuente y fecha, y Voice confirmó entrega (mensaje 1699). El rechazo anterior no se reprodujo; su JSON histórico es irrecuperable. No atribuir una causa ni declarar reparado el contrato sin evidencia del rechazo.
- Prueba genérica `busca` iniciada por webhook real: `telegram:1793493578111`, tarea `task_c8f28047363445d68630e3633b90612b`. La instrumentación de rechazo está activa; aceptación de esta búsqueda todavía en curso.
- Corrección local de observabilidad y autorreparación en validación: Voice persiste rechazos de `AssistantTurn` en `model_rejections` (respuesta acotada, hash completo, esquema, errores por campo, modelo, duraciones, tarea/revisión y traza). Redacta secretos conocidos; no almacena el prompt. El diario es inmutable y se recupera tras reiniciar.
- Mind devuelve una referencia de evidencia al rechazar el JSON. Coordinator pasa el objetivo a DIAGNOSING, registra el fallo y abre un incidente de Heart automáticamente; Senses entrega el rechazo exacto ligado a esa tarea. Pixy recibe la propuesta según su autoridad habitual, y Dreams registra el fallo anterior al despacho sin inventar un run. Los incidentes sin worker se deduplican por intento/revisión para no confundir recuperaciones sucesivas.
- El lector de errores aceptaba solo prefijos `req-`; ahora acepta también `req_`/`task_`, utilizados por las trazas reales de Telegram. Pruebas de reinicio/redacción/inmutabilidad y del recorrido Voice→Mind→Senses→Heart→Pixy→Dreams aprobadas; suite completa en curso.
- El propietario eligió investigar y corregir primero el JSON de low; no cambiar a nano para saltar la prueba. El primer rechazo histórico solo conservó `ValidationError` y no se puede reconstruir su contenido: repetir con instrumentación, sin atribuir un campo concreto hasta observarlo.
- Validación de la observabilidad y derivación automática: 286 pruebas y 25 subpruebas aprobadas en 192,33 s. Aceptación de producción de estos cambios pendiente de repetición instrumentada con low.
- `bf4aa01` desplegado en Core y proxy con respaldo verificado `/opt/khai/data/backups/rejection-audit-20261101T003131Z` (31 de octubre, hora de Santiago). Low reactivado por webhook `telegram:1793493126610`: 67,39 s de activación total y 33,04 s de precarga, conectado. Se valida además que DIAGNOSING conserve un único job propietario mientras Heart consulta un modelo lento, evitando un diagnóstico simultáneo que cierre la tarea prematuramente.
- Protección de diagnóstico lento validada con las suites completas de Mind/Heart: 57 pruebas aprobadas; prueba concurrente confirma una única apertura de incidente mientras el primer diagnóstico sigue activo.
- Recuperación del parser completada: `case-20261031de7b1b47` RECOVERED, alcance `postcondition`, intento 2 con artefacto y versión originales. CSV `run_93d5211e94764d36a38f73d332ac395b`, PDF `run_a21bee5cdf2b4fdf948eb3c30e07b123`, XLSX `run_a0fe22c1193642b593d8bfe40ba69574`; versión activa `336203723246426888be8e1358d0a357`. Las tres salidas se contrastaron con fixtures independientes y Dreams registró éxito por ejecución. Informe `/opt/khai/data/voice/deployment-document-parser.json`.
- `9606b6e` desplegado en Core y proxy. Salud HTTPS verificada HTTP 200; `quick_check` ok en control, Voice, Dolphin y runtime Docker. Imagen de dependencias reutilizada por las tres ejecuciones: `sha256:7aa875728c7be42edbb85e57ed8ec1e84c83a884848a53fb853e75bbcfc903a8`.
- Aceptación conversacional pendiente: orden de preferencia `telegram:1793482754879`, tarea `task_18d27f9f287a4e64922aa4ea208546ae`. Low respondió tras casi 3 min, pero su JSON no cumplió el esquema de Mind (`ValidationError`); no se guardó preferencia ni se ejecutó búsqueda. Se consultó al propietario si continuar con nano o investigar primero el JSON de low.
- Informe de revisión paralela autorizado por el propietario: `docs/LOAD_TIMES.md`. Las afirmaciones de carga completa basadas solo en `offloaded` se corrigieron con referencia a la versión instalada.
- `f1bd6ad` desplegado en Core y proxy. `web-search` actualizada y verificada: tarea `task_bdcaed05ae8540b2b8ca3ed614d7a0f9`, ejecución `run_6cebad97c8a54e808186bc3ddf1b561b`, versión `79a56e394b684224b1d84b990ed5f998`. La preferencia de búsqueda y el recorrido conversacional completo siguen pendientes de aceptación.
- Disco `dolphin-boot-balanced-20261031-b` READY en `us-west1-b`, restaurado desde la instantánea verificada. Cambio de zona autorizado expresamente; Voice conserva ambos ajustes y Core los cargó al reiniciar. Fuente de la nueva prueba high: `telegram:1793481829521`. La VM G2 y WireGuard funcionaron; high volvió a agotar 90 s de precarga (144,83 s totales de activación), y mid también. No ampliar el plazo: el propietario pidió medir disco y GPU primero. Cloud Monitoring respondió 403 `SERVICE_DISABLED`; habilitación pendiente de decisión.
- Medición por serial de mid: Ollama iniciado 21:27:34 UTC, L4 detectada 21:27:59, inicio de tensores 21:28:06, mensaje `offloaded 33/33 layers` 21:28:33; la generación expiró 21:29:30. Corrección de interpretación: ese mensaje se imprime antes de `load_all_data` en llama.cpp b11901, usado por Ollama 0.40.12 observado en el servidor. No prueba que hayan terminado la lectura o transferencia de los pesos. Tamaños informados: Qwen 19.851.349.765 bytes, Dolphin 4.920.757.726 bytes.
- Derivación conversacional a Pixy/Heart `telegram:1793482022431`: caso `case-202610318ef1109f`, diagnóstico `model_activation_failed` vinculado a high, intento y traza correctos, sin reparación de una tarea ajena.
- Parser: primer intento `task_41e60cf26332491bb4c582dca425b8f4` falló al consultar hashes de PyPI por timeout TLS; el proxy confirmó 404 para su execution_id: no se despachó worker. Dos comprobaciones posteriores PyPI dieron HTTP 200. Heart registró `temporary_provider` en `case-202610319c4e1193`. Se añade manejo tipado de fallos previos al despacho y recuperación `resume_prepared_task`, elegida por el propietario para conservar código/versión aprobados después del diagnóstico y autorización de Pixy. Quedan bloqueadas ejecuciones inciertas, cambios de artefacto y planes que mezclen reanudación con otras acciones.
- Respaldo válido de 4 bases con integridad verificada: `/opt/khai/data/backups/tools-verified-20261031T212014Z`. Un intento previo se interrumpió al incluir `proxy-owner.sqlite3`, que es un archivo de bloqueo vacío, no una base; no se usa ese intento como respaldo válido.
- Validación de la corrección de recuperación: suite local 281 pruebas y 25 subpruebas aprobadas en 155,04 s; prueba adicional de Coordinator→Heart y reanudación del artefacto aprobadas.
- El propietario rechazó habilitar Cloud Monitoring API y eligió continuar únicamente con registros disponibles. No habilitar la API ni ampliar el plazo de precarga sin una nueva decisión; la medición por serial no equivale a un benchmark puro de disco.
- Revisión delegada expresamente por el propietario: high 54,70 s antes de precarga +90,13 s de precarga; mid 105,15+90,12 s; low 92,95+30,81 s y conectado. Cascada total 464,01 s (7 min 44 s). `/api/ps` confirmó Dolphin en CPU, contexto 4096, `size_vram=0`. Causa GPU todavía no demostrada. El techo publicado de balanced de 100 GiB es 168 MiB/s: lectura fría íntegra de Qwen sería al menos 112,7 s bajo ese supuesto, no una medición del servidor. No aumentar costo ni plazo automáticamente.
- Segundo intento del parser autorizado por Pixy/Heart: caso `case-20261031de7b1b47`, misma versión y artefacto, intento 2. Descarga de 5 wheels Linux verificada y almacenada con lock `8233cebee1bfbc8e48c15c9d2a3133b58a9350f0c4b46e0c7e05bee95a82845e`. El proxy rechazó el despacho porque su esquema HTTP omitía `dependency_lock`; se corrige el contrato y se conserva el mismo ID idempotente no despachado. También se transmite `goal_revision` en recuperaciones, preservando el fingerprint de recibos antiguos cuando vale 0.
- Trabajo preparado para despliegue: `web-search` usa la plantilla `busca {query:text}`. El propietario autorizó usar Brave para esta búsqueda genérica; la preferencia se registrará con fuente y fecha al activar la actualización, no por el mero hecho de instalar una skill.
- Voice prepara únicamente wheels de PyPI para CPython 3.10/Linux x86_64, con raíces fijadas, resolución transitiva y SHA-256 contrastado con la publicación oficial no retirada. La caché y su lock se verifican antes de reutilizarse. El proxy instala con `--network=none`, `--no-index` y `--require-hashes`; reutiliza una imagen/venv por lock. `/opt/venv` sustituye el `/tmp` histórico porque el tmpfs del worker es `noexec`.
- Hand incorpora `plan_dependencies`; `khai_documents.read_document` y el candidato `skills/document-parser` extraen PDF, CSV UTF-8 y XLSX dentro del worker. No incluyen OCR ni recepción de adjuntos Telegram. Límites y versiones en `docs/ARCHITECTURE.md`; pruebas con archivos reales y descarga real de 4 wheels verificados y reutilizados.
- Brain registra intentos, etapas, errores tipados, disco y traza. Senses entrega esos eventos a Heart en consultas de modelos; Heart identifica el punto de fallo sin inventar una causa ni reparar una tarea ajena. Corregida la importación obsoleta de `dolphin_controller` en el manejo de errores de inferencia.
- Prueba conversacional inicial: `/mode high`, fuente `telegram:1793479876848`. High, mid y low agotaron 90 s de precarga; la VM fue retirada al volver a nano. GPU L4 detectada; demora de descubrimiento CUDA y disco `pd-standard` de 100 GiB son observaciones, no una causa raíz demostrada. `/p` no obtuvo entonces telemetría de Brain, motivando la corrección anterior.
- Migración autorizada a `pd-balanced` de 100 GiB (~US$10/mes base, frente a ~US$4 de standard, sin cómputo ni respaldo temporal). Instantánea `khai-balanced-20261031` READY. La creación en `us-west1-a` terminó con HTTP 503 `ZONE_RESOURCE_POOL_EXHAUSTED`, razón `persistent_disk_availability`; no se cambió la configuración ni se borró el original. Se evalúan `us-west1-b/c`, con los tipos G2 y CPU presentes, misma subred regional y cuota SSD 500 GiB/uso 0. La disponibilidad real de capacidad no queda probada por esas consultas.
- Evidencia operativa: `/opt/khai/data/voice/disk-migration-20261031.json`. Mantener `dolphin-boot-disk` y la instantánea durante la validación. La continuidad con nano y la cancelación de transiciones siguen pendientes de implementación; no confundir telemetría con ese diseño completo.
- Validación previa: suite local 278 pruebas y 25 subpruebas aprobadas en 167,52 s; prueba adicional del proxy/build offline aprobada. Descarga real de dependencias: lock `6ddaa6102eb2a88922a3dff9f246d971d49846762745b108a53298679d80a023`, 4 wheels y reutilización verificada. Aceptación de producción de esta continuación: en curso; registrar resultados finales antes de declararla completada.

## Despliegue autorizado del 31 de octubre de 2026
- Completado en Azure con código `f4c845d`. Imagen Docker `khai-bot` reconstruida y `khai_core` reiniciado; proxy conservado. `/health` respondió HTTP 200 con certificado verificado y `{"status":"ok"}`. `PRAGMA quick_check` dio `ok` en `control.sqlite3` y `voice.sqlite3`.
- Brave autenticado y verificado: consulta real de infraestructura con 3 fuentes oficiales, seguida de ejecución de worker sin red directa, revisión de Pixy y recibo de Voice. `web-search` activa tanto en `versions` como en `skill_specs`; tarea de aceptación `task_b09af793c9b742a4979ccdd7b7df535d`, ejecución `run_5f381e6200bd45c686ffd4f9ce422150`, versión `c5ec15303ee848c1868be9a6611e0727`. Evidencia detallada en `docs/VALIDATION.md` y `/opt/khai/data/voice/deployment-web-search.json`.
- Respaldo previo de 3 bases SQLite: `/opt/khai/data/backups/deploy-20261031T204147Z`. La clave se transfirió por SSH mediante stdin y `Secrets.put`, conservando las demás credenciales; no se incluyó en Git ni en imágenes.
- Preparación: suite local verificada con 273 pruebas y 22 subpruebas aprobadas. La clave de Brave fue configurada por el propietario en el archivo local ignorado por Git; se transfiere exclusivamente a Voice, sin incluirla en código o imágenes.
- Alcance: integración Brave/web-search, herramientas básicas de Hand, diagnóstico acotado de Heart, verificación por evidencia y mensajes deterministas de Pixy. La continuidad mediante nano y transiciones cancelables sigue como diseño pendiente.
- Aceptación: se comprobó el recibo HTTP ligado a la ejecución, el esquema del resultado, el proveedor Brave y presencia de fuentes oficiales. La promoción se efectuó después de esas comprobaciones, sin establecer una preferencia de búsqueda predeterminada para el usuario.

## Trabajo local del 31 de octubre de 2026
- Política acordada de continuidad: conservar el modelo activo cuando pueda servir, usar nano como puente cuando sea necesario; si nano falla, informar y solicitar al usuario un perfil alternativo. Diseño pendiente de implementación.
- El candidato Google se eliminó del repositorio y fue sustituido por `skills/web-search`, con Brave como proveedor explícito. Código, SkillSpec y descriptor instalados y verificados en producción según el registro anterior.
- El descriptor histórico `google_custom_search.list` v1 aprobado en producción permite `key` como parámetro y no contiene referencia de credencial. No usarlo para pasar claves desde el worker: contradice la frontera de secretos de Voice. Su propuesta local de reemplazo se retiró al elegir Brave; los registros históricos del servidor no se han modificado.
- Diseño propuesto de continuidad con nano como puente y transiciones cancelables: `docs/MODEL_TRANSITIONS.md`; aún no implementado.
- Heart distingue postcondición verificada, entrega de conversación confirmada y evidencia insuficiente; Pixy usa texto determinista de aprobación. El fallo de cascada notificado no repite el último error ni la conexión nano.
- Hand incorpora comprobación sintáctica sin ejecutar, plantillas de descriptores no aprobadas y lectura acotada de URLs HTTPS proporcionadas por el propietario. Su búsqueda de infraestructura usa Brave mediante `config/research.yaml` y el transporte de Voice, con `BRAVE_SEARCH_API_KEY`. Se retiraron Grounding y los respaldos de buscadores del método `web_search`; Gemini sigue disponible como inferencia nano. Cambios desplegados con `f4c845d`.
- La plantilla de descriptor de Google se sustituyó por Brave. La configuración de búsqueda de infraestructura autorizada no convierte una skill instalada en predeterminada para el usuario. Los errores de clave, cuota o servicio se propagan sin cambiar de proveedor.
- El gestor dinámico de dependencias y el parser multiformato aprobados como opcionales siguen pendientes. El comprobador actual usa la biblioteca estándar y no resuelve imports ni sustituye un analizador completo.
- Heart no vincula consultas sobre cambios de modelo o conexión a una tarea reciente no relacionada. Sin telemetría de transición vinculada, informa evidencia insuficiente y no propone reparación; Pixy presenta ese resultado de forma determinista. Código desplegado; falta repetir los escenarios conversacionales de aceptación.
- La inspección inicial encontró el catálogo legado vacío y `google-search-skill` v1/v2 en borrador, con descriptores históricos de Google aprobados. Al finalizar este despliegue, el catálogo canónico `skill_specs` contiene `web-search` activa y verificada; los registros históricos de Google se conservan.
- Pendiente diseñar transiciones cancelables con servicio anterior disponible durante el arranque, telemetría correlacionada y control de costos. El acuerdo histórico de herramientas primitivas está en el checkpoint del commit `b183cd5`; varias quedaron pendientes.
## Continuaci?n del 1 de noviembre: Conectividad IPv4 en pasarelas Voice y Saneamiento Conversacional
- **Conectividad IPv4 priorizada y tolerancia a fallos IPv6 en Voice (`ExternalGateway` y `read_public_document`):** Se identific? y resolvi? una falla cr?tica de ruta (`[Errno 101] Network is unreachable`) en entornos de contenedores Docker (red bridge sobre Azure) al contactar destinos p?blicos con doble registro DNS (A y AAAA), donde direcciones IPv6 alfab?ticamente menores (ej. `2604:86c0:...` en `api.openweathermap.org`) eran seleccionadas por `sorted(ips)[0]` en un host sin rutas IPv6 externas. La conexi?n ahora prioriza direcciones IPv4 (`key=lambda ip: (1 if ':' in ip else 0, ip)`) e itera sobre todas las direcciones globales resueltas ante errores `OSError` antes de abortar.
- **Saneamiento universal y enmascaramiento conversacional:** Se reforzaron los patrones regex en `Coordinator.notify()` y `Coordinator._advance_scoped()` para eliminar completamente identificadores internos de fontaner?a (`sr_[0-9a-f]{32}`, `order_...`, `run_...`, `task_...`, `case-...`, `auth_...`, `transition_...`, `req_...`) de los mensajes visibles en Telegram, manteniendo las instrucciones al usuario claras, directas e institucionales. Se elimin? la menci?n de solicitudes internas en la captura privada de credenciales.
- **Validaci?n unitaria:** 56 pruebas y 12 subtests aprobados en `tests/test_voice.py` y `tests/test_model_contract.py`; 93 pruebas en `tests/test_mind.py` y `tests/test_hand.py`; 14 pruebas en contratos de servicios. Total consolidado de 363 pruebas unitarias y 39 subpruebas aprobadas al 100%.
- **Extensi?n presupuestaria en turnos interactivos:** Se extiende el presupuesto (`+16 llm_calls`, `+4 generations`, `+4 executions`) cuando el propietario autoriza expl?citamente la creaci?n de una capacidad (`accept_creation_approval` en Pixy) y cuando responde a preguntas de alcance (`accept_scope_reply` en Coordinator), impidiendo que flujos interactivos de creaci?n se agoten prematuramente.
- **Restricci?n can?nica `# MODE: SAFE` en contratos planificados:** El prompt de generaci?n y el validador `_finish_code` en Hand exigen obligatoriamente `# MODE: SAFE` para acciones de receta planificadas, impidiendo que el generador adopte el modo de servicio permanente `AGENT` que causaba `ValueError` al esperar una finalizaci?n s?ncrona formal.
