# CHECKPOINT - PROYECTO KHAI

**Fecha de referencia:** 30 de octubre de 2026.  
**Entorno de ejecución:** Windows PowerShell (Control) / Linux Azure (Producción).  
**Servidor Azure:**
- Host / Usuario: `carlos@20.237.211.250` (`/opt/khai`)
- Clave SSH: `.ssh/khai_azure_codex_ed25519`
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

### Commits Desplegados en Azure (`origin/main`)
- `eeceb43`: `feat(dolphin): reestructurar modos nano (Gemini), low (CPU), mid (GPU 8B) y high (GPU 32B)`
- `36f843b`: `fix(registry): reutilizar descriptores aprobados y restaurar servicios autorizados en reintentos`
- `190ad90`: `fix(hand): limpiar borrador huerfano en reintentos de creacion`
- `360dabd`: `fix(gateway): propagar y validar servicios aprobados desde creation_consent y setup_proposals`
- `126fc0d`: `fix(heart): incluir DIAGNOSING en estados de tarea y limpiar recovery_case_id en reintentos`

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
