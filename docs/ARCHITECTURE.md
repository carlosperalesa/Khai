# Manual Técnico y Arquitectura de Khai

Aceptación de continuidad con `dd00b6d`: un chat real se resolvió mediante Nano mientras low cargaba; cancelación real en ~146 ms sin activar el intento anulado. Mid cargó en 35,792 s; high agotó 90,128 s en tensores. No se cambió el plazo ni se habilitó Monitoring. Medición interna por SSH pendiente de identidad verificada; serial/API conservados en el servidor.

La búsqueda conversacional descubrió un error del Core: resolver un binding opcional `input.count` ausente. La resolución ahora omite solo entradas opcionales ausentes; nunca oculta resultados previos ni valores requeridos ausentes. Los errores genéricos pasan a DIAGNOSING con etapa/tipo/despacho; resultados inciertos conservan execution_id y no se reejecutan. Un fallo de Heart/Pixy durante DIAGNOSING se registra sin abrir otro bucle. El diagnóstico acotado distingue evidencia tipada de una hipótesis histórica y no propone recrear la skill ni reparar red por ese binding. Son correcciones en validación; aceptación final en CHECKPOINT.

Continuidad de modelos implementada localmente y en validación: [`MODEL_TRANSITIONS.md`](./MODEL_TRANSITIONS.md). La composición de producción conecta `TransitionManager` con Brain y Voice: genera solicitudes durables, cancela mediante generación y publica el modo efectivo en el SQLite de control. La inferencia no arranca VMs ni cambia perfiles automáticamente; confirma Nano antes de publicar un fallback. Los métodos legacy de mutación rechazan llamadas fuera del propietario de transición. Mind entrega esquema nativo a Ollama y separa mensaje actual del historial. Aceptación y limitaciones operativas en CHECKPOINT.

Suite consolidada de esta continuaciÃ³n: 389 pruebas y 39 subpruebas aprobadas. Incluye la verificaciÃ³n de bindings opcionales en recetas, el fallback reactivo a Nano con aviso estructurado y botonera interactiva, la homogeneizaciÃ³n de thought_signature para compatibilidad universal de herramientas, el enmascaramiento y saneamiento de mensajes internos hacia el usuario (ocultando UUIDs de tareas/transiciones, taxonomÃ­as internas y suprimiendo auditorÃ­as redundantes en chat), la precisiÃ³n diagnÃ³stica y normalizaciÃ³n de nulos en descriptores de servicios de Hand/Voice (especificaciÃ³n de campo, defaults canÃ³nicos de lectura y aislamiento de credenciales), la aceleraciÃ³n de I/O de disco (readahead 8192 KB) y la residencia de sesiÃ³n en GPU (keep_alive 30m). Las pruebas reales se documentan por sus fuentes y recibos.

El diario común fue aprobado por el propietario. Implementación y contrato en [`COMMON_JOURNAL.md`](./COMMON_JOURNAL.md): ciclos con raíz/padre, eventos inmutables, enlaces y consulta por ID en el SQLite de control. La auditoría anterior describe el estado previo; sus propuestas pendientes se resuelven según esta implementación. La aceptación conjunta y en servidor se registra en CHECKPOINT.

Validación del diario común: 329 pruebas y 30 subpruebas aprobadas; comprobación adicional del descarte de evidencia ajena aprobada. Los límites de transacciones entre bases y la reconciliación sin replay están descritos en el contrato.

Producción: código `385f20d` en Core/proxy; migración y salud HTTPS/SQLite verificadas. Trece pruebas controladas sobre la imagen desplegada y dos interacciones reales de ingreso/informe/entrega aprobaron. Los fixtures no acreditan la reparación del contrato de low ni del arranque GPU; esas tareas siguen independientes del diario.

Este documento consolida la arquitectura del sistema, contratos, almacenamiento, seguridad, decisiones históricas (ADRs), procedimientos de operaciones, la infraestructura híbrida acelerada (Brain) y la arquitectura canónica de pruebas.

Auditoría histórica de siete caminos: [`INTERACTION_AUDIT.md`](./INTERACTION_AUDIT.md). En ese estado previo, los IDs de tareas, casos, autorizaciones, herramientas y entregas no acreditaban un árbol completo padre/hijo. Se detectaron pérdida por colisión de IDs externos en AgentLoop, cierres ausentes o prematuros y contaminación de diagnóstico por evidencia histórica ajena. Esos hallazgos motivaron la implementación aprobada del diario común; su contrato actual figura arriba.

La auditoría añadió 16 pruebas dentro de las suites canónicas: validación conjunta final 303 pruebas y 25 subpruebas aprobadas (191,73 s). Este resultado no acredita reparación de los huecos enumerados; la auditoría solo cambia documentación y pruebas.

