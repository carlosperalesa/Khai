# CHECKPOINT - PROYECTO KHAI

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

**Fecha de referencia:** 30 de octubre de 2026.  
**Entorno de ejecución:** Windows PowerShell (Control y Desarrollo) / Linux Azure (Producción).  
**Servidor Azure:**
- Host / Usuario: `carlos@20.237.211.250` (`/opt/khai`)
- Clave SSH: `.ssh/id_rsa`
- Contenedores Docker: `khai_core` (puertos 8443 TLS público y 8000 inspección interna), `khai_docker_proxy` (puerto 8001 red privada)

---

## 1. OBJETIVO GENERAL Y CONSTITUCIÓN

Khai convierte objetivos conversacionales en resultados verificables manteniendo la autoridad y la seguridad:
1. **Crear:** Hand investiga, construye, valida y prueba mediante herramientas persistentes; una llamada aislada al LLM nunca constituye creación.
2. **Reparar:** Error, evidencia y trayectoria vuelven al ciclo. Ante el primer fallo de tarea se escala a Heart (diagnóstico obligatorio apoyado en CoreEyes, deduplicación por huella y logs reales).
3. **Aprender:** Todo éxito verificado y fallo técnico útil deja una nota recuperable en Dreams.
4. **Gobierno y Límite:**
   - Pixy custodia decisiones, permisos y contratos; confirmación obligatoria binaria (`y`/`n`).
   - Voice posee el acceso a la red y los secretos; los workers corren en contenedores aislados sin red directa (`--network none`).
   - Ninguna decisión sobre proveedores externos, costos o secretos se toma silenciosamente.
5. **Principio Rector de Inferencia:** `llm = brain`. Brain es el único órgano de inferencia y razonamiento; selecciona y enruta internamente entre modelos (`high`, `mid`, `low`, `nano`) según la cascada estricta.
6. **Regla Operativa Obligatoria:** Actualización documental previa obligatoria: la documentación (`CHECKPOINT.md`, `docs/ARCHITECTURE.md`, `README.md`) se actualiza SIEMPRE antes de realizar cualquier commit o push a Git.

---

## 2. SITUACIÓN ACTUAL DEL PROYECTO (Última Sesión)

### Commits Desplegados en Azure (`origin/main`)
- `8218c27`: `feat(brain): cascada estricta high>mid>low>nano en complete/generation, fallback de maquinas spot y recuperacion en heart/dreams`
- `de55ee7`: `refactor(tests): consolidar suites en los 8 organos canonicos y kernel (264 pruebas verificadas)`
- `3a79c88`: `refactor(brain): unificar controlador de modelos e inferencia en Brain y migrar scripts de soporte`
- `fb6de23`: `docs(curation): sistematizar documentacion en manual unico y centralizar reglas en GEMINI.md`
- `c69be09`: `docs(checkpoint): registrar despliegue de cascada y optimizacion de dolphin`
- `1225dc4`: `feat(dolphin): cascada high>mid>low>nano, timeouts optimizados y notificaciones concisas`

---

### Hitos Técnicos Implementados y Verificados

1. **Unificación Conceptual `llm = brain` y Cascada Estricta:**
   - `Brain` es la fachada exclusiva de razonamiento e inferencia.
   - Cascada estricta implementada y verificada tanto en `generate_content()` como en `complete()` (ciclo de herramientas de Hand):
     - `high` (`qwen2.5-32b-abliterated` en GPU L4 Spot)
     - `mid` (`dolphin3:8b` en GPU L4 Spot)
     - `low` (`dolphin3:8b` en CPU Spot 8 vCPU)
     - `nano` (Gemini API Cloud Serverless)
   - El sistema **nunca se salta escalones** ni degrada directo a nano si los nodos acelerados están en la secuencia.
   - Notificaciones concisas y sin marcas: `[Conectando con Modo <nombre>]`, `[Modo <nombre> conectado]`, `[Modo <nombre> falló por: <motivo>]`, `[Modo nano conectado]`.

