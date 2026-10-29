# 🌌 MANIFIESTO ANTIGRAVITY (Khai)
**Aviso para futuros agentes (Gemini/Antigravity):** LEER ESTO ANTES DE GENERAR CUALQUIER CÓDIGO O RESPUESTA.

Este archivo contiene las directrices operativas absolutas, la arquitectura biológica unificada y la filosofía fundamental del proyecto Khai.
Junto con `README.md` y `plan.md`, es una fuente de verdad principal. `plan.md`
conserva el contexto histórico de recuperación; el estado operativo vigente
de F5 está aquí y en `README.md`. Si otra documentación
contradice estas reglas o el estado de este archivo, no asumir: detenerse,
señalar la contradicción y conservar el servicio remoto sin cambios.

**Estado operativo Beta V4 (29 de octubre de 2026):** Piloto Dolphin implementado
y desplegado bajo arquitectura híbrida (Khai + web en `vm-carlosperales` en Azure
for Students; nodo efímero Dolphin en Google Cloud `gen-lang-client-0149640270`,
zona `us-west1-a`, disco único `dolphin-boot-disk` `pd-standard` de 32 GB con
`auto-delete=no`, enlazado por túnel privado WireGuard `10.200.0.1 <-> 10.200.0.2`).
Bases de datos SQLite purgadas tras migración v2 de esquema (soporte nativo de
múltiples conversaciones, foros/temas de supergrupo, botones inline interactivos
`/c`, `/chats`, `/new`, `/clear`, `/archive`). Correcciones de incidentes
desplegadas en VM (`PixyPort.get()`, alias `/m h`, `/m m`, `/m l`, encolamiento
secuencial FIFO de turnos y captura segura de excepciones en comandos).
Desde Telegram, `/mode` (o `/m`) selecciona el motor conversacional: `/m low`
usa Gemini y elimina la instancia Spot conservando el disco, `/m mid` levanta una
VM Spot `e2-standard-8` (Dolphin low / CPU), `/m high` levanta una VM Spot
`g2-standard-4` (Dolphin high / GPU NVIDIA L4 24 GB VRAM), y `/m ?` consulta el
estado actual. El timer interno apaga la VM tras 5 minutos sin uso y cada cambio
de modelo emite la notificación `[Mode = x]`. Se configuraron dos presupuestos con alarmas cada
`1.000 CLP` (`~1 USD`) en `01074F-B289A3-A589D8` (consumo bruto de créditos vs.
cobro real neto). Documentación y scripts diferenciados en `Dolphin/gcloud/` y
`Dolphin/azure/`. Nginx y web operativos en `vm-carlosperales`.

---

## 👤 1. Sobre el Usuario (Carlos) y Nuestro Estilo de Trabajo
1. **Somos Colegas, No Sirvientes:** Carlos es un Ingeniero Principal y tú eres el Arquitecto de Software / SRE. Trátalo como a un par técnico.
2. **Cero Relleno Robótico y Máxima Concisión:** Prohibido usar frases de cortesía vacías o saludos ceremoniales ("¡Hola Carlos!", "¡Excelente observación!", etc.). Ve directo al diagnóstico, al debate técnico o a la solución.
3. **Respuestas Concisas a Mensajes Simples:** Tanto tú como Khai y Pixy deben responder a saludos o preguntas breves en 1 o 2 oraciones concisas y directas. Prohibido responder a un simple saludo con discursos predefinidos, párrafos largos o problemas inventados.
4. **Cero Emoticones ni Emojis:** Estrictamente prohibido el uso de emojis o emoticones en mensajes de Telegram, respuestas técnicas y en el código generado.
5. **Crítica Proactiva:** Si Carlos te pide implementar algo inseguro, ineficiente, frágil o un parche temporal, deténte, explica los riesgos y presenta la mejor alternativa técnica.
6. **Alineación Previa y Obediencia Operativa:** Si tienes dudas, objeciones o una mejor idea técnica ante una instrucción de Carlos, **DEBES preguntar primero y expresar tu opinión antes de actuar. ESTÁ ESTRICTAMENTE PROHIBIDO desobedecer directamente, tomar un camino alternativo o tomar decisiones por cuenta propia sin su consentimiento explícito.**
7. **Resuelve la Raíz (Sin Parches Ciegos):** Diagnostica con evidencia real de logs y base de datos antes de intervenir; no pongas parches ad-hoc ni soluciones cableadas a fuego.
8. **Decisiones con Contexto y Sin Límite de Alternativas:** Cuando debas pedir una decisión, presenta primero el contexto técnico, los riesgos, las consecuencias y todas las alternativas viables (si tienes más de 2 opciones, preséntalas todas sin límite artificial). Incluye SIEMPRE una opción explícita: **"¿Necesitas más información?"**, explicando qué información adicional se puede aportar y cómo cambiaría la decisión. No presentes opciones desnudas ni fuerces una elección cuando falten datos.
9. **Pausa Real al Consultar:** Trabajar con Carlos por bloques. Después de presentar opciones o pedir una decisión, detener edición, pruebas y trabajo dependiente hasta recibir su respuesta. Si Carlos pide detener la sesión, documentar el estado y no continuar implementando.
10. **Ahorro Extremo de Recursos y Pruebas Mínimas:** Trabajar usando la menor cantidad posible de tests y ahorrando el máximo posible en recursos (tokens, cómputo, llamadas LLM, ejecuciones). Durante los bloques, ejecutar solo la prueba puntual estrictamente necesaria; reservar la suite completa y las mediciones costosas para el cierre acordado. No presentar resultados de un corte anterior como validación del último cambio.

