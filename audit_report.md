# Reporte Consolidado de Auditoría Técnica del Sistema Khai

**Fecha de Auditoría:** 31 de octubre de 2026 (UTC)  
**Coordinador Técnico:** `teamwork_orchestrator`  
**Subagentes Auditoría:** `code-auditor` (`teamwork_auditor`), `server-inspector` (`teamwork_explorer`), `qa-tester` (`teamwork_challenger`)  
**Modo de Auditoría:** Demo — Estrictamente Solo Lectura (Read-Only), sin modificación de código ni reinicios forzados  
**Repositorio:** `d:\OneDrive\GitHub\dev\Khai`  
**Servidor de Producción Evaluado:** Azure Linux (`carlos@20.237.211.250`)  
**Dictamen Global:** **SISTEMA OPERATIVO Y ESTRUCTURALMENTE ROBUSTO**, con incidentes críticos de configuración perimetral de credenciales y latencia en GPU Spot, e inconsistencias menores en herramientas de prueba y manejo de excepciones.

---

## 1. Resumen Ejecutivo y Metodología

Se ejecutó una auditoría técnica multidimensional sobre el sistema Khai coordinada en tres tracks paralelos independientes:
1. **Integridad del Código y Arquitectura (`code-auditor`)**: Verificación de contratos, límites de aislamiento (.agents/GEMINI.md), gobernanza en Pixy, manejo de excepciones y ejecución de la suite canónica completa.
2. **Infraestructura y Estado del Servidor (`server-inspector`)**: Inspección de procesos locales y remotos (Azure), escaneo de puertos de red, verificación de certificados y autenticación HMAC, y análisis de logs productivos recientes (`openclaw.log`).
3. **Calidad y Flujo de Reparación de Heart (`qa-tester`)**: Verificación empírica y pruebas sintéticas de estrés sobre el flujo de diagnóstico y autorreparación de Heart, validación de inmutabilidad en SQLite y comportamiento ante servidor local inactivo.

### Matriz Consolidada de Hallazgos

| Dimensión | Crítico (🔴) | Mejora (🟡) | Estable (🟢) | Total |
| :--- | :---: | :---: | :---: | :---: |
| **Código y Arquitectura** | 1 | 5 | 5 | 11 |
| **Infraestructura y Servidor** | 3 | 3 | 4 | 10 |
| **QA y Flujo de Heart** | 1 | 3 | 6 | 10 |
| **Total Consolidado** | **5** | **11** | **15** | **31** |

*(Nota: Los hallazgos transversales como la inactividad del host local y la falta de `pythonpath` en `pytest` fueron corroborados independientemente por múltiples subagentes).*

---

## 2. Hallazgos Priorizados: CRÍTICO (🔴)

Los hallazgos en esta categoría representan riesgos de seguridad, indisponibilidad operativa de componentes clave o fallos no recuperados en tiempo de ejecución.

### C-01: Credenciales y Claves Sensibles Duplicadas en la Raíz del Repositorio
- **Componente:** `secrets.env` (7,024 bytes) y `GEMINI_API.txt` (83 bytes) en la raíz del espacio de trabajo.
- **Evidencia Observable:**
  - Inspección de directorio en raíz: existen `secrets.env` y `GEMINI_API.txt`.
  - En `data/voice/secrets.env` existe un archivo idéntico de 7,024 bytes.
  - `GEMINI_API.txt` contiene una clave activa de Gemini API (`AQ.Ab8RN6KjMHYC...`).
  - `.agents/GEMINI.md` Sección 4 establece taxativamente:
    > *"Gestión de secretos: Los secretos residen exclusivamente en Voice; nunca se exponen en prompts, historiales, descriptores, repositorios ni código."*
  - `docs/ARCHITECTURE.md` Sección 3 ratifica: *"data/voice/secrets.env: Administrado exclusivamente por Voice."*
- **Análisis Forense:**
  Aunque ambos archivos están excluidos del control de versiones en `.gitignore` (confirmado mediante `git log --all --full-history -- "**/GEMINI_API.txt" "**/secrets.env"` que retornó 0 commits), su permanencia física en la raíz del proyecto vulnera la frontera de aislamiento constitucional. Cualquier montaje de volumen en contenedores Docker o copia de seguridad desatendida del repositorio expone las credenciales del sistema.