2. **Redundancia de Máquinas Spot para Modo High:**
   - Para evitar fallos por stockout en `us-west1-a`, el controlador intenta automáticamente en caliente tres máquinas con la misma GPU NVIDIA L4 de 24 GB y el mismo disco persistente de 100 GB:
     - `g2-standard-8` (primaria: 8 vCPU, 32 GB RAM)
     - `g2-standard-4` (alternativa 1: 4 vCPU, 16 GB RAM)
     - `g2-standard-12` (alternativa 2: 12 vCPU, 48 GB RAM)
   - Brinda 3 oportunidades consecutivas de levantar Qwen 32B antes de descender a `mid`.

3. **Higiene Periódica en Dreams y Autorreparación en Heart:**
   - **Dreams (`housekeeping`):** Audita periódicamente el espacio de trabajo (`data/hand`) purgando borradores huérfanos o carpetas ocultas inactivas de tareas ya finalizadas.
   - **Heart (`stale_draft_or_lock`):** Clasifica automáticamente colisiones por borradores residuales y ejecuta planes de remediación autorizados sin requerir reinicio del servidor.
   - **Mind (`ActionPlanner`):** Ante metas complejas con inputs ambiguos, degrada graciosamente a una meta atómica directa en Hand en lugar de abortar la coordinación.

4. **Blindaje de Secretos y Frontera de Red:**
   - Secretos duplicados (`secrets.env` y `GEMINI_API.txt`) eliminados de la raíz del proyecto; las credenciales residen exclusivamente en `data/voice/secrets.env`.
   - Endpoint público `@app.get('/health')` desplegado y verificado en Azure respondiendo `{"status":"ok"}`.
   - Excepciones de red acotadas en `telegram_gateway.py` y manejo de SQLite protegido en `dreams.py`.

5. **Consolidación de Pruebas Canónicas (264 Pruebas en Verde):**
   - 8 suites canónicas mapeadas 1:1 a los órganos de Khai:
     - `tests/test_brain.py`: 18 tests
     - `tests/test_dreams.py`: 6 tests
     - `tests/test_hand.py`: 51 tests
     - `tests/test_heart.py`: 24 tests
     - `tests/test_kernel.py`: 86 tests
     - `tests/test_mind.py`: 20 tests
     - `tests/test_pixy.py`: 26 tests
     - `tests/test_voice.py`: 33 tests
   - **Total:** 264 passed en 153s (0 errores, 0 fallos).

---

## 3. ESTADO DE PRODUCCIÓN (AZURE `20.237.211.250`)
- Contenedor `khai_core` activo y saludable (`Uvicorn running on https://0.0.0.0:8443`).
- Endpoint `/health` verificado respondiendo `{"status":"ok"}`.
- Webhook `/webhook` activo y protegido por HMAC (`HTTP 401 Unauthorized` ante peticiones no autenticadas).
- Disco persistente de inferencia: `dolphin-boot-disk` de 100 GB en GCP `us-west1-a` listo para montarse en Modo High.

---

## 4. PRÓXIMO PASO INMEDIATO (Validación Sintética en Telegram)
Reanudar el recorrido sintético de producción según [`docs/VALIDATION.md`](./docs/VALIDATION.md):
1. **Comando de Inicio en Telegram:**
   `/mode high` -> Verificar que Brain conecte con GCE Spot GPU L4 (`qwen2.5-32b-abliterated`) y emita `[Modo high conectado]`.
2. **Paso 1 y 2:**
   Enviar: `crea una skill para conectarse a internet y buscar en Google. Cuando diga “busca en Google <consulta>”, devuelve una lista con título, enlace y descripción de cada resultado`
3. **Paso 3:**
   Aprobar en Pixy con `y` y verificar que Hand redacte el `ServiceDescriptor` y construya la skill en Modo High sin timeouts.
