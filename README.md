# Khai — ejecución controlada y aprendizaje con evidencia

Khai es un asistente personal para Telegram que interpreta objetivos, genera o reutiliza código Python y lo ejecuta mediante Voice con DockerRuntime o LocalRuntime aislado. Mantiene estado en SQLite para recuperar el trabajo sin repetir automáticamente operaciones de resultado incierto.

El proyecto utilizaba originalmente nombres de OpenClaw. Los contenedores Docker ahora se denominan `khai_core` y `khai_docker_proxy`. Su motor de ejecución es propio; no utiliza el Gateway de OpenClaw ni el runtime de Hermes Agent. Ambos proyectos sirvieron como referencias de diseño.

## Bloque 3 local: recuperación y aprendizaje con transacciones compartidas (29 de octubre de 2026)

Implementado bajo la opción A: transacciones compartidas en SQLite (`ControlUnitOfWork`) reutilizando los puertos y máquinas de estados existentes. No añade LangGraph ni dependencias externas.

- Heart vincula planes a la identidad observada (`task_binding`), emite órdenes declarativas canónicas validadas y supervisa la ejecución sin tolerar resultados antiguos o inferidos.
- Hand consume la autorización de Pixy y despacha la remediación atómicamente en Control Plane junto con la transición del Coordinator (`build_attempt`) y extensión acotada de presupuestos.
- Dreams deduplica observaciones por `execution_id`, rechaza explícitamente atribuciones contradictorias y consolida propuestas de habilidades y reglas atómicamente con `Rules.propose`.
- `Rules.rollback` y aprobación de reglas operan de manera atómica bajo transacciones `BEGIN IMMEDIATE`.
- Detalles y contratos en [el bloque 3](docs/BLOQUE3_RECUPERACION_Y_APRENDIZAJE.md).

## Bloque 2 local: ejecución y cancelación (29 de octubre de 2026)

Voice valida cada admisión externa contra la tarea vigente en SQLite. Cancelar
bloquea las peticiones siguientes; una petición ya admitida conserva su recibo.
Las escrituras inciertas no se repiten automáticamente.

Los servicios AGENT mantienen supervisión y gateway mientras sigan activos,
incluso después de aceptar el resultado. El estado del proceso se conserva por
separado de la aceptación del objetivo. Una corrección espera la parada confirmada
antes de replantear las acciones y mantiene el mismo objetivo.

Suite completa del corte: **232/232 pruebas**, incluyendo 20 nuevas de autoridad,
cancelación y supervisión. Sin dependencias nuevas ni cambios en Azure.
Quedan pendientes recuperación y aprendizaje, credenciales, presupuestos de
proveedores y entrega incierta de Telegram. Detalles y límites en
[el bloque 2](docs/BLOQUE2_EJECUCION_Y_CANCELACION.md).

## Bloque 1 local: conversación persistente (29 de octubre de 2026)

Implementado sobre SQLite, Voice y Coordinator existentes, sin dependencias nuevas.
Cada chat Telegram conserva una sesión; los mensajes se resuelven como charla u
operaciones internas estructuradas. Las respuestas se vinculan con preguntas
realmente enviadas; las correcciones mantienen el objetivo y descartan trabajo
de revisiones anteriores. El planificador desarrolla objetivos ya resueltos,
sin una segunda clasificación de charla frente a acción.

Suite completa del corte: **212/212 pruebas**, incluyendo 22 nuevas de
conversación, decisiones y revisiones. La prueba de composición cierra sus workers
antes de retirar los datos temporales. Los demás bloques de la auditoría siguen
pendientes; este resultado no valida todavía Telegram ni proveedores reales.
No hubo commit, push, purga ni despliegue. Detalles y límites en
[el bloque 1](docs/BLOQUE1_CONVERSACION_PERSISTENTE.md).

## Revisión local anterior de interacciones (29 de octubre de 2026)