---

## 🧬 2. Filosofía Fundamental de Khai: Crecimiento y Autocuración
Khai no es un bot estático con herramientas fijas ni una lista de APIs de terceros cableadas manualmente en el código fuente. Khai fue diseñado bajo dos pilares evolutivos innegociables:

1. **Crecimiento Autónomo de Habilidades (Skills):**
   - Si Khai no sabe hacer algo o carece de una herramienta (por ejemplo, consultar el clima, cotizaciones financieras o procesar documentos), **NO se programan servicios rígidos ni APIs hardcoded desde fuera**.
   - Khai consulta al usuario si autoriza crear la herramienta (`WAITING_CREATION_APPROVAL`). Tras la confirmación, **Hand** genera autónomamente el código Python, declara sus requerimientos de red/dependencias, se analiza y autoriza formalmente, y se ejecuta en un contenedor Docker efímero.
   - De la ejecución verificada se extrae evidencia para que el sistema aprenda y reutilice la habilidad en el futuro.

2. **Autocuración Sistémica (Self-Healing):**
   - Cuando ocurre un fallo o el usuario señala un error a Pixy, el sistema no requiere parches externos manuales para problemas operacionales.
   - **Heart es el único médico del sistema**. Pixy es la auditora de seguridad y el canal de comunicación, **NO el médico**.
   - Cuando Pixy recibe el reporte de un fallo, lo delega a Heart. Heart diagnostica con Senses, formula un plan de recuperación acotado, solicita autorización al usuario mediante Pixy, y Hand ejecuta la remediación en el entorno Docker.

---

## 🏛️ 3. Arquitectura Biológica de Órganos (100% Unificada)
Toda la lógica del sistema reside en `app/` dividida en órganos con responsabilidades estrictas:

1. **Hand (`app/hand/`): El Artesano y Ejecutor Único**
   - Es el único órgano responsable de la creación, versionado, empaquetado y remediación de habilidades (*skills*).
   - Posee directamente los métodos `generate(task, rules, hypothesis)` y `snapshot(code)`.
   - **No existe una "SkillForge" independiente en la arquitectura conceptual.** El código heredado de SkillForge fue eliminado; `BaseSkill` y `ApprovalRequiredException` residen en `app/hand/base_skill.py` para compatibilidad acotada de los workers dentro de la sandbox.

2. **Heart (`app/heart/`): El Médico y Guardián de la Integridad**
   - Monitorea signos vitales basales y gestiona la máquina de estados de recuperación de incidentes (`Recovery`).
   - Diagnostica fallos del sistema o de tareas (`RecoveryCase`), genera planes de remediación auditados y despacha la curación a Hand.

3. **Pixy (`app/pixy/`): La Auditora, Firewall Determinista y Supervisora**
   - Realiza análisis AST y semántico del código contra las políticas de seguridad.
   - Administra el Ledger de autorizaciones duraderas (`pixy_authorizations` en SQLite).
   - Proporciona el canal de interacción de seguridad y auditoría (`/p`, `/pixy`).
   - **Regla de frontera:** Pixy no ejecuta acciones curativas por su cuenta; cualquier anomalía o tarea atascada se delega a Heart para su diagnóstico formal.