- **Acción Correctiva:**
  Eliminar `secrets.env` y `GEMINI_API.txt` de la raíz del repositorio, consolidando todas las claves requeridas exclusivamente en `data/voice/secrets.env`.

---

### C-02: Timeouts Recurrentes en Nodos de Inferencia Acelerada GPU (High / Mid / Low) en GCE Spot
- **Componente:** `app/voice/gateway/dolphin_controller.py` / `app/mind/brain/controller.py` en entorno Azure.
- **Evidencia Observable:**
  - Registros en `/opt/khai/data/logs/openclaw.log` del servidor de producción:
    ```text
    2026-10-30 16:32:27,161 WARNING [req_63174600-20261030163116-aac3a1] app.voice.gateway.dolphin_controller Activacion Dolphin high fallida (TimeoutError)
    2026-10-30 16:38:28,701 WARNING [req_63174602-20261030163429-584f0c] app.voice.gateway.dolphin_controller Activacion Dolphin low fallida (ReadTimeout)
    2026-10-30 16:45:44,517 WARNING [req_63174607-20261030164327-f5e7ff] app.voice.gateway.dolphin_controller Activacion Dolphin mid fallida (TimeoutError)
    ```
- **Análisis Forense:**
  La arquitectura de cascada de inferencia (`high > mid > low > nano`) experimenta fallos de conexión o tiempos de espera agotados al comunicarse con las instancias de Google Cloud Platform Spot GPU (`us-west1-a` con GPU NVIDIA L4 para `qwen2.5-32b-abliterated`). Como resultado, el sistema se ve forzado a degradar el procesamiento de lenguaje natural y planificación hacia el modo nano (Gemini), afectando la soberanía local de los modelos abliterados.
- **Acción Correctiva:**
  Verificar el script de aprovisionamiento en caliente `app/mind/brain/gcloud-ollama.ps1` y los límites de timeout en `dolphin_controller.py` para asegurar que las instancias Spot no se apaguen prematuramente durante ráfagas de solicitudes.

---

### C-03: Error no Recuperado en Planificador de Acciones de Mind (`ValueError: Invalid composition inputs`)
- **Componente:** `app/mind/action_planner.py:289` y `app/mind/coordinator.py:1791`.
- **Evidencia Observable:**
  - Traza de excepción en `/opt/khai/data/logs/openclaw.log`:
    ```text
    2026-10-30 16:58:52,405 ERROR [-] app.mind.coordinator Error de coordinación
    Traceback (most recent call last):
      File "/app/app/mind/coordinator.py", line 282, in _run_task
        self.advance(task)
      File "/app/app/mind/coordinator.py", line 1546, in advance
        self._advance_scoped(task)
      File "/app/app/mind/coordinator.py", line 1791, in _advance_scoped
        proposal = planner.propose(intent.get('extracted_goal') or request, lambda spec:
      File "/app/app/mind/action_planner.py", line 289, in propose
        raise ValueError('Invalid composition inputs')
    ValueError: Invalid composition inputs
    ```
- **Análisis Forense:**
  Cuando un objetivo solicitado por el usuario requiere descomposición en múltiples acciones y las intenciones extraídas no satisfacen la estructura esperada por `ActionPlanner`, se eleva un `ValueError` no atrapado dentro de `_advance_scoped`. Esto aborta abruptamente el avance de la tarea en el coordinador sin registrar un resultado ordenado de fallo en `ControlStore`.
- **Acción Correctiva:**
  Manejar la excepción `ValueError` en `coordinator.py:1791`, registrando un diagnóstico estructurado y permitiendo a Heart reorientar la descomposición de la meta.

---

### C-04: Bloqueo de Hand por Borrador Inconcluso Huérfano (`An unfinished draft already exists`)
- **Componente:** `app/hand/hand.py:410`.
- **Evidencia Observable:**
  - Registro de colisión en `/opt/khai/data/logs/openclaw.log`:
    ```text
    File "/app/app/hand/hand.py", line 410, in create
      raise ValueError('An unfinished draft already exists')
    ValueError: An unfinished draft already exists
    ```