La revisión SSH de solo lectura encontró el checkout de Azure en `6a47f3d`.
Las correcciones siguientes están **solo en el workspace local**, pendientes de
autorización de despliegue: el planificador comprueba también solicitudes que
Mind clasifica como charla o respuesta corta, recibe historial y estado real de
tareas, y no publica promesas de ejecución descartadas. Heart centra el
diagnóstico en la tarea actual; los fallos anteriores se consultan expresamente.
`/?` y `/help` muestran `/mode low`, `/mode mid`, `/mode high` y `/mode ?`
con botones que envían el comando completo.

La suite completa de este corte pasó **190/190 pruebas** con Python 3.12.
Esto valida contratos y flujos locales con proveedores simulados; falta la
comprobación real en Telegram tras la activación. No se hizo commit, push,
purga, reconstrucción ni reinicio remoto. El detalle y los límites están en
[la revisión de interacciones](docs/REVISION_INTERACCIONES_2026-10-29.md).
Las cifras y estados de los apartados siguientes pertenecen a cortes anteriores.

## Estado de esta revisión

`README.md`, [`.agents/GEMINI.md`](.agents/GEMINI.md) y
[`plan.md`](plan.md) son las fuentes de verdad del proyecto. `plan.md` conserva
el contexto histórico del plan de recuperación; el estado vigente de F5 se
describe aquí y en `.agents/GEMINI.md`. La arquitectura
vive en `app/` y el roadmap detallado está en
[`docs/ESTADO_MIGRACION_Y_ROADMAP.md`](docs/ESTADO_MIGRACION_Y_ROADMAP.md).

Fases F0 a F5 completadas y validadas con la suite completa (176/176 pruebas exitosas, incluyendo 41/41 de F5). El proyecto
no está en producción y las bases de datos y skills anteriores se purgarán antes
de usar esta revisión. No se requieren pruebas de compatibilidad con esos datos.
**Estado Beta V4, 28 de octubre de 2026:** Se completó la integración del piloto
Dolphin en Google Cloud (`us-west1-a`) sobre un único disco persistente
`dolphin-boot-disk` (`pd-standard`, 32 GB, `auto-delete=no`) conectado a Khai en
`vm-carlosperales` (Azure for Students) mediante un túnel privado WireGuard
(10.200.0.1 <-> 10.200.0.2, 22,5 ms RTT). Desde Telegram, /mode (o /m)
controla el motor conversacional: /m low selecciona Gemini y elimina la VM Spot
de Dolphin conservando el disco, /m mid crea/enciende la VM Spot CPU
(e2-standard-8), /m high crea/enciende la VM Spot GPU NVIDIA L4
(g2-standard-4), y /m ? consulta el estado actual, si el uso está cubierto
por créditos y el saldo estimado en CLP. Cada cambio de modelo emite [Mode = x]. El detalle por proveedor está en [Dolphin/README.md](Dolphin/README.md),
[`Dolphin/gcloud/PILOTO_GCLOUD.md`](Dolphin/gcloud/PILOTO_GCLOUD.md) y
[`Dolphin/azure/PILOTO_AZURE.md`](Dolphin/azure/PILOTO_AZURE.md).

## Comportamiento actual

