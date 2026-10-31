# CHECKPOINT - PROYECTO KHAI

## Continuación del 31 de octubre: busca, dependencias, parser y disco
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
