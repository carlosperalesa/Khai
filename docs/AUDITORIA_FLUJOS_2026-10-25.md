# Auditoría de creación de herramientas y autorreparación de Khai

Fecha: 25 de octubre de 2026, America/Santiago. Revisión local: `d8dc31d`.

Plan de ejecución asociado: [PLAN_AUTONOMIA_Y_AUTORREPARACION.md](PLAN_AUTONOMIA_Y_AUTORREPARACION.md).

## 1. Dictamen

Khai tiene una base útil de aislamiento, persistencia, versionado y evidencia. Sin embargo, sus dos ciclos centrales todavía no cumplen conjuntamente la filosofía declarada: **crear una capacidad que falta, verificarla, reutilizarla y reparar sus fallos sin perder el objetivo ni ampliar implícitamente la autoridad**.

Los problemas observados no se explican principalmente por el modelo LLM. Hay contratos contradictorios entre generación y ejecución, atajos que confunden reintento con recuperación, autorizaciones fragmentadas, y pruebas que validan esos atajos. Cambiar el modelo o aumentar los reintentos dejaría esas causas intactas.

La recomendación es conservar la arquitectura por órganos y el runtime propio, reparar primero los contratos y completar dos recorridos verticales reales: una herramienta nueva que consulta un servicio externo, y esa misma herramienta reparándose ante un fallo inducido. La comparación con Hermes y OpenClaw aporta patrones; no demuestra que migrar de runtime resuelva los defectos locales.

## 2. Alcance y evidencia

Se revisaron el [README](../README.md), el [manifiesto de filosofía](../.agents/GEMINI.md), [arquitectura](ARCHITECTURE.md), [migración](ARCHITECTURE_MIGRATION.md), [operación](OPERATIONS.md), [implementación](IMPLEMENTATION.md) y el [plan previo de recuperación](../plan.md). Se recorrieron composición, ingreso Telegram, Coordinator, Hand, Pixy, autorizaciones, Heart, Senses, Dreams, gateway, worker, runtimes, proxy y pruebas.

Validación ejecutada:

- Suite existente: `python -B -m unittest discover -s tests -v`, con el intérprete del entorno virtual del proyecto: **94 pruebas, todas correctas, 63,701 segundos**.
- Quince observaciones dirigidas con clases reales, SQLite y archivos temporales; LLM, Telegram y Docker sustituidos por dobles. Resultados en la sección 7.
- `git diff --check` sin errores antes de escribir los entregables.
- Consulta de cuatro páginas oficiales de Hermes y OpenClaw, enumeradas en la sección 8.

No se consultó la VM ni se ejecutaron reparaciones, despliegues o llamadas con credenciales. No había un archivo de log operativo local que permitiera atribuir los incidentes reportados a ejecuciones concretas. Los hallazgos prueban comportamientos del código local; la frecuencia y el impacto real en Azure requieren correlacionarlos con recibos y eventos del despliegue. Las referencias remotas son documentación viva, no una revisión de seguridad del código de esos proyectos.

Se conservó el cambio preexistente de eliminación de `publisher.tar.gz`. Esta auditoría añade documentación; no modifica la lógica de producción.

## 3. Filosofía que debe gobernar el diseño

De la documentación se desprenden seis compromisos:

1. **Crecimiento autónomo con consentimiento:** si falta una herramienta, Hand debe poder crearla sin que un desarrollador programe una integración nueva para cada necesidad.
2. **Propiedad por órgano:** Mind interpreta y Coordinator conserva el objetivo; Heart diagnostica; Senses observa; Hand construye y ejecuta la remediación; Pixy revisa y autoriza; Voice transporta, ejecuta técnicamente y protege secretos.
3. **Evidencia antes de declarar éxito:** ni texto del modelo, ni código de salida cero, ni reinicio solicitado equivalen a objetivo cumplido.
4. **Autoridad acotada:** permisos ligados al contenido, operación, objetivo y plazo; los workers no reciben secretos, bases del Core ni Docker socket.
5. **Persistencia sin repetir efectos inciertos:** un reinicio conserva identidad, presupuestos, decisiones y recibos.
6. **Aprendizaje por detonadores:** Dreams observa resultados verificados o fallos de proceso y propone conocimiento; no activa reglas por reflexión o temporizador.