- Un `update_id` de Telegram crea como máximo un objetivo durable. Se comprueban el secreto del webhook y la identidad del usuario, también para callbacks.
- Cada objetivo comienza con 4 generaciones, 4 ejecuciones, 16 llamadas LLM y 30 minutos de plazo. Cada corrección del propietario añade el mismo presupuesto y extiende el plazo dentro del mismo `task_id`; los contadores consumidos no se reinician.
- Si no existe una habilidad previa para una petición en lenguaje natural, Coordinator consulta al usuario antes de crearla (`WAITING_CREATION_APPROVAL`).
- Hand genera y versiona código; Pixy lo revisa; Coordinator decide las transiciones; Docker Proxy administra contenedores identificados por `execution_id`.
- Se declaran hitos de progreso explícitos en cada fase sin emplear emoticones ni emojis.
- La revisión y la aprobación se vinculan al SHA256 del archivo. Cambiar el código invalida la autorización; Pixy no reescribe un artefacto después de revisarlo.
- Pixy guarda la aprobación humana de una versión de skill con el hash del código, el hash de la versión, las capacidades y las operaciones de servicio aprobadas. Una versión activa intacta puede reutilizarla en tareas posteriores con un alcance igual o más acotado, después de una nueva revisión de Pixy. Ampliar el alcance o cambiar la versión requiere otra aprobación. La primera configuración ordinaria de escritura puede incluir el descriptor en una sola decisión; `/service approve` queda para propuestas avanzadas sin ese consentimiento.
- Cada habilidad dispone de un workspace propio. No recibe la base de datos, los módulos, los secretos ni el socket Docker del Core.
- Salir con código `0` significa que el proceso terminó. Los comprobantes tipados verifican formato y efectos técnicos; la satisfacción del objetivo completo queda en `UNVERIFIED` hasta recibir un `y` del propietario. Un `n` o una corrección en texto revisan el mismo objetivo.
- Lanzar un servicio AGENT produce `started`, no éxito verificado. Su proceso se supervisa hasta el plazo del objetivo.
- Dreams aprende exclusivamente por detonadores (event-driven: éxito verificado o fallo de proceso); las reglas nuevas pasan por estado `proposed` y aprobación explícita.
- Heart actúa como el médico exclusivo del sistema: diagnostica anomalías con Senses y ejecuta planes de remediación auditados mediante Hand.
- Pixy actúa como auditora determinista y canal de supervisión (`/p`), delegando cualquier remediación a Heart.
- Voice persiste inbox y outbox ordinarios. Los recibos inciertos no se reenvían automáticamente. Los comandos privados `/api`, `/us` y `/pas` evitan el inbox ordinario y guardan el valor solo en Secrets.
- Hand puede proponer operaciones versionadas de servicios HTTPS. La primera configuración ordinaria muestra los proveedores concretos y una sola decisión humana aprueba la creación de la skill y las operaciones declaradas; `/service` queda para propuestas avanzadas. Voice limita cada petición a la operación y versión aprobadas para la tarea. Una clave faltante deja la tarea en `WAITING_SECRET` y se reanuda con un recibo; Coordinator consulta solo disponibilidad y versión.
- El catálogo SkillSpec promociona una candidata tras aceptación y permite reutilizar una versión activa con otros parámetros. La búsqueda semántica local es provisional; sin modelo y umbrales calibrados usa selección por LLM. Las recetas tipadas ejecutan acciones reutilizables dentro de un solo objetivo; Mind conserva recibos anteriores como provisionales cuando el propietario corrige una acción posterior.
- Antes de ejecutar una petición ordinaria, Mind muestra los valores concretos que usará, marca los predeterminados guardados y espera confirmación o un cambio. Un cambio invalida la confirmación previa. La primera configuración de una capacidad nueva se autoriza una vez en lenguaje cotidiano, mostrando los proveedores propuestos; esa decisión permite crear la skill y activar sus operaciones declaradas solo para el alcance aprobado, tras la revisión interna de Pixy. Los usos posteriores de una versión activa intacta reutilizan esa aprobación dentro del mismo alcance o uno menor.
- La respuesta final de una receta presenta los resultados por acción en lenguaje legible y solo informa un envío a un destinatario concreto si Voice conserva un recibo de escritura completado cuyo parámetro de destinatario coincide con el valor confirmado. Voice guarda hashes de parámetros para esa comprobación, no copia sus valores al resumen fijo. La aceptación `y` del propietario sigue siendo posterior al efecto externo. La integración con proveedores reales de noticias y correo sigue pendiente.
- Voice transporta mensajes y registra recibos técnicos, sin evaluar la calidad del resultado. Ante una corrección que contradice un recibo de escritura, Mind pregunta qué observó el propietario antes de ofrecer actualizar el recurso registrado, crear otro con riesgo de duplicado o cancelar, según el contrato disponible.