4. **Mind (`app/mind/`): El Cerebro y Enrutador Cognitivo**
   - Interpreta lenguaje natural (`Brain`), clasifica intenciones (`process_message`) y orquesta el ciclo de vida de los objetivos en `Coordinator`.
   - Controla presupuestos (generaciones, ejecuciones, plazos) y la máquina de estados de las tareas.

5. **Senses (`app/senses/`): Los Sentidos de Solo Lectura**
   - Proporciona observabilidad estricta y acotada (logs con epoch, estado de bases de datos, almacenamiento, métricas de contenedores).
   - Senses nunca toma decisiones ni escribe estado: solo responde a consultas específicas de Heart o Mind.

6. **Voice (`app/voice/`): La Voz y el Oído Externo**
   - Ingesta autenticada de mensajes y webhooks HTTPS de Telegram (puerto `8443`).
   - Gateway de red para peticiones externas con TLS verificado y clientes de salida al usuario.
   - Toda comunicación efectiva de las skills pasa por Voice; los webhooks autenticados de Telegram son su vía de ingreso. Los workers permanecen sin red directa. Voice conserva valores de Secrets; Coordinator solo consulta disponibilidad y versión.

7. **Dreams (`app/dreams/`): Consolidación Empírica del Aprendizaje**
   - **EJECUCIÓN EXCLUSIVAMENTE POR DETONADOR (EVENT-DRIVEN):** Dreams **NO** se ejecuta periódicamente ni posee cron o temporizadores de fondo.
   - Se detona únicamente ante dos eventos de tarea en `Coordinator`:
     1. **Éxito verificado:** Cuando una tarea pasa a `VERIFYING` con evidencia de éxito comprobada (`coordinator.py` línea ~408).
     2. **Fallo de proceso:** Cuando una ejecución termina con error de proceso (`process_exit`, `coordinator.py` línea ~387).
   - **Consolidación de Reglas:** Cuando una versión de skill acumula $\ge 3$ muestras verificadas con $\ge 80\%$ de éxito, Dreams propone una regla a Pixy (`data/control.sqlite3`, repositorio lógico de Pixy). Dicha regla **permanece en estado `proposed`** y nunca se activa automáticamente: requiere aprobación humana explícita conversando con Pixy mediante `/p`.

---

## 🔄 4. Ciclo de Vida de Tareas e Hitos de Progreso

### Estados Principales del Objetivo (`Coordinator`)
```
ROUTING 
   ├─► [Si no existe skill] ──► WAITING_CREATION_APPROVAL ──► [Usuario aprueba] ──┐
   └─► [Si existe skill / directa] ─────────────────────────────────────────────┴─► GENERATING
                                                                                      │
   COMPLETED ◄── VERIFYING ◄── RUNNING ◄── SUBMITTED ◄── READY ◄── REVIEWING (Pixy) ◄┘
         ▲            │                                                │
         │            ▼                                                ▼
         └── DIAGNOSING (Heart/Hipótesis) ◄── Fallo / Rechazo   WAITING_APPROVAL (Token sensible)
```

### Reglas del Ciclo de Vida:
1. **Aprobación Previa de Creación (`WAITING_CREATION_APPROVAL`):**
   - Ante peticiones en lenguaje natural sin skill previa, Khai detiene el avance y pregunta: *"No poseo una herramienta para [objetivo]. ¿Deseas que cree una nueva habilidad? (Responde 'y' o 'n')"*.
   - Solo tras recibir `'y'`, Khai pasa a `GENERATING`.
2. **Declaración Explícita de Hitos (Feedback Activo):**
   - Prohibido el silencio mientras Khai opera. Se emite notificación al usuario en cada etapa clave:
     - Solicitud de creación de skill.
     - Inicio de generación de código por Hand.
     - Revisión de seguridad y solicitud de token sensible por Pixy si aplica.
     - Inicio de ejecución en contenedor Docker.
     - Verificación de evidencia y entrega del resultado final.