Actualización de implementación del 31 de octubre (aceptación en curso): `busca <consulta>` usa el SkillSpec de `web-search`; el proveedor se resuelve por una preferencia aprobada y auditable, nunca por instalación implícita.

Voice mantiene un diario append-only `model_rejections` para rechazos del contrato de Mind. Conserva respuesta de hasta 16.000 caracteres (truncamiento explícito y SHA-256 del contenido completo), esquema esperado, errores Pydantic sin inputs/contextos, modelo y duraciones disponibles, task_id, revisión y traza. Redacta valores de secretos conocidos antes de persistir o devolver referencias; no incluye prompts ni credenciales. El contenido del modelo se marca como no confiable. El diario reside en Voice, no se monta en workers y sobrevive al reinicio.

Ante un rechazo, Mind entrega una referencia del diario; Coordinator registra `routing_failure`, pasa a DIAGNOSING y abre Heart en el primer fallo. Senses entrega el registro que coincide con referencia y task_id, no un error reciente de otra tarea. Heart diagnostica contra ese JSON y esquema; Pixy custodia el plan y sus autorizaciones, y Dreams conserva un episodio con run_id ausente porque todavía no hubo worker. Una negativa terminal de contenido se registra sin reintentar para eludirla. Los incidentes anteriores al despacho se identifican por intento y revisión. El lector de archivos reconoce trazas tanto con guion como con guion bajo.

DIAGNOSING se agenda en el mismo executor con propiedad por task_id que las llamadas de generación. Si Heart tarda, el ciclo principal no inicia otro diagnóstico ni transforma la tarea en fallo por observar un caso aún incompleto. La mensajería y otras tareas pueden continuar.

Aceptación de producción de la instrumentación: código `19ce591` activo. La repetición de la orden de preferencia con low produjo un contrato válido en 164,33 s y guardó la preferencia auditable. Esto acredita ese turno, no explica el rechazo histórico ni demuestra que todos los errores puedan repararse automáticamente. La integración Voice→Mind→Senses→Heart→Pixy→Dreams ante un JSON inválido está cubierta por una prueba controlada; no se presenta como un rechazo nuevo del modelo de producción.

La aceptación del parser recorrió fallo real de TLS en PyPI, diagnóstico de Heart, decisión de Pixy, reanudación del artefacto aprobado y verificación independiente de CSV/PDF/XLSX. El caso terminó RECOVERED por postcondiciones y Dreams registró las tres ejecuciones. La aceptación conversacional de búsqueda detectó además que low puede conectar y aun devolver un JSON inválido para Mind; la conexión de un modelo no acredita su compatibilidad con el router.

Los errores de preparación de dependencias se distinguen del transporte al proxy: `DependencyPreparationError` certifica que Voice falló antes del despacho. Coordinator registra etapa y `dispatched=false`, abre diagnóstico de Heart y evita el reintento de transporte. Para fallos transitorios, la acción cerrada `resume_prepared_task` requiere autorización exacta de Pixy y conserva artefacto, versión, parámetros y aprobación. Hand crea un intento nuevo que vuelve a READY; se verifican de nuevo los hashes y permisos antes de ejecutar. No admite efectos inciertos, artefactos sin autorización ni planes combinados. El propietario eligió esta recuperación en lugar de regenerar toda la skill.

El disco de modelos ya fue copiado a `pd-balanced` en `us-west1-b` con autorización del propietario. Se comprobó G2, conexión y carga CUDA; la primera activación high volvió a agotar la precarga de 90 s, por lo que el cambio de disco no acredita resolución del fallo. Se conservan el volumen standard original en `us-west1-a` y la instantánea durante la medición. No se amplió el plazo; se solicitó medir almacenamiento y GPU primero.

Cloud Monitoring API permanece deshabilitada por decisión explícita del propietario. El diagnóstico se limita a los registros existentes; los tiempos de carga observados no acreditan IOPS ni throughput puro del disco.

El contrato HTTP del proxy declara `dependency_lock` y `goal_revision`: no debe descartarlos al serializar la petición validada. Para compatibilidad, omite lock ausente y revisión0 al calcular el payload de recibos anteriores. Una revisión de recuperación positiva llega al worker y a Voice, evitando solicitudes asociadas a una revisión obsoleta. Prueba de regresión a través de ASGI/HTTP, además de la prueba de Engine.

Ollama 0.40.12 observado en producción fija llama.cpp b11901. En ese código `offloaded X/X layers` precede a `load_all_data`; no usar el mensaje como prueba de pesos ya transferidos. Las hipótesis de lectura de disco, transferencia e inicialización CUDA requieren evidencia adicional. Low sí completó precarga en 30,81 s; la cascada completa duró7m44s.