## Componentes

| Componente | Responsabilidad |
| --- | --- |
| Orquestador / Voice | Autenticar Telegram (Webhook HTTPS 8443), persistir entradas y gestionar gateways. |
| Coordinator / ControlStore | Presupuesto, estados, transiciones, aprobaciones y trazabilidad (`trace_id`). |
| Mind | Clasificar intenciones y proponer hipótesis cognitivas. |
| Hand | Artesano único: generar scripts, guardar snapshots SHA256, versionar y ejecutar remediaciones. |
| Pixy | Auditora de seguridad, filtro AST y Ledger de autorizaciones duraderas. |
| Docker Proxy / Engine | Preparar imágenes, ejecutar, supervisar y aislar contenedores. |
| Dreams / Memory | Aprendizaje por detonador, métricas empíricas verificadas y propuestas de reglas. |
| Heart | Médico del sistema: signos vitales basales y máquina de estados de recuperación. |
| Senses | Observabilidad acotada de solo lectura (logs con epoch, estado de BD, contenedores). |

### Simplificaciones y Modelo de Enrutamiento Unificado (28 de octubre de 2026)

1. **Enrutamiento LLM Unificado y Cascada Completa:**
   - En modo `/m low`: La inferencia se dirige al pool Cloud (Gemini / Azure / OpenAI) con reintento automático entre claves disponibles y respaldo final en Dolphin.
   - En modo `/m mid` y `/m high`: La inferencia se envía prioritariamente al nodo privado Dolphin en puerto 11434; ante cualquier indisponibilidad de la VM Spot, conmuta fluidamente al pool Cloud.
   - Todos los órganos (`Mind`, `Pixy`, `Hand`, `Heart`) utilizan esta cascada unificada sin bifurcaciones paralelas.
2. **Modelo Binario de Planificación (`ActionPlanner`):**
   - Una petición del usuario se clasifica de forma binaria: `0` habilidades requeridas (charla general o consulta directa de sistema) o `N >= 1` habilidades requeridas (1 o múltiples habilidades indistintamente).
   - Se inyecta al LLM la lista de habilidades activas junto con una plantilla JSON estandarizada para proponer pasos o nuevas especificaciones `SkillSpec`.
3. **Gobernanza Conversacional sin Copia de IDs:**
   - Pixy gestiona reglas de aprendizaje y descriptores de servicio de manera conversacional (`/p`). El usuario no necesita copiar hashes SHA256 de 64 caracteres ni identificadores largos (`rule_...`, `auth_...`, `task_...`).
   - Al responder `y` o `n` sin ID específico, el sistema resuelve la decisión sobre la solicitud pendiente más reciente.
4. **Resiliencia en Recuperación (`Heart` y `Coordinator`):**
   - Heart genera diagnósticos basados en el perfil completo basal de telemetría y propone planes declarativos de reintento (`retry_task`), remediación o creación de habilidades (`create_skill`).
   - `Coordinator.retry_task` es consciente del estado previo de la tarea y reanuda en `ROUTING` cuando no existe manifiesto generado, evitando fallos de clave (`KeyError: 'manifest'`).
   - Aislamiento estricto de memoria: los eventos de conversación a largo plazo se persisten únicamente tras una respuesta válida del LLM, y las llamadas fallidas marcan la tarea como `FAILED` para evitar envenenamiento del historial.

## Modos de ejecución

