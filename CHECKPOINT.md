# CHECKPOINT - PROYECTO KHAI

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