Voice posee `WheelCache` (`app/voice/runtime/dependencies.py`). Después de la autorización de la tarea, prepara wheels de PyPI con raíces `nombre==versión`, resolución transitiva para CPython 3.10/Linux x86_64 y verificación de SHA-256 contra los metadatos oficiales de cada release no retirada. No admite fuentes, URLs de paquetes ni otro índice como respaldo. El lock incluye raíces, plataforma, nombres, versiones y hashes; el proxy verifica todo el contenido de la caché antes de construir una imagen sin red. Pip instala desde el contexto verificado mediante `--no-index --require-hashes --only-binary=:all:`. Se reutiliza la imagen por lock y su venv en `/opt/venv`; el `/tmp` del worker permanece `noexec`. La base `python:3.10-slim` debe estar aprovisionada durante el despliegue. No se exportan secretos al proceso de descarga.

El parser `khai_documents` se monta de solo lectura dentro del worker. Admite PDF (`pypdf==6.21.4`), CSV UTF-8 y XLSX (`openpyxl==3.1.5`, `defusedxml==0.7.1`). Solo lee rutas relativas contenidas en `KHAI_SKILL_DATA`, sin enlaces. Limita el archivo a 8 MiB, la expansión XLSX a 32 MiB, 30 páginas PDF, 10 hojas, 500 filas y 50 columnas por hoja, y la salida a 16.000 caracteres. No calcula fórmulas, carga enlaces externos ni ejecuta macros. Un PDF sin texto devuelve `needs_ocr`; no incorpora OCR ni un flujo de adjuntos Telegram. La skill candidata `document-parser` requiere auditoría y ejecución verificable antes de activarse.

Brain conserva hasta 500 eventos de activación en `model_transition_events`, con intento, traza, perfil, disco, etapa, duración y error tipado. Senses expone los 40 recientes a Heart cuando la consulta trata de modelos. Heart limita el diagnóstico al perfil pedido y a fallos recientes; distingue la etapa observada de la causa todavía desconocida y no propone una reparación de otra tarea. Esto no implementa aún transiciones asíncronas cancelables.

Operación de disco: cambiar el tipo exige instantánea y disco nuevo, no una edición del volumen original. El 31 de octubre se autorizó `pd-balanced` 100 GiB; la instantánea quedó READY pero `us-west1-a` rechazó el nuevo disco por falta de capacidad. La evaluación de otras zonas de la misma región conserva la subred y exige verificar WireGuard, GPU y generación real antes de cambiar el modo activo. Conservar disco anterior y respaldo durante la aceptación; no eliminar recursos por falta de capacidad.

---

## 1. Visión General y Flujo de Órganos

Khai opera mediante órganos especializados con responsabilidades y límites estrictos:

```text
                  +-------------------------------------------------+
                  |               Usuario (Telegram)                |
                  +-----------------------+-------------------------+
                                          |
                                          v
                                +-------------------+
                                |       Voice       | (Red externa, secretos, webhooks)
                                +---------+---------+
                                          |
                                          v
                                +-------------------+
                                |   Mind / Brain    | (Intención, capacidad, razonamiento,
                                +---------+---------+  cascada de inferencia y modos VM)
                                          |
                     +--------------------+--------------------+
                     |                                         |
                     v                                         v
           +-------------------+                     +-------------------+
           |       Pixy        | (Gobernanza)        |       Hand        | (Creación y ejecución)
           +---------+---------+                     +---------+---------+
                     |                                         |
                     |         +-------------------+           |
                     +-------->|       Heart       |<----------+
                               |   (Diagnóstico)   |
                               +---------+---------+
                                         |
                                         v
                               +-------------------+
                               |      Dreams       | (Aprendizaje y notas de episodios)
                               +-------------------+
```

### Roles y Responsabilidades:
- **Mind / Brain:** Mind interpreta el lenguaje natural extrayendo la intención (`Intent`), la capacidad requerida y el proveedor opcional. Incorpora **Brain** (`app/mind/brain/`), que actúa como la fachada unificada de razonamiento, selección de modelos, controlador de VM Spot (`ModelController`) y orquestador de la cascada de inferencia (`high > mid > low > nano`).
- **Hand:** Ciclo activo de herramientas persistente; investiga, genera código, valida esquemas y ejecuta órdenes en contenedores seguros.
- **Pixy:** Custodia de gobernanza, autoridad, secretos y contratos; solicita confirmación binaria (`y`/`n`) y emite `DecisionReceipt`.
- **Heart:** Diagnóstico objetivo de fallos reiterados apoyado en telemetría real y lectura de logs del contenedor vía `CoreEyes`. Las consultas sobre transiciones de modelo no se vinculan a una tarea arbitraria; sin telemetría correlacionada se informa evidencia insuficiente sin generar un plan de reparación ajeno.

