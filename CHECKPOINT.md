# CHECKPOINT - PROYECTO KHAI

**Fecha de referencia:** 30 de octubre de 2026.  
**Entorno de ejecución:** Windows PowerShell (Control) / Linux Azure (Producción).  
**Servidor Azure:**
- Host / Usuario: `carlos@20.237.211.250` (`/opt/khai`)
- Clave SSH: `.ssh/id_rsa`
- Contenedores Docker: `khai_core`, `khai_docker_proxy`

---

## 1. OBJETIVO GENERAL Y CONSTITUCIÓN

Khai convierte objetivos conversacionales en resultados verificables manteniendo la autoridad y la seguridad:
1. **Crear:** Hand investiga, construye, valida y prueba.
2. **Reparar:** Heart diagnostica basándose en la evidencia real (logs/tracebacks), Pixy aprueba y Hand corrige sin repetir errores.
3. **Aprender:** Dreams aprende de cada éxito o fallo técnico útil (episodios persistidos).
4. **Gobierno y Límite:**
   - Pixy custodia decisiones, permisos y contratos.
   - Voice posee el acceso a la red y los secretos; los workers corren en aislamiento estricto sin red directa.
   - Ninguna decisión sobre proveedores externos, costos o secretos se toma silenciosamente.

---

## 2. SITUACIÓN ACTUAL DEL PROYECTO (Última Sesión)

### Commits Recientes (`origin/main`)
- `de55ee7`: `refactor(tests): consolidar suites en los 8 organos canonicos y kernel (264 pruebas verificadas)`
- `3a79c88`: `refactor(brain): unificar controlador de modelos e inferencia en Brain y migrar scripts de soporte`
- `fb6de23`: `docs(curation): sistematizar documentacion en manual unico y centralizar reglas en GEMINI.md`
- `c69be09`: `docs(checkpoint): registrar despliegue de cascada y optimizacion de dolphin`
- `1225dc4`: `feat(dolphin): cascada high>mid>low>nano, timeouts optimizados y notificaciones concisas`
- `eeceb43`: `feat(dolphin): reestructurar modos nano (Gemini), low (CPU), mid (GPU 8B) y high (GPU 32B)`
- `36f843b`: `fix(registry): reutilizar descriptores aprobados y restaurar servicios autorizados en reintentos`
- `190ad90`: `fix(hand): limpiar borrador huerfano en reintentos de creacion`
- `360dabd`: `fix(gateway): propagar y validar servicios aprobados desde creation_consent y setup_proposals`
- `126fc0d`: `fix(heart): incluir DIAGNOSING en estados de tarea y limpiar recovery_case_id en reintentos`

### Brain Unificado e Infraestructura Acelerada (Completado y Verificado)
1. **Unificación de Brain (`app/mind/brain/`):**
   - El módulo `Brain` absorbió la coordinación de razonamiento, selección de modelos y ciclo de vida de VM (`ModelController`), conservando `DolphinController` como alias de compatibilidad.
   - Soporte de scripts unificado: `gcloud-ollama.ps1` fue reubicado en `app/mind/brain/gcloud-ollama.ps1`, eliminando el directorio raíz obsoleto `/Dolphin/`.
2. **Disco Persistente Redimensionado a 100 GB:** `dolphin-boot-disk` en GCP (`us-west1-a`) redimensionado y verificado en 100 GB (`sizeGb: 100`, status: `READY`). 100% cubierto por los créditos promocionales ($2.80 USD/mes por los 70 GB excedentes del Free Tier).
3. **Modelo 32B Descargado y Persistido:** Descargado en disco `qwen2.5-32b-abliterated` (19 GB, sha256 validado) junto a `dolphin3:8b` (4.9 GB).
4. **Timeouts Optimizados:** Timeout de espera a Ollama ajustado a 90s; warmup acotado a 90s (máximo 120s) con inferencia mínima de 1 token (`num_predict: 1`). Se eliminó el timeout excesivo de 180s.
5. **Cascada de Modos y Notificaciones Concisas:**
   - Cascada estricta: `high > mid > low > nano`.
   - Mensajes limpios y concisos durante la transición:
     - `[Conectando con Modo <nombre>]`
     - `[Modo <nombre> conectado]`
     - `[Modo <nombre> falló por: <motivo>]` (ej. `spot no disponible`)
     - En fallback final a Gemini: `[Modo nano conectado]`
   - Eliminados mensajes innecesarios de facturación y logs durante el uso habitual (la facturación se reserva para `/mode ?`).