Hay decisiones documentales incompatibles que deben resolverse explícitamente:

| Tema | Documentación / intención | Implementación observada | Resolución recomendada |
| --- | --- | --- | --- |
| Acceso externo | README/arquitectura antigua permiten red aprobada; migración exige Voice | Hand propone HTTP directo; Docker fuerza `network none` | Conservar aislamiento y ofrecer a Hand capacidades de Voice extensibles y aprobables |
| APIs nuevas | El manifiesto excluye integraciones rígidas creadas desde fuera | Gateway solo conoce operaciones de YAML; configuración inicial vacía | Registro durable de propuestas de servicios creado por el agente y aprobado por Pixy |
| Médico único | Heart diagnostica fallos del sistema y de tareas | Coordinator diagnostica con Mind; Heart reinicia tareas sin corregirlas | Heart debe poseer el caso y la evidencia; Brain puede asistirle sin poseer las transiciones |
| Administración del Core | README reserva administración del host al operador | RecoveryEngine ya reinicia Core y proxy | Distinguir tareas, recuperación operacional cerrada y reparación/despliegue del Core |
| Presupuestos | Reintentos comparten presupuesto del objetivo | `retry_task` pone contadores a cero | Presupuesto acumulado; extensiones explícitas y auditadas |
| Aprendizaje | Arquitectura anterior exige tres objetivos distintos y retiro por fracaso | Dreams cuenta ejecuciones y no retira reglas degradadas | Definir contrato actual y probarlo sobre Dreams real |
| Pruebas | Implementación y plan previo hablan de 70 | Suite actual tiene 94 | Registrar versión, alcance y resultado; no presentar un número como garantía funcional |

El [plan previo](../plan.md) ya propuso acertadamente el ciclo Pixy → Heart → Senses → Hand → verificación. Parte de ese ciclo existe. Su problema actual es la desviación de contratos, especialmente el atajo de reparación de tareas, no la ausencia total de infraestructura de recuperación.

## 4. Hallazgos prioritarios

P0 significa bloquear la ampliación de autonomía hasta corregirlo; no implica que se haya demostrado una intrusión. P1 afecta directamente confiabilidad o cumplimiento del objetivo. P2 corresponde a consolidación operativa. «Reproducido» indica ejecución controlada local; «inspección» indica evidencia del código sin validación en Linux/Azure.

### A01 · P0 · Se generan herramientas de red que el runtime no puede ejecutar