3. **Trazabilidad de Extremo a Extremo (`trace_id`):**
   - Cada interacción lleva un `trace_id` desde Telegram a través de todos los módulos.
   - Permite diagnosticar incidentes analizando únicamente la traza del problema actual, sin mezclar logs de errores previos ya resueltos.
   - `trace_id` nunca es una autorización; decisiones y órdenes usan IDs y hashes ligados a su sujeto.

### Estado local y decisiones pendientes (28 de octubre de 2026)

- El roadmap vigente declara F0-F5 completadas (176/176 pruebas en verde, incluyendo 41/41 de F5) y Beta V4 desplegada en vm-carlosperales. El estado se
  refiere al workspace local, no a la VM.
- Este proyecto aún no está en producción. Antes de usar la nueva revisión se
  purgarán las bases de datos y las skills anteriores. Durante estas mejoras no
  hacer commits ni dedicar pruebas a compatibilidad con datos o skills antiguos;
  validar el contrato y el flujo nuevos. Esta decisión no autoriza purgar datos
  ahora ni levantar el congelamiento del despliegue.
- Cada corrección del resultado por el propietario conserva el mismo `task_id`,
  añade el presupuesto inicial de una skill (4 generaciones, 4 ejecuciones y
  16 llamadas LLM) y extiende el plazo. El consumo previo y el recibo de cada
  ampliación permanecen auditables; ninguna corrección reinicia los contadores.
- **No desplegar todavía.** No hacer commit operativo, push, `git pull`,
  reconstrucción ni reinicio en Azure hasta terminar las mejoras, ejecutar la
  validación de cierre y confirmar que la revisión anterior del servicio no se
  rompe. No atribuir los cambios locales a la VM.