- **Análisis Forense:**
  El método `Hand.create()` valida que no existan borradores pendientes antes de iniciar la construcción de un nuevo artefacto. Si un ciclo de creación previo es interrumpido de manera anómala (por timeout o desconexión), el borrador permanece en estado huérfano. Todas las invocaciones subsiguientes de `create` fallan de inmediato, bloqueando el pilar de Creación hasta una intervención manual.
- **Acción Correctiva:**
  Implementar un mecanismo de caducidad (TTL) o recuperación en `Hand` que descarte o archive borradores huérfanos inactivos si la tarea creadora ya no está activa en el coordinador.

---

### C-05: Inactividad del Servidor en el Entorno Local de Desarrollo (Windows)
- **Componente:** Ingress local de Khai / Puertos 8000, 8443, 8001, 8080 en Windows.
- **Evidencia Observable:**
  - Verificación de sockets locales TCP en PowerShell:
    ```powershell
    .venv\Scripts\python.exe -c "import socket; ports = [8000, 8443, 8001, 8080]; print({p: socket.socket().connect_ex(('127.0.0.1', p)) for p in ports})"
    # Salida: {8000: 10061, 8443: 10061, 8001: 10061, 8080: 10061} (WSAECONNREFUSED)
    ```
  - `Get-Process -Name *python*, *uvicorn*, *docker*, *khai*` arrojó 0 procesos del servidor activos.
  - El daemon de Docker no está instalado en el host local de Windows (`CommandNotFoundException`).
  - `coordinator.lock` se encuentra libre (`msvcrt.LK_NBLCK` confirma que ningún proceso lo retiene).
- **Análisis Forense:**
  Khai opera bajo un esquema desacoplado: el host local de Windows se utiliza para desarrollo estático, edición y ejecución de suites de pruebas (`tests/`), mientras que el entorno de ejecución vivo de producción reside en Azure Linux (`20.237.211.250`). En la máquina local, el servidor está **completamente inactivo/caído**, impidiendo pruebas sintéticas de red en vivo contra un host local sin forzar arranques manuales.
- **Acción Correctiva:**
  Documentar explícitamente en `README.md` la separación entre el host de desarrollo (Windows) y el host de ejecución (Azure), y proveer un arnés de simulación local (mock server) para pruebas de integración sin requerir infraestructura remota.

---

## 3. Hallazgos Priorizados: MEJORA (🟡)

Los hallazgos en esta categoría corresponden a optimizaciones operativas, discrepancias documentales y prácticas de código que dificultan el mantenimiento o las pruebas.

### M-01: Falla de Ejecución Directa de `pytest` por Ausencia de Configuración `pythonpath` en Raíz
- **Componente:** Raíz del repositorio / `tests/test_*.py`.
- **Evidencia Observable:**
  - Ejecutar `.venv\Scripts\pytest -v` produce 8 errores de importación en fase de recolección:
    ```text
    ERROR tests/test_brain.py: ModuleNotFoundError: No module named 'app'
    ERROR tests/test_heart.py: ModuleNotFoundError: No module named 'app'
    ERROR tests/test_mind.py: ModuleNotFoundError: No module named 'app'
    ```
  - En contraste, `.venv\Scripts\python -m pytest -v` o `pytest -o pythonpath=.` ejecuta los 264 tests con 100% de éxito.
- **Análisis Forense:**
  El repositorio no cuenta con un archivo `pytest.ini` o `pyproject.toml` en la raíz. La Constitución (`.agents/GEMINI.md` Regla 6) exige la ejecución de `pytest` antes de confirmar cualquier cambio; al no configurar `pythonpath`, la invocación estándar de la CLI de `pytest` falla.
- **Recomendación:**
  Crear `pytest.ini` en la raíz del repositorio con el siguiente contenido:
  ```ini
  [pytest]
  pythonpath = .
  testpaths = tests
  ```

---