| Modo | Uso | Autorización |
| --- | --- | --- |
| SAFE | Script corto en workspace privado. | Pixy revisa cada tarea; red, dependencias o una indicación de Pixy pueden requerir aprobación inicial. Una skill activa intacta reutiliza esa aprobación dentro del alcance aprobado. La lectura pública declarada se autoaprueba según la política vigente. |
| AGENT | Proceso de larga duración, sin puertos publicados automáticamente. | Requiere aprobación humana inicial del artefacto; una versión activa intacta puede reutilizarla dentro del alcance aprobado. |
| GOD | Compatibilidad con `DynamicSkill(BaseSkill)` y checkpoints locales. | Requiere aprobación humana inicial; una versión activa intacta puede reutilizarla dentro del alcance aprobado. No otorga acceso al host. |

El nombre GOD es histórico: no permite administrar el VPS, modificar el Core ni controlar Docker. La administración del host se realiza por un operador fuera del agente.

## Uso desde Telegram

```text
/help o /?
/mode <low|mid|high|?> o /m <low|mid|high|?>
/pixy <mensaje> o /p <mensaje>
/health
/cancel [TASK_ID] o /stop [TASK_ID]
/restart [core|proxy]
/us <usuario> /pas <contraseña>
/api <valor>
```

`/help` y `/?` muestran los comandos disponibles. `/mode` (o `/m`) selecciona el motor conversacional (`low`=Gemini, `mid`=Dolphin CPU Spot, `high`=Dolphin GPU NVIDIA L4 Spot, `?`=estado y créditos CLP) y notifica cada transición con `[Mode = x]`. `/health` consulta directamente a Heart el estado de RAM, disco e integridad de bases SQLite. `/cancel` y `/stop` detienen los objetivos activos (o el `TASK_ID` indicado). `/restart` reinicia en secuencia segura ambos contenedores Docker (`khai_core` y `khai_docker_proxy`). Los comandos privados `/us`, `/pas` y `/api` pueden enviarse juntos en un mismo mensaje (ej. `/us usuario /pas clave`) o en mensajes separados.

`/pixy` y `/p` dirigen el mensaje directamente a Pixy. Cuando ocurre un problema o se solicita un diagnóstico, Pixy le indica a Heart que hay un incidente; Heart inicia la investigación con la ayuda de Senses y le entrega el resultado junto con una propuesta de solución a Pixy para su aprobación, la cual puede o no requerir intervención del usuario (`y` o `n`). La consulta, aprobación o reversión de reglas de aprendizaje y descriptores de servicio también se realiza conversando con Pixy mediante `/p`.

## Instalación y operación

El despliegue está pensado para Linux con Docker Engine y Compose. Configura los valores de [runtime.env.example](config/runtime.env.example), mantén las credenciales fuera de Git y sigue [la guía de operación](docs/OPERATIONS.md). Core y proxy deben actualizarse juntos porque el protocolo de ejecución cambió.

## Pruebas

```bash
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests -v
```

Las pruebas no requieren credenciales ni un daemon Docker. Las pruebas
registradas para el estado actual son las que figuran en
[`docs/ESTADO_MIGRACION_Y_ROADMAP.md`](docs/ESTADO_MIGRACION_Y_ROADMAP.md).
La suite completa y la validación real de Docker, HTTPS, Telegram y proveedores
siguen siendo requisitos de cierre antes de considerar cualquier despliegue.

## Pendientes antes de cualquier despliegue

- Completar F5: validar de extremo a extremo la composición de lectura y
  escritura, las confirmaciones de variables, los permisos por alcance y los
  recibos de efectos individuales y en lote frente a contratos HTTPS reales o
  fixtures fieles; corregir los defectos que revele esa integración.
- Ejecutar la suite completa y las validaciones de integración sin credenciales
  productivas.
- Validar en Linux/Docker aislamiento, fault injection, recuperación
  transaccional, canario controlado y entrega durable.
- Mantener la revisión remota sin cambios durante este trabajo local.
- Mantener la Fase 7 de evolución fuera del despliegue hasta contar con
  evaluación independiente, promoción y rollback auditados.