La recuperación de tareas distingue postcondiciones verificadas, entrega de una respuesta conversacional confirmada y evidencia insuficiente. La entrega no verifica la calidad de la respuesta. Las plantillas de Hand son propuestas no aprobadas y el lector de Voice acepta solo URLs explícitas del propietario, con límites y contenido no confiable. El diseño pendiente de continuidad con nano está en `docs/MODEL_TRANSITIONS.md`.

La skill `skills/web-search` llama a `brave_search.search` mediante Voice y devuelve resultados estructurados. Incluye SkillSpec, código sin dependencias y descriptor para Brave Search API. Voice resuelve `BRAVE_SEARCH_API_KEY` y la envía mediante `X-Subscription-Token`; el worker no recibe el secreto. Fue activada en producción el 31 de octubre tras revisión de Pixy y una ejecución aislada con recibo real; las actualizaciones futuras requieren repetir revisión, autorización y verificación.

La investigación de infraestructura de Hand usa `config/research.yaml` mediante `ResearchSearch` y el transporte existente de `ExternalGateway`: DNS público fijado, TLS, límites de tiempo, tamaño y frecuencia. El proveedor se declara en configuración; no se sustituyen proveedores al fallar. No modifica la inferencia de nano ni las preferencias de búsqueda del usuario. El candidato anterior de Google y su plantilla de descriptor fueron retirados del código local; los registros históricos de producción permanecen pendientes de migración.
- **Voice:** Concentrador ?nico de conectividad externa con verificaci?n TLS estricta, DNS pineado, resoluci?n multihoming con priorizaci?n IPv4 y tolerancia a fallos de ruta IPv6, y aislamiento absoluto de secretos (`secrets.env`).
- **Dreams:** Persiste notas de episodios (`episode_notes`), consolida experiencias y formula recomendaciones de evolución de catálogo.

### 1.1. Capa de Humanización y Experiencia de Usuario: Mind como Wrapper
Mind opera como la capa cognitiva y el envoltorio humano (*Human Interface Wrapper*) bidireccional entre la maquinaria técnica determinista de Khai (Heart, Hand, Workers y Skills) y el usuario:

```text
[Usuario] <---(Telegram / Voice)---> [ MIND (Coordinator) ]
                                       | Inbound: Resuelve contexto y perfil del usuario
                                       v Outbound: Traduce salidas tecnicas a lenguaje humano
                         +-------------+-------------+
                         |                           |
                         v                           v
                  [ HAND / WORKER ]           [ HEART / PIXY ]
                  (Ejecuta código,            (Diagnósticos, auditorías,
                   emite JSON y outputs)       permisos y revisiones)
```

1. **Inbound Wrapper (Contexto y Base de Conocimiento del Usuario):**
   - Controla las preferencias y el perfil persistente del usuario (`user_profile` en `MemoryEngine`), incluyendo idioma (`es`), ubicación base (`Santiago, Chile`), sistema métrico (`units: metric`) y zona horaria.
   - En el enrutamiento y planificación de recetas (`ActionPlanner`), infiere y enlaza automáticamente entidades y parámetros requeridos presentes en el mensaje del usuario o en su perfil de preferencias, suprimiendo turnos y preguntas redundantes de alcance.
   - Si un parámetro obligatorio falta y no existe en el perfil, formula preguntas en español natural y empático (ej. *"¿Podrías indicarme la ciudad para continuar?"*) erradicando consultas técnicas de variables como `¿Qué valores corresponden a city?`.

2. **Outbound Wrapper (Traducción a Humano y Síntesis Contextual):**
   - Intercepta y normaliza toda salida previa a su notificación en Voice (`Coordinator.notify()`):
     - Aplica el principio de mínimo hardcoding mediante prompts libres al LLM: toma el objetivo o pregunta del usuario (`task['request']`) y el resultado técnico crudo de la herramienta para generar una respuesta conversacional, fluida y adaptada al objetivo formulado (ej. si el usuario pidió un resumen de noticias, redacta un resumen fluido; si pidió un dato puntual, lo entrega con precisión).
     - Desempaqueta y limpia objetos JSON estructurados devueltos por workers y skills (claves estándar como `weather_info`, `result`, `message`), suprimiendo sintaxis `{}` y comillas crudas en el fallback determinista.
     - Suprime prefijos mecánicos de pipelines y recetas (`Paso X:`, `\w+ info:`, `Resultado:`).
     - Traduce términos residuales de APIs internacionales (ej. descripciones meteorológicas en inglés) a español claro.
   - Convierte los diagnósticos y avisos de recuperación de Heart en explicaciones comprensibles, evitando la exposición de números de caso, firmas o trazas internas al usuario.

3. **Directrices de Localización para Hand:**
   - La generación de código en `Hand.generate` instruye obligatoriamente a parametrizar servicios externos con localización métrica (`units='metric'` para °C y km/h) e idioma español (`lang='es'`), redactando resultados conversacionales completos y prohibiendo emojis o prefijos de depuración interna.