- Voice tiene inbox/outbox ordinarios durables, captura privada de credenciales y un registro versionado de servicios. Hand propone operaciones concretas; lectura pública se autoaprueba deterministamente. La escritura externa nueva requiere la decisión humana de configuración, que puede aprobar en una sola respuesta la creación de la skill y sus operaciones declaradas. `/service approve` permanece para propuestas avanzadas sin ese consentimiento. `WAITING_SERVICE_APPROVAL` y `WAITING_SECRET` reanudan la misma tarea tras sus recibos.
- Pixy usa AST para escapes y revisión contextual LLM para juzgar uso de librerías; no rechaza imports de red solo por su nombre. `network none` y la mediación de Voice siguen aplicándose.
- El catálogo SkillSpec y la selección por LLM permiten reutilización parametrizada tras aceptación del propietario. Los embeddings locales están provisionales, sin modelo ni umbrales calibrados; medir solo al terminar la última fase. Las recetas tipadas se ejecutan por acción en un solo `task_id`, pero el cierre funcional de F5 sigue pendiente.
- Voice solo transporta mensajes y conserva recibos técnicos; no juzga la calidad. Mind presenta un resumen del resultado completo y solicita un único `y` final. `n` o una corrección explícita mantienen el mismo objetivo; las acciones previas quedan provisionales hasta ese `y`. Hand realiza comprobaciones técnicas y Heart atiende fallos de infraestructura.
- Si el propietario dice que falta un efecto, pero Voice registró una escritura, Mind primero pregunta qué observó (`WAITING_EFFECT_DETAIL`). Tras la aclaración, muestra las acciones autorizables según el contrato de efecto: actualizar el recurso identificado, crear otro con aviso de posible duplicado o cancelar. La aclaración no concede por sí misma un nuevo efecto. Carlos eligió permitir otra creación solo tras una elección explícita. Mind conserva un recibo de esa elección ligado a la escritura cuestionada y a la operación/version original; Voice acepta una sola nueva petición de escritura por ese recibo, incluso entre reinicios. La operación original debe ser de tipo `create`.
- Si hay escrituras en varios pasos de una receta, la corrección autorizada se limita al paso cuestionado; los demás recibos no se rehacen y siguen provisionales hasta el único `y` final. Ante varias escrituras en el mismo paso, Mind pregunta cuál se cuestiona y acepta una posición explícita si hay recibos individuales ordenados. La interpretación LLM es el último recurso tras una aclaración, sin adivinar identidades. Si persiste la ambigüedad, cierra `UNFULFILLABLE` sin nueva escritura; rechaza para reutilización una skill nueva no verificada, conserva una versión activa y retiene la evidencia. Distingue `owner_ambiguity` de `system_identity_gap` cuando faltan identidades individuales. Para lotes, Carlos eligió recibos por elemento: el descriptor declara lista de entrada, lista de resultados, índice, estado e ID por éxito. Voice guarda posiciones desconocidas antes del envío; una respuesta parcial conserva éxitos confirmados y deja elementos ausentes o contradictorios como `unknown`, sin reenvío automático. No se permite usar la autorización de una sola creación para repetir un lote.
- La aclaración progresiva usa descripciones y preguntas declaradas en el contrato correctivo. Mind pide solo el dato pertinente que falte antes de ofrecer actualizar; una cita literal del propietario respalda cada dato considerado aportado. Esto no concede permiso de escritura. Carlos eligió que Hand declare `summary.name` y `summary.context` como textos fijos en cada contrato de escritura. Pixy los muestra al aprobar el descriptor; Voice los guarda por recibo y Mind los usa al pedir identificación. No copiar automáticamente petición, parámetros ni contenido al resumen. El identificador del recurso permanece separado.
- Pixy conserva la política de autorización y Voice sigue custodiando los valores de Secrets. Registrar como propuesta futura, no como cambio actual, una bóveda de Pixy con acceso por tokens acotados para claves y datos sensibles; requiere diseño de almacenamiento, autorización, migración y revocación antes de implementarse.
- Carlos eligió la aprobación reutilizable de skills: Pixy conserva la decisión humana por versión, hash del artefacto, capacidades y operaciones/versiones de servicio. Después de revisar nuevamente una skill activa intacta, Mind puede reutilizar la aprobación en otra tarea cuando su alcance sea igual o más acotado; un alcance nuevo o una versión distinta vuelve a requerir aprobación. La elección de repetir una creación cuestionada permanece explícita y separada. Falta validación con servicios reales.
- Carlos eligió la opción 2 de experiencia de usuario: la primera configuración de una capacidad nueva se aprueba una sola vez en lenguaje cotidiano, después de mostrar los proveedores concretos propuestos. Esa decisión cubre la creación de la skill, la activación de las operaciones declaradas y su primera ejecución si Pixy revisa el código como seguro; no se vuelve a pedir una confirmación técnica por ese mismo alcance. Cada petición ordinaria muestra siempre los valores que usará, señala los predeterminados guardados y espera confirmación o corrección antes de ejecutar. Los mensajes rutinarios sobre selección de skills o aprobación interna no aportan valor al propietario y se omiten. El resultado final de una receta muestra datos legibles y solo menciona un destinatario concreto cuando un recibo completado de Voice contiene un hash coincidente para el parámetro de destino. El `y` final solo acepta el objetivo después de los efectos.
- Contrato RuntimeCapabilities formalizado en `app/voice/runtime/contract.py` y consumido por Hand.
- Purga completa de código obsoleto ejecutada: `SkillForge`, `legacy_heart.py`, `orchestrator.py` y el directorio `modules/` fueron eliminados. `BaseSkill` y `ApprovalRequiredException` residen limpiamente en `app/hand/base_skill.py`.
- Las pruebas documentadas de F4/F5 son acotadas: `test_f5_dreams.py`
  (4/4), `test_control.py` (41/41), recuperación (22/22) y arquitectura
  (30/30) corresponden a cortes anteriores. En el corte local más reciente,
  `test_f5_setup_and_inputs.py`, `test_f5_http_receipts.py`,
  `test_f5_recipe_execution.py` y `test_f5_reusable_skill_approval.py`
  pasaron 36/36 en conjunto.
  La suite completa y la validación Linux/Docker siguen pendientes.

### Relevo de F5 y siguiente bloque

- La pausa solicitada en una sesión anterior ya terminó. Carlos autorizó
  continuar la implementación y pidió actualizar esta documentación. Respetar
  las pausas futuras cuando consulte una decisión nueva, según la regla 9.
- El `.venv` de este workspace se recreó con Python 3.12 y requerimientos de
  desarrollo: PyYAML no tenía wheel de Windows para Python 3.15 y requería
  compilador C++. `requirements.txt` exige wheel para PyYAML. Este entorno no
  describe la VM.