### M-02: Discrepancia entre la Regla Constitucional de Escalación a Heart e Implementación en Mind
- **Componente:** `app/mind/coordinator.py:2239-2249` vs `.agents/GEMINI.md` Pilar 2.
- **Evidencia Observable:**
  - `.agents/GEMINI.md` Pilar 2 define: *"dos errores equivalentes escalan a Heart (diagnóstico obligatorio apoyado en CoreEyes y logs reales)"*.
  - En `coordinator.py:2239`, ante cualquier estado `status in {'failed', 'timeout', 'cancelled'}` o `returncode != 0`:
    ```python
    case = self.manager.heart.recovery.open_incident_case(task, result)
    self.store.update(tid, expected={'RUNNING'}, state='DIAGNOSING', ...)
    ```
- **Análisis Forense:**
  La escalación a Heart ocurre en el **primer** fallo de ejecución de una tarea, en lugar de esperar dos errores equivalentes. Si bien la deduplicación de Heart previene planes redundantes, existe una divergencia conceptual entre la especificación gobernante y el código en producción.
- **Recomendación:**
  Alinear la regla en `.agents/GEMINI.md` aclarando que el diagnóstico de Heart se activa desde el primer fallo de tarea con deduplicación por `incident_key`, o implementar un contador de reintentos en `ControlStore` previo a la apertura del caso.

---

### M-03: Ausencia de Endpoint HTTP `GET /health` en el Servidor FastAPI
- **Componente:** `app/voice/voice.py:397-402`.
- **Evidencia Observable:**
  - En los logs del contenedor en Azure (`docker logs khai_core`):
    `INFO: 172.18.0.1:51948 - "GET /health HTTP/1.1" 404 Not Found`
  - En `app/voice/voice.py` únicamente se declara `@app.post('/webhook')`.
  - La función `_format_health(manager)` existe en la línea 238 pero solo se invoca como comando de texto dentro de Telegram.
- **Análisis Forense:**
  Sondas de salud externas, balanceadores o servicios de monitoreo de contenedores reciben `404 Not Found` al verificar la operatividad del servicio vía HTTP.
- **Recomendación:**
  Declarar una ruta pública ligera `@app.get('/health')` en `app/voice/voice.py` que responda `{"status": "ok"}` e informe el estado general del runtime.

---

### M-04: Artefacto Residual `MagicMock/mock.control_db` en la Raíz del Espacio de Trabajo
- **Componente:** Directorio `MagicMock/mock.control_db` en la raíz.
- **Evidencia Observable:**
  - Directorio existente en raíz: `MagicMock/mock.control_db`.
  - En `app/mind/coordinator.py:128-130`, `session_db` se inicializa con `getattr(manager, 'control_db', None)`.
  - Cuando los tests pasan un objeto `Mock()` como manager, `manager.control_db` devuelve un `MagicMock`.
  - `app/mind/conversation.py:25` invoca `migrate(path)` que a su vez llama a `Path(path).parent.mkdir(...)`, creando la carpeta en el sistema de archivos de Windows.
  - El directorio no está contemplado en `.gitignore` ni en `scripts/clean.py`.
- **Recomendación:**
  Añadir validación de tipo `isinstance(session_db, (str, Path))` en `coordinator.py` y agregar `MagicMock/` a `.gitignore` y `scripts/clean.py`.

---

### M-05: Uso de Bloque `except:` Desnudo en Pasarela de Telegram
- **Componente:** `app/voice/gateway/telegram_gateway.py:173-174`.
- **Evidencia Observable:**
  ```python
  try:
      response = requests.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
      response.raise_for_status()
      return response.json()
  except:
      pass
  ```
- **Análisis Forense:**
  Un bloque `except:` sin clase captura `BaseException` (incluyendo `KeyboardInterrupt` y `SystemExit`), suprimiendo silenciosamente fallos de red sin dejar registro en logs.
- **Recomendación:**
  Reemplazar con `except requests.RequestException as err:` y registrar el evento con `logger.debug(...)`.

---

### M-06: Supresión Silenciosa de Errores SQLite en Motor de Aprendizaje Dreams
- **Componente:** `app/dreams/dreams.py` (líneas 68, 91, 109, 124, 138, 171, 182, 187, 235, 241, 261, 272, 278, 296).
- **Evidencia Observable:**
  Más de 14 bloques `try: ... except Exception: pass` que descartan fallos durante la inserción en tablas de feedback o migración de hipótesis.