4. **Herramienta `set_user_preference` y Sincronización Inline:**
   - Permite registrar y recordar atributos del usuario (`sign`, `name`, `city`, `units`, `language`) de forma determinista y auditable.
   - `Mind.process_message` extrae entidades declaradas en lenguaje natural (`yo soy cáncer, recuérdalo`) y las persiste de inmediato en `MemoryEngine.set_user_profile` y `MemoryEngine.set_preference`, previniendo la pérdida de datos de perfil en solicitudes compuestas.

5. **Desburocratización y Omisión de Confirmación en Consultas de Lectura:**
   - En el estado `READY`, las tareas cuyos servicios externos sean de solo lectura (`effect == 'read'`) y posean inputs explícitos sin valores sustitutos omiten la confirmación intermedia `WAITING_INPUT_CONFIRMATION` y avanzan a ejecución directa (`SUBMITTED`), eliminando la fricción de confirmación en búsquedas o consultas informativas. Las tareas con escrituras (`effect == 'write'`) conservan la confirmación obligatoria.

### 1.2. Gobernanza Administrativa: Separación entre System, Root y Userland
Khai distingue tres niveles jerárquicos de autoridad para preservar la seguridad constitucional:
- **Nivel System (Inmutable en runtime):** Kernel del sistema operativo, daemon de Docker, red aislada, proxy de ejecución y librerías base del Core. No modificables desde el chat bajo ninguna circunstancia.
- **Nivel Root (Gobernado por Pixy con aprobación humana explícita `y`/`n`):** Identidad y personalidad en `data/memory/agent.md`, directivas dinámicas de comportamiento en `dynamic_guidelines` y perfiles de usuario. Cualquier modificación requiere una propuesta formal de Pixy y confirmación binaria explícita del usuario (`y`/`n`).
- **Nivel Userland (Skills en Sandbox):** Código de skills en `data/skills/`, generado por Hand, diagnosticado por Heart y ejecutado en contenedores efímeros sin red.

### 1.3. Erradicación de Amnesia Conversacional y Manejo Flexible de Decisiones
- **Continuidad del Hilo de Alcance (`WAITING_SCOPE`):**
  - Cuando una tarea solicita parámetros adicionales (ej. ciudad, fechas o formato), la pregunta abierta queda registrada en `conversation_questions` (`kind='scope'`).
  - `Coordinator.handle_reply` reconoce respuestas naturales no binarias (ej. "Santiago", "en Valparaíso", "celsius") y las enlaza de inmediato con la tarea pendiente mediante `accept_scope_reply`. La tarea avanza a `RESOLVING_SCOPE`, donde el LLM (`requirements.resolve`) integra los datos y reanuda la ejecución sin congelar el objetivo ni requerir que el usuario repita su pregunta inicial.
- **Aprobaciones en Lenguaje Natural y Respuestas Mixtas:**
  - Se flexibiliza el reconocimiento de intenciones de aprobación (`dale`, `ok`, `acepto`, `claro`, `procede`) y cancelación (`cancela`, `rechazo`, `abortar`, `olvídalo`).
  - Ante respuestas mixtas o clarificaciones durante la aprobación (ej. "sí, y además dame el viento", "¿sabes? ahora prefiero el clima de Valparaíso"), `Coordinator.answer_question` no emite bloqueos mecánicos de "responde sí o no"; ejecuta `revise_goal` adaptando el alcance y actualizando los requerimientos en caliente.

### 1.4. Protocolo de Evolución de Skills (Hand + ActionPlanner + Catalog)
- **Habilidades Evolutivas vs. Código Congelado:**
  - Las habilidades en Khai no son estáticas. Si un usuario formula un requerimiento adicional sobre un tema ya cubierto (ej. agregar viento o lluvia a una skill de temperatura existente), o si el usuario corrige una unidad métrica previa:
  - `ActionPlanner` examina la especificación activa en el catálogo. Al detectar que la habilidad existente carece de las propiedades o datos solicitados, propone en `new_skill` la evolución de la habilidad (`is_evolution=True`, enlazada a `previous_version_id`).
  - `Hand.create` detecta la versión previa activa y crea la versión siguiente (`v2`, `v3`) con tipo de operación `update`.
  - La nueva versión se valida y prueba en contenedor Docker seguro sin red (`--network none`). Tras la verificación de Pixy, `SkillCatalog.promote` transiciona la versión anterior a `superseded` y promueve la nueva versión a `active`.

### 1.5. Puente Mind -> Heart: Autopsias Cognitivas e Incidentes Semánticos
- **Detección Determinista y Herramienta `report_issue`:**
  - Cuando el usuario formula quejas o correcciones cualitativas sobre un resultado previo (ej. *"No me vuelvas a dar grados Kelvin"*, *"Te equivocaste con los datos"*), Mind ejecuta la herramienta `report_issue(issue, task_id, skill_name)` o activa el detector semántico determinista.
  - `Coordinator.report_cognitive_issue` abre de inmediato un caso de incidente en Heart (`Recovery.open_incident_case`) bajo el código tipado `cognitive_discrepancy`.