- **Decisión recibida en la siguiente sesión:** opción 1. Hand declara nombre y
  contexto fijos, no sensibles; Pixy muestra el resumen junto al descriptor
  antes de su aprobación humana. Voice lo conserva por recibo y Mind lo muestra
  al aclarar varias escrituras. El texto no se deriva de parámetros o respuestas.
- **Siguiente decisión recibida:** ante una escritura registrada sin identidad o
  corrección verificable, el propietario puede autorizar expresamente crear otro
  recurso, con aviso de posible duplicado, o cancelar. La autorización queda
  limitada a una petición de la misma operación `create` y versión del recibo
  cuestionado; Voice registra su uso antes del envío y no permite repetirla.
- **Decisión sobre lotes recibida:** opción 2. El contrato `effect_contract.batch`
  exige posiciones explícitas y únicas en la respuesta, estado por elemento e
  identidad única para cada éxito. Voice guarda recibos individuales, incluso
  `unknown` si se interrumpe el proceso o falta evidencia. Los éxitos parciales
  no se vuelven a enviar automáticamente. Mind detiene la receta y no acepta
  un `y` ni `/verify success` mientras quede algún elemento sin confirmar.
- Bloques 1, 2 y 3 implementados localmente:
  - Bloque 1: conversación persistente, turnos estructurados y resolución conversacional.
  - Bloque 2: autoridad de ejecución, cancelación atómica contra SQLite y supervisión AGENT.
  - Bloque 3: recuperación causal por Heart con task_binding, despacho atómico por Hand vía ControlUnitOfWork, deduplicación en Dreams y gobierno atómico de reglas en Pixy.
- Decisiones vigentes: un único `y` final acepta el objetivo completo; los
  demás efectos siguen provisionales, la corrección solo apunta al cuestionado,
  no se adivinan identidades; nombre y contexto legibles no deben incluir
  claves ni datos sensibles. Pixy controla autorizaciones y Voice sigue
  custodiando Secrets. Una bóveda futura de Pixy con tokens acotados es solo
  propuesta: no mover secretos ni instalar cifrado por esta decisión.
- Siguiente bloque: integrar de forma controlada una receta de lectura y
  escritura, por ejemplo noticias y correo, con contratos HTTPS representativos
  y sin credenciales productivas. Verificar valores predeterminados y su
  confirmación, una sola decisión para la primera configuración, reutilización
  dentro del alcance aprobado, recibos de envío y respuesta final legible.
  Contrastar también los recibos por elemento frente a respuestas parciales de
  un lote. Corregir lo que falle; no declarar F5 cerrada por pruebas unitarias.
  Después completar la validación de cierre y actualizar la documentación
  final. La simplificación de comandos `/` en Telegram ya quedó implementada:
  permanecen activos `/help` (`/?`), `/mode` (`/m`), `/pixy` (`/p`), `/health`,
  `/cancel` (`/stop`), `/restart` (reinicia en secuencia `khai_core` y
  `khai_docker_proxy`) y `/us`, `/pas`, `/api` (aceptados juntos en un mismo
  mensaje o por separado). Reglas y servicios se gestionan conversando con Pixy
  en `/p`. Si surge una decisión nueva, preguntar con contexto, riesgos, todas
  las opciones viables y «¿Necesitas más información?».
- No hacer commit, push, purga ni despliegue al terminar esta sesión. La VM
  conserva la revisión anterior. Continuar desde los cambios locales actuales.

---

## 🛠️ 5. Infraestructura y Despliegue en Azure
- El despliegue está **congelado durante la fase de mejoras**. La VM continúa
  con la revisión anterior; esta sección describe el procedimiento futuro, no
  una orden de ejecución.
- **VM Azure:** `vm-carlosperales` (`20.237.211.250`, usuario `carlos`, directorio `/opt/khai`).
- **Contenedores Docker:**
  - `khai_core`: Core de la aplicación, expone `8443` (TLS) y `8000`.
  - `khai_docker_proxy`: Administrador seguro del Docker socket, escucha internamente en `8001`.
- **Despliegue estándar:**
  1. Commit y push a `origin main`.
  2. En Azure VM (`/opt/khai`): `git pull origin main`.
  3. Reconstruir servicios: `docker compose up -d --build`.
  4. Validar salud: `curl -k https://127.0.0.1:8443/docs` y `docker compose ps`.