- **Análisis Forense:**
  La captura ciega de excepciones oculta posibles corrupciones de base de datos, bloqueos de concurrencia o violaciones de integridad referencial.
- **Recomendación:**
  Restringir la captura a `sqlite3.Error` y emitir advertencias estructuradas en el log de depuración.

---

### M-07: Omisión de Dependencias de Pruebas en `requirements.txt`
- **Componente:** `requirements.txt`.
- **Evidencia Observable:**
  El archivo lista dependencias productivas (`fastapi`, `uvicorn`, `docker`, `openai`, etc.), pero omite `pytest` y `pytest-subtests`.
- **Análisis Forense:**
  Dificulta el aprovisionamiento reproducible de nuevos entornos de desarrollo alineados con la Constitución.
- **Recomendación:**
  Crear `requirements-dev.txt` o incluir `pytest>=8.0.0` y `pytest-subtests` en `requirements.txt`.

---

### M-08: Divergencia entre Puertos en `docker-compose.yml` y Reglas de Seguridad en Azure NSG
- **Componente:** `docker-compose.yml:9` vs Azure Network Security Group.
- **Evidencia Observable:**
  `docker-compose.yml` mapea `8000:8000` y `8443:8443`. Sin embargo, pruebas de red hacia `20.237.211.250` confirman que el puerto `8000` está cerrado/filtrado por el firewall de Azure, permitiendo tráfico únicamente por `8443`.
- **Recomendación:**
  Documentar en el encabezado de `docker-compose.yml` que el puerto 8000 es de uso exclusivo para inspección interna/local.

---

### M-09: Notificación de Casos Terminales en `Recovery.tick()` Condicionada a `authorization_id`
- **Componente:** `app/heart/recovery.py:771`.
- **Evidencia Observable:**
  La consulta que recupera casos terminales para emisión periódica exige `WHERE authorization_id IS NOT NULL`.
- **Análisis Forense:**
  Casos que abortan en fases tempranas de diagnóstico (`FAILED` sin requerir autorización) no son emitidos en el generador de `tick()`, delegando su seguimiento exclusivamente al loop del coordinador.
- **Recomendación:**
  Permitir la notificación periódica de casos terminales no autorizados para mayor transparencia en las métricas de salud.

---

### M-10: Ausencia de Modo Mock de Servidor para Desarrollo Local
- **Componente:** Entorno de desarrollo en Windows.
- **Evidencia Observable:**
  Al no disponer de daemon de Docker ni proceso uvicorn en Windows, no existe un comando simple en `Makefile` para simular la recepción de webhooks de Telegram y probar la cadena completa de Voice en local.
- **Recomendación:**
  Añadir una receta en `Makefile` o script PowerShell para levantar un servidor de prueba con webhook simulado.

---

### M-11: Manejo de Expiración en Casos en Espera de Autorización
- **Componente:** `app/heart/recovery.py:expire_waiting`.
- **Evidencia Observable:**
  El mecanismo de expiración transiciona casos a `DENIED` correctamente, pero la retroalimentación al coordinador no genera un evento de alerta persistente en Telegram.
- **Recomendación:**
  Emitir una notificación de advertencia en el canal de administración cuando un caso médico expire por inacción del operador.

---

## 4. Hallazgos Priorizados: ESTABLE (🟢)

Los componentes en esta categoría demuestran madurez, alta confiabilidad, cumplimiento estricto de la Constitución (.agents/GEMINI.md) y arquitectura defensiva verificada empíricamente.