- **Diagnóstico y Remediación en Heart:**
  - Heart no limita su diagnóstico a tracebacks o fallos de infraestructura (`returncode != 0`). Clasifica la discrepancia semántica y emite una orden de remediación (`evolve_skill:<skill_id>`) hacia Hand para re-forjar la capacidad con las directivas métricas y de formato corregidas.
- **Persistencia en Dreams:**
  - Cada incidente cognitivo genera un registro negativo en el almacén de episodios de `Dreams`, permitiendo que el sistema consolide reglas de aprendizaje y evite tropezar dos veces con el mismo patrón erróneo.

### Ciclo de Vida de una Solicitud:
1. **Interpretación (Mind):** Convierte el mensaje en un `Intent` (objetivo, capacidad requerida y proveedor opcional).
2. **Filtrado de Catálogo:** Busca skills activas que satisfagan la capacidad. Si no hay proveedor explícito, usa una preferencia aprobada persistente; si no existe, consulta al usuario.
3. **Creación o Reparación (Hand):** Si la capacidad no existe o falló, abre un `agent_run` durable con herramientas persistentes (LLM, análisis de código, tests locales).
4. **Gobernanza y Autorización (Pixy):** Revisa descriptores de servicio y artefactos. Si se requieren permisos externos, secretos o costos, solicita confirmación binaria (`y`/`n`) al usuario y emite un `DecisionReceipt`.
5. **Ejecución Segura (Voice + Worker):** Voice procesa llamadas externas mediante servicios declarativos. El worker ejecuta la skill en un contenedor Docker con red directa deshabilitada (`--network none`).
6. **Verificación y Aprendizaje (Dreams):** Evalúa postcondiciones observables. Si el resultado es exitoso, promueve la skill en el catálogo y registra notas en `episode_notes`. Ante el primer fallo de tarea, transfiere el diagnóstico a Heart con deduplicación por huella de incidente.

---

## 2. Contratos y Estructura de Skills

### Contratos Fundamentales:
- **`Intent`:** Contiene `goal`, `capability` y `provider` (opcional).
- **`CapabilitySpec`:** Declara la capacidad técnica, proveedor, alias, operaciones permitidas, requerimientos y estado de ciclo de vida.
- **`DecisionReceipt`:** Registra la decisión del usuario, alcance autorizado, firma/hash, vigencia y procedencia.
- **`SkillSpec`:** Vincula esquemas, entorno de ejecución, dependencias, servicios autorizados y verificador de postcondición.

### Anatomía de una Skill (`data/skills/<nombre>/`):
- `SKILL.md`: Documento canónico de instrucciones con frontmatter YAML (`capability`, `provider`, `aliases`).
- `app/`: Código ejecutable (`main.py`) que implementa la lógica determinista.
- `data/`: Almacén de datos local y aislado de la skill.
- `other/`: Archivos auxiliares, plantillas y recursos estáticos.

---

## 3. Almacenamiento Central y Puertos de Control

Todo el estado transaccional del Core se consolida en una base de datos SQLite común: **`data/control.sqlite3`**.

### Puertos Acotados y Propietarios Lógicos:
Cada órgano accede a través de un puerto formal con operaciones de escritura restringidas:

| Puerto | Escrituras Permitidas | Propietario Lógico |
| --- | --- | --- |
| **Coordinator** | Objetivos, intentos, transiciones de estado, presupuestos, comandos de usuario | Coordinación global |
| **Pixy** | Solicitudes de aprobación, decisiones tomadas, autorizaciones emitidas | Gobernanza y contratos |
| **Hand** | Artefactos, versiones de skills, órdenes de trabajo, recibos de ejecución | Construcción y ejecución |
| **Heart** | Casos de recuperación, evidencias referenciadas, diagnósticos, planes correctivos | Salud y diagnóstico |

### Principios del Almacén de Control:
- **Sin bearer tokens:** Los identificadores (`authorization_id`, `order_id`) son referencias contextuales; requieren coincidencia exacta de operación, caso, hash y vigencia para ser consumidos.
- **Transacciones atómicas:** La aprobación en Pixy y la habilitación de la orden en Hand ocurren en una transacción atómica. Hand consume la orden de forma idempotente.
- **Archivos excluidos de `control.sqlite3`:**
  - `data/voice/secrets.env`: Administrado exclusivamente por Voice.
  - `data/voice/dolphin_usage.sqlite3`: Sesiones y estado de modo de inferencia local administrado por Brain (`ModelController`).
  - `data/mind/identity.yaml`: Identidad estática del agente.
  - Recibos externos de gateways de red.