- Mantener provisional la búsqueda semántica local. Elegir e instalar un modelo fijado, calibrar umbral y margen, y medir con casos reservados **solo después de finalizar la última fase**, como acordó Carlos. Hasta entonces no declarar completada su aceptación.
- Validar con servicios reales la aprobación reutilizable de skills y la política
  de `/service approve` al cerrar F5. La autorización para repetir una creación
  cuestionada sigue siendo una elección explícita y separada.
- La atomización por acción ya tiene recetas tipadas locales; falta validar la
  composición y reutilización de acciones con servicios reales. Un LLM local
  sigue como tema de diseño, sin aceptación de modelo ni umbrales.
- Recordatorio solicitado: más adelante eliminar la mayor parte de los comandos
  `/`, conservando solo los que sigan siendo necesarios para seguridad y operación.
- Toda comunicación del worker pasa por Voice; Telegram ingresa por su webhook autenticado. Heart puede proponer cambios locales, pero modificar código interno requiere un plan concreto, aprobación humana y ejecución acotada. Esa capacidad sigue pendiente como evolución posterior, sin cambiar el estado de cierre de F4.

## Relevo de sesión: F5 (28 de octubre de 2026)

Una decisión aplicada fue pedir una aclaración cuando el propietario dice
que falta un efecto externo pero Voice tiene un recibo de escritura. El estado
`WAITING_EFFECT_DETAIL` guarda la explicación y luego presenta solo las acciones
permitidas por el contrato: actualización del recurso identificado, creación
potencialmente duplicada o cancelación. La aclaración no concede un permiso de
escritura por sí sola; la elección posterior queda ligada al mismo `task_id`.

En una receta con escrituras en pasos distintos, la corrección solo fija el
recurso del paso cuestionado. Los demás recibos permanecen provisionales hasta
el `y` final del objetivo completo. Si un mismo paso registró varias escrituras,
Khai pregunta cuál se cuestiona y acepta una posición explícita (por ejemplo,
«la segunda» o «2») cuando el registro conserva un recibo distinto por escritura.
Solo después de identificarla ofrece la acción correctiva. Si la respuesta sigue
sin distinguirla tras la aclaración y el último intento de interpretación, cierra
el objetivo como `UNFULFILLABLE`, sin nueva escritura. Una skill nueva no verificada
se rechaza para reutilización; una versión que ya estaba activa se conserva.
Los recibos quedan auditables. Si Voice no conservó identidades distintas por
efecto, el cierre registra `system_identity_gap`, no una ambigüedad del usuario.
Una escritura en lote requiere un contrato `effect_contract.batch` que declara
la lista de entrada, la lista de resultados, una posición explícita, un estado
por elemento y un ID único por éxito. Voice guarda un recibo por elemento,
incluyendo `unknown` antes del envío para conservar la incertidumbre ante un
reinicio. Solo los éxitos con posición e ID válidos pueden seleccionarse para
corrección individual; no se infieren identidades desde texto libre ni se
reenvían automáticamente lotes parciales o inciertos. Mind detiene la receta y
rechaza la aceptación final mientras quede algún elemento sin confirmar.

Si una operación `create` quedó registrada sin identidad o corrección verificable,
Carlos eligió permitir una nueva creación solo tras una elección explícita que
avisa del riesgo de duplicado. La decisión queda ligada al `task_id`, al recibo
cuestionado y a la misma operación/version. Voice admite una sola petición nueva
por esa decisión y registra su uso antes del envío; un resultado incierto no
habilita otro intento. Las demás operaciones no se presentan como creación.

Para una operación correctiva declarada, el contrato ahora describe cada
parámetro distinto de la identidad y la pregunta natural para obtenerlo. Mind
contrasta la explicación y el recibo con esas descripciones: si falta un valor
pertinente, pregunta solo por ese valor y mantiene la tarea en
`WAITING_EFFECT_DETAIL`. La respuesta se conserva para la revisión del alcance;
recién después ofrece actualizar, crear con riesgo de duplicado o cancelar.
Esto cubre tanto un efecto reportado como ausente como un recurso registrado
con contenido incorrecto. La interpretación del LLM solo selecciona campos
declarados y exige una cita literal para considerar un valor aportado; no
autoriza por sí misma una escritura.