### E-01: Aislamiento Total de Red y Confinamiento de Workers Docker
- **Componente:** `app/voice/runtime/docker_engine.py:164-175` y `app/voice/runtime/docker_runtime.py:8-10`.
- **Evidencia Observable:**
  - `docker_runtime.py:8`: Si el payload contiene `'network': True`, eleva `PermissionError('Skills must use Voice Gateway')`.
  - `docker_engine.py:164-175`: La construcción de argumentos para el contenedor inyecta irrevocablemente:
    `--network none`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 128`, `--memory 512m`, `--cpus 1`, `--tmpfs /tmp:rw,nosuid,noexec,size=64m`.
- **Evaluación:** Cumplimiento total de la Sección 4 de GEMINI.md. Ningún worker puede acceder a la red directa ni escalar privilegios en el host.

---

### E-02: Blindaje de Red en Pasarela Externa Voice (`ExternalGateway`)
- **Componente:** `app/voice/gateway/external_gateway.py:266-300`.
- **Evidencia Observable:**
  - Validación de protocolo HTTPS estricto y puerto 443.
  - Bloqueo de direcciones no globales mediante `ipaddress.ip_address(ip).is_global` (impide SSRF contra localhost, 127.0.0.1, 10.0.0.0/8, 192.168.0.0/16).
  - DNS IP Pinning y verificación estricta de certificado TLS contra el nombre de host antes de emitir la petición.
- **Evaluación:** La salida a Internet está centralizada y protegida de manera determinista.

---

### E-03: Gobernanza y Autoridad Determinista en Pixy
- **Componente:** `app/pixy/pixy.py:64-68` y `app/pixy/authorization.py`.
- **Evidencia Observable:**
  - `check_authorization()` devuelve `False` y `consume_authorization()` eleva `PermissionError('trace_id no es una autorización')` conforme a ADR-0004.
  - Exigencia de tokens criptográficos de un solo uso (nonces SHA-256) vinculados a la huella de la acción y verificación de TTL.
- **Evaluación:** Cumplimiento riguroso del principio de no invención de autorizaciones y custodia de decisiones (GEMINI.md Sección 2).

---

### E-04: Servidor de Producción Azure Activo y Contenedores Operativos
- **Componente:** Host Linux Azure (`20.237.211.250`) y Docker runtime.
- **Evidencia Observable:**
  - Uptime continuo de 21 días (`00:00:57 up 21 days, load average: 0.01`).
  - Contenedor `khai_core` en ejecución con `make start` -> `python -m app.voice.voice`.
  - Contenedor `khai_docker_proxy` en ejecución con `uvicorn docker_proxy.proxy:app` en red interna bridge aislada (puerto 8001).
- **Evaluación:** La infraestructura central de ejecución se encuentra estable y disponible.

---

### E-05: Autenticación Criptográfica Estricta en Webhook Ingress (HMAC SHA-256)
- **Componente:** `app/voice/voice.py:401-402` en puerto HTTPS 8443.
- **Evidencia Observable:**
  - Peticiones externas sin token de autenticación a `https://20.237.211.250:8443/webhook` responden unívocamente con `HTTP 401 Unauthorized`.
  - Verificación en código: validación con `hmac.compare_digest` del encabezado `X-Telegram-Bot-Api-Secret-Token`.
- **Evaluación:** Ingress seguro inmune a accesos anónimos o no autorizados.

---

### E-06: Suite de Pruebas Canónica 100% Verde (264 Tests Pasando)
- **Componente:** 8 suites de pruebas unitarias e integradas (`tests/test_*.py`).
- **Evidencia Observable:**
  - Ejecución con `.venv\Scripts\python -m pytest -v`:
    ```text
    tests/test_brain.py ................. (17)
    tests/test_dreams.py ...... (6)
    tests/test_hand.py ................................................... (51)
    tests/test_heart.py ........................ (24)
    tests/test_kernel.py .................................................................................. (82)
    tests/test_mind.py .................... (20)
    tests/test_pixy.py .......................... (26)
    tests/test_voice.py ................................. (33)
    ============= 264 passed, 18 subtests passed in 377.34s =============
    ```
- **Evaluación:** Cero fallos, cero regresiones en toda la base del código.

---

### E-07: Suite Canónica del Módulo Heart Impecable (24 Tests Pasando)
- **Componente:** `tests/test_heart.py`.
- **Evidencia Observable:**
  - Ejecución verificada tanto con `python -m unittest tests.test_heart` (38.9s) como con `pytest -o pythonpath=. tests/test_heart.py` (34.4s).
  - Todos los 24 casos de prueba de diagnóstico, recolección de evidencia y ejecución de planes pasaron satisfactoriamente.