### Consolidación de Suites de Pruebas Canónicas (264 Pruebas en Verde)
Se consolidaron todas las pruebas históricas fragmentadas en las **8 suites canónicas** correspondientes a los órganos y kernel del sistema:
- `tests/test_brain.py`: 18 tests (Inferencia, cascada de modos, ciclo de vida de VM, timeouts y banners).
- `tests/test_dreams.py`: 6 tests (Aprendizaje experiencial, notas de episodios y propuestas de evolución).
- `tests/test_hand.py`: 51 tests (Construcción de código, ciclo de herramientas, ejecución de recetas y validación).
- `tests/test_heart.py`: 24 tests (Diagnóstico con `CoreEyes`, análisis de logs reales y planes de recuperación).
- `tests/test_kernel.py`: 86 tests (Control store `control.sqlite3`, puertos de control, buzón de voz y migraciones).
- `tests/test_mind.py`: 20 tests (Extracción de intenciones, intenciones compuestas y coordinación de diálogo).
- `tests/test_pixy.py`: 26 tests (Autoridad de ejecución, gobernanza binaria `y`/`n`, emisión de recibos y contratos).
- `tests/test_voice.py`: 33 tests (Red externa segura, DNS pineado, TLS estricto, secretos aislados y cliente Telegram).
- **Total:** 264 pruebas pasando al 100% (0 errores, 0 fallos).

### Diagnóstico de la Prueba Sintética en Producción
Durante la ejecución del Paso 1 de `docs/VALIDATION.md` (*"crea una skill para conectarse a internet y buscar en Google..."*):
1. **Flujo de Gobernanza Verificado:** Pixy solicitó autorización explícita para la operación `list` de Google Custom Search; el usuario autorizó con `y` y la configuración quedó aprobada.
2. **Cuello de Botella Detectado:** Hand entró en un bucle de 14 llamadas repetidas a `web_search` hasta agotar el presupuesto de 16 turnos (`Presupuesto agotado: llm_calls`).
3. **Causa Raíz:**
   - La búsqueda web de Google Grounding en Gemini arrojó cuota agotada (`HTTP 429`).
   - El fallback de DuckDuckGo en Azure fue bloqueado por reto de bot (`HTTP 202`).
   - El fallback secundario de Wikipedia devolvió artículos genéricos de desarrollo ("GitHub", "Markdown") en vez de la documentación de la API de Google.
   - Hand quedó **completamente a ciegas**, sin poder leer la documentación oficial para redactar los 15 campos del `ServiceDescriptor`.

### Decisión de Modelo: Modo HIGH Activo
- **De ahora en adelante, todas las pruebas se realizarán en Modo High.**
- Perfil: `high` (GCE Spot `g2-standard-8`, NVIDIA L4 24 GB VRAM en `us-west1-a`).
- Modelo Ollama: `qwen2.5-32b-abliterated` (32B parámetros con alta capacidad de razonamiento técnico y generación estructurada de contratos JSON).

---

## 3. ACUERDOS Y DECISIONES: HERRAMIENTAS DE FÁBRICA

Se acordó dotar a Khai de un conjunto de herramientas primitivas (de infraestructura) y opcionales para evitar que el agente opere a ciegas:

### A. Herramientas Primitivas (Fundamentales)
1. **Motor de Búsqueda Web de Infraestructura (con Créditos de Google y Fallback):**
   - **Uso de créditos GCP:** Se utilizará la cuenta de Cloud Billing de Google Cloud (la misma de las VMs) para habilitar:
     - *Primaria:* Google Custom Search JSON API o Vertex AI Search en GCP (deducible directamente de los créditos de GCP).
     - *Fallback 1:* Instancia ligera / contenedor de **SearXNG** en GCP (metabuscador autoalojado financiado por créditos de cómputo) o API alternativa (Brave/Tavily).
     - *Fallback 2:* Catálogo de plantillas semilla pre-aprobadas en Voice.