Decisión adicional: cada escritura necesita un nombre y contexto breve legibles
para ayudar a distinguir efectos, pero ese resumen no debe incluir claves ni
datos sensibles. Carlos eligió campos fijos `summary.name` y `summary.context`
declarados por Hand en el descriptor; Pixy los presenta para revisión al aprobar
el servicio. Voice los conserva en cada recibo técnico y Mind los presenta al
aclarar varias escrituras. No se copia automáticamente la petición, parámetros
ni contenido del recurso. El identificador queda separado. Pixy
conserva la política de acceso y Voice sigue custodiando físicamente las
credenciales conforme a la arquitectura actual. Como evolución futura, estudiar
una bóveda propia de Pixy que entregue acceso acotado mediante tokens; esto no
autoriza mover ni duplicar secretos durante F5.

La decisión sobre el resumen ya fue respondida e implementada localmente. El
descriptor de escritura exige ambos textos fijos y el gateway los entrega en
el recibo sin derivarlos del contenido variable de la operación.

El siguiente bloque de F5 es una integración controlada de una receta de lectura
y escritura (por ejemplo, buscar noticias y enviar un correo) con contratos
HTTPS representativos. Debe comprobar la confirmación de variables y valores
predeterminados, la primera autorización única de configuración, la reutilización
posterior dentro del alcance aprobado, el recibo técnico del envío y la respuesta
final legible. En el mismo bloque se contrastará el contrato por elemento de lotes
con respuestas parciales. Solo al cerrar F5 corresponde la validación integral y
Linux/Docker; la VM sigue en la revisión anterior. La bóveda de Pixy con tokens
es una propuesta futura, no una tarea autorizada para el próximo bloque.

La validación completa de cierre de F5 pasó 176/176 pruebas en total (41/41 en las 5 suites de F5). El error temporal que aparece en el log
de una prueba es simulado y verifica el reintento de rechazo de una skill no
verificada; la prueba pasa. La suite completa y Linux/Docker quedan pendientes
para el cierre; no se ha hecho commit, purga ni despliegue. No se instaló
una dependencia adicional de cifrado ni se cambió la custodia de secretos.
El `.venv` local fue recreado con Python 3.12 y se instalaron solo los
requerimientos de desarrollo necesarios; no se instalaron herramientas extra.

## Simplificaciones y saneamiento de interacción (Octubre 2026)

1. **Saneamiento conversacional:** Eliminación de prefijos y variables internas en el canal de usuario (`task_...:`, `auth_...`, `case-...`, `run_...`), boilerplates obsoletos de comandos (`/verify ...`, `/cancel ...`) y notificaciones redundantes de progreso intermedio. Los cambios de modo muestran exclusivamente `[Mode = <modo>]`, reservando la telemetría detallada de costos, máquinas y túnel para `/mode ?`.
2. **Normalización de servicios externos:** Sanitización de identificadores y dominios de servicio mediante la biblioteca estándar (`urllib.parse` y expresiones regulares) para evitar rechazos por caracteres inválidos como puntos o guiones en nombres de host (e.g. `openweathermap.org` -> `openweathermap_org`).
3. **Propagación en remediación de Heart:** Cuando Heart autoriza la remediación o creación de una nueva habilidad/servicio, la tarea se re-enruta (`ROUTING`) con capacidades de red habilitadas y reseteo de manifiesto caduco, garantizando que el código generado cuente con los servicios aprobados.

## Documentación

- [Migración actual y arquitectura corporal](docs/ARCHITECTURE_MIGRATION.md).
- [Arquitectura, contratos y estados](docs/ARCHITECTURE.md).
- [Configuración, migración, operación y recuperación](docs/OPERATIONS.md).
- [Cambios, validación y límites pendientes](docs/IMPLEMENTATION.md).