---

### E-08: Inmutabilidad Histórica de Transiciones en SQLite Respaldada por Triggers
- **Componente:** `app/heart/recovery.py:48-53` / Base de datos `control.sqlite3`.
- **Evidencia Observable:**
  - Intento de ejecución de `DELETE` o `UPDATE` sobre la tabla `recovery_transitions` eleva:
    `sqlite3.IntegrityError: recovery history is immutable`.
- **Evaluación:** La trazabilidad de incidentes de Heart no puede ser borrada ni alterada por ningún proceso en ejecución.

---

### E-09: Catálogo Cerrado de Acciones y Defensa Anti-Inyección en Planes de Heart
- **Componente:** `app/heart/recovery.py:validate_plan`.
- **Evidencia Observable:**
  - Verificado en arnés de estrés sintético con 16 casos límite:
    - Inyección de campos espurios (`cmd: rm -rf`) -> Rechazado con `ValueError`.
    - Acciones fuera de `ACTIONS` o metas fuera de `TARGETS` -> Rechazado con `ValueError`.
    - Planes con más de 5 pasos -> Rechazado con `ValueError`.
    - Inyección SQL en `task_id` -> Rechazado con `ValueError`.

---

### E-10: Normalización Determinista de Huellas de Incidentes (`cause_fingerprint`)
- **Componente:** `app/heart/recovery.py:classify_cause` y cálculo de fingerprint.
- **Evidencia Observable:**
  - Direcciones volátiles de memoria en punteros (`0x7f9a...` vs `0x7f9b...`) son saneadas mediante expresiones regulares antes de aplicar SHA-256, arrojando la misma huella determinista y previniendo tormentas de diagnósticos duplicados.

---

### E-11: Oráculo de Verificación Post-Reparación Riguroso y Antirregresión
- **Componente:** `app/heart/recovery.py:verify`.
- **Evidencia Observable:**
  - Pruebas sintéticas verificaron que `verify()` rechaza reintentos con número de intento obsoleto (`stale attempts`), reinicios de contenedor no confirmados (`started_at < restart_requested_at`) y persistencia del síntoma original.

---

### E-12: Aislamiento Seguro del Socket Docker en Producción
- **Componente:** Contenedor `khai_docker_proxy` en Azure.
- **Evidencia Observable:**
  - El socket Unix `/var/run/docker.sock` reside exclusivamente dentro de `khai_docker_proxy`.
  - La comunicación desde `khai_core` ocurre mediante peticiones HTTP filtradas en el puerto interno 8001 en la red Docker privada `control`.

---

### E-13: Estado Limpio del Almacén de Control Local (`ControlStore`)
- **Componente:** `data/control.sqlite3` y `data/coordinator.lock` en Windows.
- **Evidencia Observable:**
  - `data/control.sqlite3` cuenta con sus 5 migraciones estructurales aplicadas.
  - Tablas operativas limpias (0 tareas pendientes, 0 bloqueos residuales).
  - `coordinator.lock` completamente liberado.

---

### E-14: Unificación Arquitectónica de Brain y Retiro de Componentes Obsoletos
- **Componente:** `app/mind/brain/`.
- **Evidencia Observable:**
  - Cascada de modelos unificada en `app/mind/brain/controller.py`.
  - Directorio residual `/Dolphin/` completamente eliminado de la raíz del repositorio.
  - Preservación de alias retrocompatibles (`DolphinController = ModelController`).

---

### E-15: Arquitectura de Persistencia Inmutable de Planes (`plan_hash`)
- **Componente:** `app/heart/recovery.py`.
- **Evidencia Observable:**
  - Todo plan de reparación genera un hash canónico SHA-256 vinculando `case_id`, `steps`, `capabilities` y `target`. Cualquier discrepancia entre la propuesta autorizada y la ejecución genera un fallo de integridad.

---

## 5. Matriz de Recomendaciones y Plan de Acción Inmediato