2. **Lector Web de Documentación Técnica con Truncado Inteligente:**
   - **Decisión: APLICAR.** Voice incorporará extracción de texto/markdown limpio (Trafilatura/Jina) con truncado inteligente (~4.000 tokens priorizando esquemas, parámetros y ejemplos) para que Hand pueda leer la documentación oficial de los enlaces encontrados.
3. **Validador Local de Sintaxis e Importaciones (Static Code Checker):**
   - Se evaluaron las opciones (`ast.parse` vs `Pyflakes` vs `Ruff`). Queda pendiente seleccionar la variante final (recomendado `Pyflakes`/`Ruff`) para que Hand valide el código antes de entregarlo a Pixy.
4. **Plantillas Semilla de Descriptores y APIs (Catalog Seed):**
   - **Decisión: QUEDA PARA APLICAR.** Voice dispondrá de esqueletos pre-configurados para APIs REST estándar (Google, GitHub, Open-Meteo) para acelerar la creación y evitar alucinaciones de contratos.
5. **CoreEyes / Lector de Diagnóstico Interno:**
   - **Decisión: APLICADO.** Heart debe apoyarse siempre en `CoreEyes` para leer los logs reales del contenedor (`openclaw.log` y trazas de error) antes de proponer cualquier plan de reparación.

### B. Herramientas Opcionales
1. **Navegador Headless Completo (Playwright):** **NO** (Descartado; innecesario para consumo de APIs y muy pesado).
2. **REPL Sandbox Interactivo:** **NO** (Descartado; Khai no procesa volúmenes masivos de datos).
3. **Gestor Dinámico de Dependencias:** **APROBADO con arquitectura de venv general en `/tmp`** para no duplicar entornos virtuales ni dependencias entre skills.
4. **Parser de Documentos Multiformato (PDF, CSV, Excel):** **OK** (Aprobado para futuras interacciones de archivos por chat).
5. **Memoria Semántica Vectorial:** **ANOTADA EN ROADMAP** (Para cuando el historial de experiencias y skills de Dreams crezca a gran escala).

---

## 4. PLAN DE ACCIÓN PARA LA SIGUIENTE SESIÓN

1. **Configuración de Búsqueda y Modo High:**
   - Encender el nodo en modo `high` (`/mode high` -> GCE `g2-standard-8` con `qwen2.5-32b-abliterated`).
   - Configurar el proveedor de búsqueda en Voice respaldado por créditos de GCP / SearXNG.
   - Integrar el extractor web con truncado inteligente en Hand/Voice.
2. **Reanudar Recorrido Sintético en Producción (`docs/VALIDATION.md`):**
   - **Paso 1 y 2:** Ejecutar en sesión limpia:
     `crea una skill para conectarse a internet y buscar en Google. Cuando diga “busca en Google <consulta>”, devuelve una lista con título, enlace y descripción de cada resultado`
   - Aprobar en Pixy con `y` y verificar que Hand redacte el `ServiceDescriptor` y construya `google-search-skill` exitosamente con Qwen 32B.
   - **Paso 3:** Si ocurre algún detalle, verificar el flujo `/p corrige el ultimo error` con diagnóstico de Heart.
   - **Paso 4:** Inyectar `busca en Google OpenClaw skills` y comprobar resultados reales atribuidos a Google.
   - **Paso 5:** Inyectar `busca OpenClaw skills` (sin proveedor) y comprobar que Khai pregunte neutralmente por el proveedor.
   - **Paso 6:** Inyectar `usa Google como buscador predeterminado`, comprobar persistencia en SQLite (`control.sqlite3`) y re-ejecutar sin preguntas.
   - **Paso 7:** Probar reutilización de la skill sin recreación.
3. **Documentación Final:**
   - Volcar todas las evidencias observables en `docs/VALIDATION.md`.