---

## 4. Aislamiento de Red y Modelo de Seguridad

- **Workers Desconectados:** Los contenedores de ejecución de skills carecen de acceso a interfaces de red externas (`--network none`).
- **Pasarela Centralizada (Voice):** Cualquier salida HTTP pasa por Voice mediante descriptores de servicio validados que definen dominios, rutas, métodos y esquemas de parámetros autorizados.
- **Aislamiento de Secretos:** Las claves de API y credenciales residen en el host y se inyectan en tiempo de ejecución en Voice. Nunca se graban en imágenes de Docker, historiales de chat, prompts ni descriptores de skills.

---

## 5. Decisiones Arquitectónicas Canónicas (ADRs)

### ADR-0001: Separar Aprendizaje, Reparación y Evolución
- **Decisión:** Tratar aprendizaje, reparación y evolución como finalidades distintas con criterios de aceptación independientes.
- **Consecuencias:** `COMPLETED`, `RECOVERED` y entrega confirmada representan hechos diferentes. Una reparación genera evidencia pero no demuestra aprendizaje hasta su reutilización exitosa. Dreams propone; no activa reglas ni despliega cambios sin supervisión.

### ADR-0002: Mantener Workers sin Red Directa
- **Decisión:** Mantener deshabilitada la red directa en los workers. Las operaciones externas pasan exclusivamente por Voice mediante servicios declarativos y recibos idempotentes.
- **Consecuencias:** Hand propone descriptores, Pixy autoriza su alcance y Voice registra el transporte. Los secretos permanecen aislados en Voice.

### ADR-0003: Control Store Común en `data/control.sqlite3`
- **Decisión:** Consolidar el estado operacional del Core en `data/control.sqlite3` manteniendo propietarios lógicos por repositorio.
- **Consecuencias:** Esquema estructurado y transaccional único. Migraciones versionadas por componente para prevenir colisiones.

### ADR-0004: Puertos Acotados, Repositorios Lógicos y Recibos
- **Decisión:** Cada órgano interactúa únicamente mediante un puerto formal inyectado en bootstrap con validación estricta de procedencia.
- **Consecuencias:** Desacoplamiento estricto. Los órganos no comparten conexiones SQLite directas entre sí. `trace_id` se reserva para correlación y no confiere autoridad.

### ADR-0005: Capacidades, Proveedores y Transparencia
- **Decisión:** Representar solicitudes bajo la tríada (capacidad, proveedor opcional, objetivo). Proveedores registrados en metadatos y resueltos por orden explícito, preferencia aprobada o consulta.
- **Consecuencias:** Agregar un proveedor nuevo no altera Mind ni Coordinator. Prohibidas las sustituciones silenciosas de servicios.

---

## 6. Operaciones, Despliegue y Ciclo de Vida

### Entorno de Producción:
- **Servidor:** Azure VM (`carlos@20.237.211.250`, Ubuntu 24.04 aarch64 en `/opt/khai`).
- **Contenedores:**
  - `khai_core` (aplicación principal y webhook HTTPS en `:8443`).
  - `khai_docker_proxy` (controlador seguro del socket de Docker en `:8001`).

### Procedimiento de Despliegue:
La transferencia de secretos actualiza exclusivamente la clave necesaria en `data/voice/secrets.env`, conservando las demás credenciales y sin incluir valores en comandos, Git, logs o imágenes. La imagen excluye `data/` y archivos `.env`. Tras reconstruir el Core, verificar `/health`, SQLite y una operación real del servicio configurado. Una skill candidata solo entra al catálogo después de revisión de Pixy, ejecución aislada y evidencia del resultado.

1. Conectar por SSH a `carlos@20.237.211.250`.
2. Actualizar código fuente: `cd /opt/khai && git pull origin main`.
3. Reconstruir y reiniciar contenedores: `sudo docker compose up -d --build bot`.
4. Verificar logs de inicio: `sudo docker logs --tail 30 khai_core`.
5. Comprobar integridad del sistema: `make stats` o consulta a `m.heart.check_integrity()`.

### Procedimiento de Purga y Mantenimiento:
- **Limpieza rutinaria:** Ejecutar `make clean` (elimina archivos temporales, logs antiguos y caché preservando `.venv`).
- **Purga de estado:** Se realiza resolviendo rutas absolutas para eliminar exclusivamente SQLite de runtime, borradores y workspaces generados, preservando siempre certificados, identidad y secretos (`secrets.env`).
- **Rollback:** Si un despliegue presenta problemas de arranque, se detiene el contenedor, se hace checkout al commit sano previo en Git, se reconstruye el contenedor y se documenta el incidente con `/p corrige el ultimo error`.

---

## 7. Infraestructura Acelerada de Inferencia y Razonamiento (Brain)