**Evidencia:** [Hand.generate](../app/hand/hand.py#L102) autoriza en el prompt `urllib.request`, `requests` y `httpx` para APIs públicas. [Pixy](../app/pixy/pixy.py#L68) permite esos imports con `Network`; la [prueba correspondiente](../tests/test_architecture.py#L150) lo afirma. Pero [Coordinator](../app/mind/coordinator.py#L364) siempre envía `network=False` y [Docker Engine](../app/voice/runtime/docker_engine.py#L53) rechaza habilitar red y utiliza `--network none`.

El worker sí implementa [khai_gateway.request](../app/voice/runtime/worker_runtime.py#L33), pero Hand no recibe ese contrato en su prompt. El [gateway](../app/voice/gateway/external_gateway.py#L18) solo utiliza servicios preconfigurados y [la configuración](../config/gateways.yaml) contiene `services: {}`.

**Efecto:** una consulta de clima, precios o APIs puede superar la auditoría y obtener aprobación, pero fallar por una capacidad físicamente ausente. Regenerar el código no cambia esa ausencia. Los fallos de tareas `Network` además pueden terminar en `UNKNOWN`, lo que impide una reparación ciega con razón, pero deja al usuario sin salida útil.

**Corrección:** contrato único de capacidades efectivo, descubrimiento previo a generación y registro dinámico revisado de servicios en Voice. No resolverlo con una lista de APIs de clima/finanzas escritas manualmente ni con acceso general a Internet.

### A02 · P0 · Heart declara recuperación antes de ejecutar y verificar la corrección

**Evidencia reproducida:** en [Recovery.advance](../app/heart/recovery.py#L279), el caso con target `task` llama directamente a `Coordinator.retry_task` y pasa de `AUTHORIZED` a `RECOVERED`. En la reproducción hubo **cero llamadas a Hand**, **cero lecturas posteriores de Senses** y la autorización siguió `approved`.

La [prueba de integración de reparación](../tests/test_recovery.py#L356) espera expresamente `RECOVERED` mientras la tarea está en `GENERATING`.

**Efecto:** «reparado» significa únicamente «se volvió a poner en cola». El caso queda cerrado aunque la siguiente generación falle igual. Tampoco existe un artefacto de reparación ni un recibo de Hand para esa operación.

**Corrección:** la reprogramación es un paso del plan. El caso debe permanecer en ejecución/verificación hasta recibir evidencia del nuevo intento y comparar el síntoma original. Revisar la prueba que legitima el falso positivo.

### A03 · P0 · El reintento borra la causa y renueva límites sin control

**Evidencia reproducida:** [retry_task](../app/mind/coordinator.py#L108) pone a cero generaciones, ejecuciones y llamadas LLM; renueva 30 minutos; elimina `error` y `feedback`. Conserva campos del intento anterior como `execution_id`, `diagnosed` y `alternatives`.

El método tampoco valida el estado de origen. Se comprobó que acepta tareas `UNKNOWN` y `CANCELLED` y las devuelve a `GENERATING`. El diagnóstico normal de Heart selecciona `FAILED`/`EXHAUSTED`, por lo que esto es un defecto de la frontera del método, no prueba de que hoy todas las tareas inciertas se repitan automáticamente.

**Efecto:** pierde el dato necesario para reparar, puede reutilizar código anterior al quedar `feedback` vacío y debilita el presupuesto acumulado. Una cancelación ocurrida después de formular el plan puede ser deshecha por la orden antigua.

**Corrección:** validar estado y revisión esperados; conservar intentos y evidencia inmutables; crear un nuevo intento con instrucción de cambio concreta; contabilizar extensiones de presupuesto sin reiniciar los consumos históricos. Rechazar reintentos de resultados inciertos hasta reconciliarlos.

### A04 · P0 · `y/n` puede actuar sobre otra solicitud y no deduplica la decisión

**Evidencia reproducida:** [Pixy.direct_response](../app/pixy/pixy.py#L129) busca primero cualquier autorización de recuperación pendiente y después la última tarea esperando creación. La creación usa `store.update`, no `store.command(source, ...)`.

Con dos tareas esperando creación, enviar dos veces `y` con el **mismo `source=telegram:duplicate`** dejó ambas en `GENERATING`. La primera aprobación consume la última espera; la segunda cae sobre la anterior. Recuperación y creación tampoco comparten una única cola de preguntas: una respuesta a una creación puede aprobar primero una reparación pendiente.

**Efecto:** reentregas de Telegram y respuestas tardías pueden autorizar algo distinto a lo presentado. Hay autenticación del propietario, pero falta vinculación entre mensaje y decisión.

**Corrección:** solicitud durable con ID, tipo, sujeto, revisión, mensaje de origen y vencimiento; callback con token; inbox deduplicado para todos los comandos. Aceptar `y/n` solo como abreviación cuando haya una pregunta inequívoca, y resolverla una sola vez.

### A05 · P0 · El alcance autorizado no identifica todo lo que se ejecutará

Hay tres problemas relacionados:

- [plan_hash](../app/common/recovery.py#L53) usa `validate_plan`, que conserva solo target, pasos y capacidades. Dos planes de un mismo caso con `task_id` distintos produjeron **el mismo hash**. [Recovery._order](../app/heart/recovery.py#L274) también omite la tarea concreta.
- [Hand.approve](../app/hand/hand.py#L309) acepta el fallback `check_authorization(trace)` sin comparar capacidad, objetivo ni hash de la herramienta. Se activó una skill `Network` usando una autorización `DockerRestart` para `core` con la misma traza.
- La ruta de tarea descrita en A02 verifica el permiso, pero no lo consume.

**Límite de la evidencia:** la aceptación cruzada se reprodujo en la API interna de Hand; no se demostró que un worker pueda invocar esa API ni que el recorrido normal de Coordinator use ese fallback. Sigue siendo una frontera incorrecta para integrar nuevos flujos.

**Corrección:** una autorización debe vincular identidad de operación, sujeto concreto, revisión, plan, artefacto/bundle, capacidades y política. `trace_id` sirve para correlación, nunca como credencial. Consumo idempotente vinculado a la misma orden y ejecutor.

### A06 · P1 · El catálogo mezcla acciones de tarea y de infraestructura

**Evidencia reproducida:** [validate_plan](../app/common/recovery.py#L31) valida que acción y target pertenezcan a conjuntos globales, pero no la compatibilidad del par. Aceptó `retry_task` sobre `core`.

[RecoveryEngine.submit](../app/voice/runtime/recovery_engine.py#L68) comparte ese catálogo. Cualquier acción distinta de `health_check` cae en `_restart`. Con Docker simulado, `retry_task`/`task` produjo la orden `docker restart --time 10 task`.

**Efecto:** el contrato permite solicitudes que el ejecutor interpreta como otra operación. El atajo actual de Heart evita este camino para tareas, pero trasladarlo sin corregir contratos a Hand introduciría un nuevo fallo.

**Corrección:** tipos discriminados por acción con targets permitidos y handler explícito. Un handler de tareas debe pedir una transición validada a Coordinator; el de infraestructura solo acepta operaciones operacionales de su catálogo. Nunca usar «todo lo demás significa reiniciar».

### A07 · P1 · La recuperación no se dispara automáticamente y puede diagnosticar otra tarea

**Evidencia:** [Beat](../app/heart/beat.py#L27) registra signos vitales, pero no abre casos. [Coordinator.tick](../app/mind/coordinator.py#L167) informa fallos/agotamiento y avanza casos existentes, sin abrir uno por cada incidente. `DIAGNOSING` consulta directamente a Mind, en lugar de entregar el caso a Heart.

[bootstrap](../app/bootstrap.py#L78) devuelve las últimas tareas en orden ascendente y [Recovery.diagnose](../app/heart/recovery.py#L184) toma `failed_tasks[0]`: en la reproducción eligió `older` en vez de `newer`. La pregunta o el ID mencionado no seleccionan un expediente concreto. Un `/p` repetido crea otro caso y puede sustituir la autorización pendiente; no hay deduplicación por incidente.

**Efecto:** el sistema anuncia delegación sin crear siempre un caso; cuando se consulta a Pixy, puede volver a un fallo viejo. No existe una condición clara de «incidente ya resuelto» que excluya errores históricos.

**Corrección:** eventos de fallo tipados con task/run/revision; Heart hace diagnóstico idempotente sobre ese sujeto. Usar una clave de incidente y evidencia con vigencia. El caso de infraestructura se separa del fallo funcional de una herramienta.

### A08 · P1 · Hay ventanas de caída que dejan decisiones y casos desincronizados

**Evidencia reproducida:** [Pixy](../app/pixy/pixy.py#L141) registra la aprobación y después llama a Heart, en bases distintas. Simulando caída entre ambos pasos, la autorización quedó `approved` y el caso `WAITING_USER_APPROVAL`. [Recovery.tick](../app/heart/recovery.py#L379) no reconcilia esa aprobación; posteriormente puede expirar.

También quedan sin continuación automática las fases `RECEIVED`, `COLLECTING_EVIDENCE` y `SOLUTION_PROPOSED`, que no pertenecen al conjunto de fases activas. En [Hand.create_recovery](../app/hand/hand.py#L519), una caída después del rename y antes del registro SQLite deja una carpeta no registrada que se rechaza en el siguiente intento.

**Efecto:** «sí, aprobado» puede no iniciar nada tras un reinicio. «Persistido en SQLite» no garantiza por sí solo que el workflow sea reanudable.

**Corrección:** outbox/inbox por dominio, IDs estables y reconciliación de cada frontera; nunca fingir transacción atómica entre bases. Reutilizar el patrón de [ControlStore](../app/pixy/control.py) y ampliar sus garantías a decisiones, Hand y notificaciones.

### A09 · P1 · La biblioteca no se descubre por capacidad y activa versiones antes de verificarlas

**Evidencia:** [Coordinator](../app/mind/coordinator.py#L264) descubre reutilización recorriendo tareas `COMPLETED` con verificación exitosa y **texto de solicitud exactamente igual**. El objetivo puede contener un manifiesto generado por el LLM; cambiar su redacción impide la coincidencia. No se consulta un catálogo funcional de skills parametrizadas.

[Hand](../app/hand/hand.py#L198) sí tiene versiones y archivos, pero [READY](../app/mind/coordinator.py#L347) activa la versión antes de ejecutar y verificar su comportamiento. `active` prueba disponibilidad/integridad, no éxito funcional; una actualización fallida no recupera por sí sola la versión anterior.

**Efecto:** proliferan herramientas casi iguales y quedan desaprovechadas las verificadas. La existencia de versionado no completa un ciclo de actualización segura.

**Corrección:** catálogo de capacidades con contratos de entrada/salida, parámetros, ejemplos, dependencias y última verificación. Separar versión candidata, ejecutable y promovida. Recuperación debe editar una versión candidata y preservar la última válida.

### A10 · P1 · Falta verificación funcional y entrega durable del resultado

**Evidencia:** [verify_result](../app/voice/runtime/contract.py#L68) solo acepta hash de archivo esperado por el usuario. El código distingue correctamente salida cero de éxito; sin embargo, un resultado dinámico como clima no tiene normalmente un hash conocido de antemano y necesita `/verify` manual.

[Coordinator.notify](../app/mind/coordinator.py#L84) absorbe fallos de envío. La tarea pasa a `COMPLETED` antes de asegurar entrega; los casos de Heart se marcan reportados antes de notificar. El [gateway Telegram](../app/voice/gateway/telegram_gateway.py#L52) reintenta transportes, pero no hay outbox con conciliación del resultado de entrega.

**Efecto:** pueden quedar resultados correctos eternamente sin verificar, o completados que nunca se entregaron. También se puede perder precisamente la pregunta que bloquea una tarea.

**Corrección:** verificadores deterministas por contrato, con criterios fijados antes de ejecutar y separados del autor del artefacto. Estado de entrega independiente, outbox y consulta recuperable. Ante resultado de envío incierto, registrar incertidumbre en vez de prometer entrega exactamente una vez.

### A11 · P1 · Una generación lenta bloquea supervisión y los AGENT pierden el gateway

**Evidencia:** [Coordinator.tick](../app/mind/coordinator.py#L167) recorre todas las tareas secuencialmente y ejecuta llamadas LLM, notificaciones, consultas y gateway en ese recorrido. Una llamada de proveedor puede durar 60 segundos y un failover varios intentos. El worker espera [45 segundos](../app/voice/runtime/worker_runtime.py#L44) por Voice.

Además, `UNVERIFIED` pertenece a [TERMINAL](../app/pixy/control.py#L12). Para un AGENT `started`, la rama terminal consulta su recibo pero no procesa el mailbox. Reproducción: **un poll de estado y cero llamadas al gateway**.

**Efecto:** demora de progreso/cancelación, falsos timeouts de red y pérdida del acceso externo de un servicio aún vivo. El proxy sigue supervisando procesos por separado, pero eso no mantiene el servicio de Voice del Core.

**Corrección:** scheduler con trabajos de LLM separados de polling, mailbox, plazos y entrega; concurrencia acotada y ownership por tarea. Separar estado del objetivo, del proceso y de la verificación.

### A12 · P1 · El aprendizaje activo no cumple garantías que prueban componentes heredados

**Evidencia reproducida:** [Dreams](../app/dreams/dreams.py#L18) deduplica por ejecución, pero su métrica cuenta ejecuciones, no objetivos distintos. Tres éxitos del mismo objetivo generaron una propuesta. Tras aprobarla y registrar ocho fallos, la tasa bajó a **3/11 = 27,27 %** y la regla siguió activa.

Las pruebas de tres objetivos y retiro están en [test_control](../tests/test_control.py), pero usan `LearningLedger`; la composición real utiliza Dreams. Asimismo, Mind consulta hipótesis históricas y las ordena por `confidence`, mientras Dreams consolida versiones; falta un contrato que conecte esos dos mecanismos. Las reglas activas se inyectan globalmente en Hand, sin filtrar por aplicabilidad.

**Efecto:** la suite puede pasar garantías de un componente que ya no gobierna producción. Una recomendación deja de ser útil sin que el sistema la suspenda, y un score del modelo se confunde con evidencia empírica en decisiones de reutilización.

**Corrección:** mantener Dreams por detonadores, definir muestras independientes, atribución por versión/contexto, reglas aplicables y suspensión por degradación. Sustituir las pruebas heredadas por contratos contra la composición real. No aumentar autonomía basándose solo en tasa observacional.

### A13 · P1 · La correlación de trazas no coincide con los IDs emitidos

**Evidencia reproducida:** [Voice](../app/voice/voice.py#L203) crea IDs `req_<update_id>-...`; [LOG_TRACE_RE](../app/common/tracing.py#L17) reconoce prefijos `req-`, `trace-`, etc. Un log generado con el formato real produjo `trace_id=None` al parsearlo.

Heart filtra logs por arranque, pero no pasa una traza objetivo a `parse_log_errors`; también interpreta el agregado de tareas recientes. El log central de eventos conserva correlaciones, pero no un campo uniforme de traza/causalidad para todos los órganos.

**Efecto:** los errores pueden mezclarse pese a la promesa de diagnóstico por sesión y solicitud. Un nuevo `boot_time` tampoco equivale a resolución de incidentes anteriores.

**Corrección:** IDs con contrato compartido, logs estructurados y campos separados de trace, task, case, run y causation. Consultar evidencia por sujeto y ventana temporal, no depender de extraer causalidad de texto truncado.

### A14 · P1 · La ruta directa de Pixy evita el ingreso durable y depende del LLM para explicar decisiones

**Evidencia:** [webhook](../app/voice/voice.py#L189) espera `command`, que puede ejecutar [Pixy.diagnose](../app/pixy/pixy.py#L194), consultas a Senses y generación LLM antes de devolver HTTP. Los comandos `/p` y `y/n` no tienen un inbox universal. Las excepciones pueden convertirse en respuesta genérica y HTTP 200.

Pixy crea caso/autorización antes de pedir la explicación al modelo. Si el proveedor falla, la decisión durable puede existir sin que el usuario reciba una propuesta legible. Fuera del Coordinator, estas llamadas no comparten el presupuesto de un objetivo.

**Efecto:** exposición a reentregas, preguntas no entregadas y diagnóstico inaccesible precisamente cuando falla el proveedor. La docstring de ingreso «no model calls here» no coincide con el recorrido efectivo.

**Corrección:** persistir primero todos los mensajes/comandos; responder aceptación y procesar en background. El resumen de plan y su resultado deben tener un render determinista; el LLM puede mejorar explicación sin ser condición para aprobar, rechazar o consultar estado.

### A15 · P2 · La salud operacional aún no valida el servicio ni la recuperación completa

**Evidencia de inspección:** [Recovery.verify](../app/heart/recovery.py#L343) comprueba reinicio y `running`; un contenedor vivo no prueba webhook, scheduler y proveedor funcionales. [VitalSigns](../app/heart/vital_signs.py) mide memoria global y la raíz del volumen, no necesariamente los límites cgroup y montaje de datos relevantes. [CoreEyes](../app/voice/gateway/observation_gateway.py#L53) lee el log completo antes de truncarlo. El proxy no supervisa que su tarea de scheduler siga viva si `engine.tick` lanza una excepción no controlada en [su loop](../docker_proxy/proxy.py).

La documentación ya reconoce ausencia de cuotas y retención. Compose no establece límites del Core/proxy ni healthchecks funcionales. Un Core muerto tampoco puede diagnosticar ni ordenar su propio reinicio: requiere un supervisor fuera de ese proceso.

**Corrección:** readiness por componente, métricas de cgroup/disco de datos, lectura acotada, watchdog externo mínimo, retención con referencias activas y pruebas Linux de caída. La autorreparación de infraestructura debe tener presupuestos de reinicio y escalamiento.

## 5. Recorridos que explican los síntomas

### «Apruebo crear la herramienta y nunca funciona»

Solicitud natural → manifiesto `Network` → `y` → Hand genera HTTP directo → Pixy lo permite → aprobación sensible → contenedor sin red → error → `UNKNOWN` o resultado no verificado si el script oculta el error → nueva consulta a Pixy sin capacidad externa disponible. Cada vuelta conserva la contradicción original.

### «Pixy dice que reparó, pero vuelve a fallar»

Fallo/agotamiento → consulta manual `/p` → Heart elige una tarea fallida reciente → propone reintentar → `y` → borra feedback y renueva contadores → marca caso `RECOVERED` → la tarea todavía debe generar, revisar, ejecutar y verificar. No se aplicó necesariamente una corrección.

### «Dije sí y no ocurrió nada, o ocurrió otra cosa»

Pregunta sin identidad explícita → otra pregunta pendiente/reentrega → resolución global por `y/n`; o aprobación persistida → caída antes del pase a Heart → estado pendiente que no se reconcilia. Son causas distintas que requieren una sola infraestructura durable de decisiones.

## 6. Qué conservar

- Deduplicación de objetivos normales por source y comandos de tarea transaccionales.
- Recibos de ejecución, IDs estables y tratamiento conservador de resultados inciertos.
- Revisión de artefactos y verificación de hashes/bundles antes de ejecución.
- Workers sin secretos, socket Docker ni bases del Core; aislamiento real como frontera, no el AST.
- Historial de versiones, archivos y recuperación de activación de Hand.
- Voice como custodio de credenciales, validación de DNS público, fijación de IP, TLS y rechazo de redirects.
- Dreams por eventos y aprobación humana de las reglas propuestas.

Estas piezas justifican una corrección incremental con pruebas de integración, en vez de una reescritura general.

## 7. Registro de reproducciones locales

Los probes utilizaron `TemporaryDirectory`, SQLite nuevas, `SimpleNamespace` y `Mock`; no cargaron secretos ni ejecutaron Docker. Se invocaron las clases y métodos citados, por lo que estos resultados son independientes de respuestas de un LLM.

| Nº | Preparación / invocación | Resultado observado | Criterio futuro |
| --- | --- | --- | --- |
| R01 | Dos tareas `WAITING_CREATION_APPROVAL`; `direct_response('y', source=S)` dos veces | Ambas `GENERATING` | Solo una decisión; replay devuelve el mismo recibo |
| R02 | Tarea agotada con 4/4/16 consumos y feedback; `retry_task` | 0/0/0, feedback/error nulos; ID anterior conservado | Historial intacto, intento nuevo y presupuesto acumulado |
| R03 | `retry_task` sobre `UNKNOWN` | `GENERATING` | Rechazo hasta reconciliación |
| R04 | `retry_task` sobre `CANCELLED` | `GENERATING` | Rechazo; orden obsoleta no revoca cancelación |
| R05 | Mismo caso/plan; cambiar solo `task_id` | Hash idéntico | Hash/alcance diferentes |
| R06 | Evidencia de tareas fallidas `older`, `newer` | Remedio para `older` | Sujeto explícito del incidente |
| R07 | Guardar aprobación, omitir entrega a Heart, ejecutar `tick` | Autorización `approved`, caso esperando | Reconcilia exactamente una vez |
| R08 | Continuar R07 entregando decisión; avanzar tarea | `RECOVERED`, permiso `approved`, Hand=0, Senses posterior=0 | Hand + consumo + recibo + verificación |
| R09 | ID real `new_trace_id(prefix='req_123')` en log | Parser devuelve traza nula | Correlación preservada |
| R10 | Tres ejecuciones exitosas del mismo objetivo/version en Dreams | Una propuesta | Política de muestras independientes explícita |
| R11 | Aprobar R10; ocho fallos posteriores | Tasa 27,27 %, regla activa | Suspensión según contrato |
| R12 | Grant DockerRestart/core; activar skill Network con igual traza | Skill `active` | Rechazo por alcance/hash |
| R13 | AGENT `UNVERIFIED`, recibo `started`; `tick` | Poll=1, mailbox=0 | Servicio de gateway mientras viva el proceso |
| R14 | RecoveryEngine `retry_task/task`, Docker simulado | Argumentos `restart --time 10 task` | Acción rechazada por ese ejecutor |
| R15 | `validate_plan` con `retry_task/core` | Plan aceptado | Par acción/objetivo inválido |

La suite actual y estos probes se complementan: **94 pruebas verdes no refutan los defectos reproducidos**. Las nuevas regresiones deben afirmar las invariantes correctas y fallar sobre la revisión auditada.

## 8. Referencias externas consultadas y adaptación

Consulta realizada durante la auditoría del 25 de octubre de 2026 en horario de Santiago. Las páginas pueden cambiar; antes de implementar una integración concreta debe fijarse la revisión relevante.

| Fuente oficial | Patrón observado | Aplicación en Khai |
| --- | --- | --- |
| [Hermes: Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) | Descubrimiento por metadatos, carga progresiva, documentación de uso/pitfalls/verificación, skills creadas por el agente | Catálogo de Hand y procedimiento versionado junto a código ejecutable; no depender de igualdad textual de tareas |
| [Hermes: Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) | Separa memoria curada, historial y procedimientos; distingue afirmación de escritura real; ofrece revisión de cambios de skills | Separar hechos, hipótesis y recetas; presentar diff y evidencia; conservar aprobación de reglas de Khai |
| [OpenClaw: Agent loop](https://docs.openclaw.ai/concepts/agent-loop) | Admisión con identidad de run, serialización por sesión, ownership de escritura, continuación acotada sin repetir efectos, recibos de entrega | Inbox durable, revisión esperada, concurrencia acotada y recuperación del mismo objetivo sin reiniciar presupuesto |
| [OpenClaw: Exec approvals](https://docs.openclaw.ai/tools/exec-approvals) | Aprobación ligada al contexto concreto de ejecución y comprobación antes de lanzar; disponibilidad y permisos son conceptos distintos | Vincular autorización a operación/artefacto/alcance; mostrar capacidades efectivamente disponibles |

Una skill de Hermes es principalmente conocimiento/procedimiento cargable; una skill de Khai es hoy un artefacto Python ejecutable. No son intercambiables sin un contrato de empaquetado y ejecución. Tampoco se recomienda copiar permisos amplios o modificar las reglas event-driven de Dreams por analogía con otros productos.

## 9. Decisión recomendada

Priorizar A01–A05 y corregir la semántica de éxito antes de ampliar la autonomía. Después completar un flujo integrado de creación y reparación con capacidades de Voice propuestas por Hand, revisión de Pixy, diagnóstico de Heart y verificación independiente. El [plan detallado](PLAN_AUTONOMIA_Y_AUTORREPARACION.md) convierte esos cambios en entregas, dependencias y criterios de salida.