| Prioridad | ID | Acción Recomendada | Componente / Archivo | Esfuerzo |
| :---: | :---: | :--- | :--- | :---: |
| 🔴 **Crítico** | C-01 | Purgar `secrets.env` y `GEMINI_API.txt` de la raíz del repositorio; mantener únicamente `data/voice/secrets.env`. | Raíz del repositorio | Inmediato |
| 🔴 **Crítico** | C-02 | Revisar timeouts y auto-arranque de VMs GPU en GCE Spot para evitar fallback forzoso a Gemini. | `dolphin_controller.py` / `gcloud-ollama.ps1` | Medio |
| 🔴 **Crítico** | C-03 | Capturar `ValueError` en `action_planner.py` durante `_advance_scoped` y derivar a diagnóstico ordenado. | `app/mind/coordinator.py:1791` | Bajo |
| 🔴 **Crítico** | C-04 | Implementar descarte o TTL para borradores huérfanos inactivos en Hand. | `app/hand/hand.py:410` | Bajo |
| 🟡 **Mejora** | M-01 | Crear `pytest.ini` en la raíz con `pythonpath = .` para habilitar `pytest` directo. | Raíz / `pytest.ini` | Inmediato |
| 🟡 **Mejora** | M-02 | Armonizar en `.agents/GEMINI.md` la política de escalación a Heart con la implementación real. | `.agents/GEMINI.md` | Bajo |
| 🟡 **Mejora** | M-03 | Agregar endpoint `@app.get('/health')` en FastAPI para sondas de monitoreo. | `app/voice/voice.py` | Bajo |
| 🟡 **Mejora** | M-04 | Eliminar directorio residual `MagicMock/` y agregarlo a `.gitignore` y `scripts/clean.py`. | Raíz / `coordinator.py` | Bajo |
| 🟡 **Mejora** | M-05 | Reemplazar `except:` desnudo por excepciones específicas en pasarela Telegram. | `telegram_gateway.py:173` | Bajo |
| 🟡 **Mejora** | M-06 | Agregar logging en bloques `try/except` de Dreams. | `app/dreams/dreams.py` | Medio |
| 🟡 **Mejora** | M-07 | Añadir `pytest` a `requirements.txt` o crear `requirements-dev.txt`. | `requirements.txt` | Inmediato |

---

## 6. Procedimiento de Verificación Independiente de Hallazgos

Cualquier auditor o desarrollador puede corroborar empíricamente todos los hallazgos citados ejecutando los siguientes comandos en PowerShell desde la raíz de `d:\OneDrive\GitHub\dev\Khai`:

```powershell
# 1. Verificar credenciales sensibles duplicadas en raíz (C-01)
Get-Item -Path "secrets.env", "GEMINI_API.txt", "data/voice/secrets.env"

# 2. Verificar inactividad del servidor y puertos locales (C-05)
.venv\Scripts\python.exe -c "import socket; ports = [8000, 8443, 8001, 8080]; print('Puertos locales (10061=Rechazado):', {p: socket.socket().connect_ex(('127.0.0.1', p)) for p in ports})"

# 3. Reproducir falla de recolección de pytest directo (M-01)
.venv\Scripts\pytest.exe -v

# 4. Verificar ejecución exitosa de la suite canónica completa (E-06)
.venv\Scripts\python.exe -m pytest -v

# 5. Verificar suite de Heart con unittest (E-07)
.venv\Scripts\python.exe -m unittest tests.test_heart -v

# 6. Verificar inmutabilidad de triggers SQLite en Heart (E-08)
.venv\Scripts\python.exe -c "import sys, tempfile, sqlite3; sys.path.insert(0, '.'); from pathlib import Path; from app.heart.recovery import Recovery; from unittest.mock import Mock; tmp = tempfile.TemporaryDirectory(); db = Path(tmp.name)/'test.sqlite3'; rec = Recovery(db, Mock(), Mock(), Mock()); rec._move('case-test', 'COLLECTING_EVIDENCE', expected=None); conn = sqlite3.connect(db); conn.execute('DELETE FROM recovery_transitions')"

# 7. Verificar estado de producción en Azure (requiere SSH autorizado) (C-02, E-04, E-05)
ssh -n -o BatchMode=yes -o ConnectTimeout=5 carlos@20.237.211.250 "uptime && docker ps -a"
```