Khai cuenta con una arquitectura híbrida para inferencia local privada de alta velocidad y razonamiento avanzado sin depender exclusivamente de APIs comerciales externas.

### Unificación en Brain (`app/mind/brain/`):
- `Brain` consolida la fachada de razonamiento, selección de modelos, controlador de infraestructura (`ModelController`) y soporte de scripting en un único subsistema cohesivo.
- Se eliminó el directorio raíz obsoleto `/Dolphin/`, consolidando sus herramientas de aprovisionamiento en `app/mind/brain/gcloud-ollama.ps1`.
- Para preservar compatibilidad con código existente, `DolphinController` permanece definido como un alias directo de `ModelController`.

### Topología de Red e Infraestructura:
```text
  Telegram Webhook (:8443) ---> Azure VM (vm-carlosperales, West US)
                                   |
                  Túnel WireGuard privado (UDP 51820)
                  10.200.0.1 (Azure) <-> 10.200.0.2 (GCP) (RTT ~22 ms)
                                   |
                                   v
             Nodo Efímero GCE Spot (us-west1-a, Oregón)
             Disco Persistente Único: dolphin-boot-disk (100 GB pd-standard, auto-delete=no)
             Ubuntu 24.04 LTS, Ollama (:11434), Drivers NVIDIA L4 (580-server-open-gcp)
```

### Perfiles de Inferencia:
1. **Modo High (`high`):** GCE Spot `g2-standard-8` (8 vCPU, 32 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `qwen2.5-32b-abliterated` (19 GB, 32B parámetros, ~$550 CLP/hora). Perfil prioritario para razonamiento técnico y generación estructurada.
2. **Modo Mid (`mid`):** GCE Spot `g2-standard-4` (4 vCPU, 16 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `dolphin3:8b` (~$418 CLP/hora). Respuestas rápidas aceleradas por GPU.
3. **Modo Low (`low`):** GCE Spot `e2-standard-8` (8 vCPU, 32 GB RAM, CPU). Modelo: `dolphin3:8b` (~$152 CLP/hora). Inferencia privada en CPU de respaldo.
4. **Modo Nano (`nano`):** Gemini Cloud API ($0 cómputo GCE). La VM Spot se apaga/elimina conservando el disco persistente en reposo. Modo de coordinación general y fallback.

### Cascada y Resiliencia:
- Cascada automática en orden estricto: `high > mid > low > nano`.
- Mensajes limpios y concisos durante transiciones:
  - Al iniciar conexión o cambio de modo: `[Conectando con Modo <nombre>]`
  - Al completar la conexión exitosamente: `[Modo <nombre> conectado]`
  - Al fallar un perfil: `[Modo <nombre> falló por: <motivo>]` (ej. `spot no disponible`) continuando inmediatamente con el siguiente perfil.
  - Al agotar la escala local y entrar a Gemini: `[Modo nano conectado]`
- Inactividad controlada: temporizador que apaga la VM tras 10 minutos sin tráfico para minimizar costos.
- Control de presupuestos en GCP con alarmas escalonadas cada $1.000 CLP.

---

## 8. Arquitectura Canónica de Pruebas y Certificación

Toda modificación al código fuente puede ser certificada localmente antes de cualquier despliegue por peticion del usuario mediante las **8 suites canónicas** (`tests/test_*.py`):

| Suite de Prueba | Órgano / Componente | Foco de Validación |
| --- | --- | --- |
| **`test_brain.py`** | Mind / Brain | Enrutamiento LLM, cascada `high>mid>low>nano`, ModelController, ciclo de VM Spot, banners y timeouts. |
| **`test_dreams.py`** | Dreams | Aprendizaje experiencial, registro de `episode_notes` y políticas de promoción de skills. |
| **`test_hand.py`** | Hand | Ciclo de construcción de código, orden de trabajo, validación de esquemas, recetas y aislamiento Docker. |
| **`test_heart.py`** | Heart | Detección de fallos repetidos, diagnóstico con `CoreEyes` y logs reales, y planes de recuperación. |
| **`test_kernel.py`** | Control Store / Kernel | Almacén transaccional `control.sqlite3`, atomicidad de autorizaciones, puertos y buzón de voz. |
| **`test_mind.py`** | Mind | Extracción de intenciones (`Intent`), resolución de capacidades y proveedores, y flujo conversacional. |
| **`test_pixy.py`** | Pixy | Gobernanza estricta, confirmación binaria (`y`/`n`), emisión de `DecisionReceipt` y revisión de contratos. |
| **`test_voice.py`** | Voice | Restricción de red externa, DNS pineado, validación TLS, gestión de `secrets.env` y cliente Telegram. |

**Ejecución estándar:**
```bash
python -m pytest -v
# o bien
make test
```
**Resultado canónico actual:** 389 pruebas y 39 subpruebas pasando al 100% (0 errores, 0 fallos).
