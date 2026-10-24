# Plan de arquitectura y refactorización de Khai

**Estado:** implementación local en `app/`; validación de despliegue y retirada final de adaptadores pendientes  
**Fecha:** 2026-10-24  
**Alcance:** definir decisiones, estructura y fases de implementación de la nueva arquitectura corporal de Khai.

Este documento es el plan de referencia para la refactorización. La implementación debe hacerse por fases, conservando el funcionamiento actual hasta completar cada migración.

---

## 1. Decisiones arquitectónicas definitivas

### 1.1 Responsabilidad de los órganos

```text
Voice
  Comunica con Telegram, proveedores externos y runtimes.
  Aplica políticas de comunicación.
  No decide el objetivo de Khai.

Mind
  Interpreta la solicitud, define objetivos y coordina la intención.

Identity
  Define quién es Khai, su personalidad y forma de hablar.

Brain
  Usa el LLM para razonar y transformar objetivos en propuestas.
  No tiene acceso directo a Internet.

Memory
  Conserva recuerdos, preferencias, evidencia y conocimiento seleccionado.

Dreams
  Consolida conocimiento y propone aprendizajes.
  No define la personalidad ni modifica reglas directamente.

Heart
  Mantiene la operación interna mediante Beat, VitalSigns y Diagnosis.

Hand
  Crea, edita, versiona, archiva, restaura, elimina y ejecuta el ciclo
  de administración de skills.

Pixy
  Audita, controla límites, presupuestos, permisos y aprobaciones.
  Puede rechazar operaciones y explicar la causa.

Senses
  Observa órganos, skills, runtimes, almacenamiento, red y servicios.
  Opera en modo lectura.

Runtime
  Ejecuta skills aprobadas.
  Es parte de Voice como contrato de comunicación y ejecución.

Docker
  Es solo una implementación opcional de Runtime.
  No contiene lógica de negocio, aprendizaje, autorización ni decisiones.
```

### 1.2 Flujo principal

```text
Entrada externa
    ↓
Voice
    ↓
Mind / Thinking
    ↓
Brain, Memory y Dreams
    ↓
Objetivo propuesto
    ↓
Pixy / Preflight Audit
    ↓
Hand / Building
    ↓
Pixy / Artifact Audit
    ↓
Voice / Runtime
    ↓
Observación de Senses
    ↓
Memory y logs
    ↓
Dreams / consolidación
```

Voice transporta y comunica, pero no decide qué debe hacer Khai. Mind decide el objetivo, Pixy autoriza los límites, Hand construye y Runtime ejecuta.

---

## 2. Estructura objetivo

### 2.1 Código estructural

```text
app/
├── voice/
│   ├── voice.py
│   ├── gateway/
│   │   ├── external_gateway.py
│   │   ├── telegram_gateway.py
│   │   └── asset_gateway.py
│   └── runtime/
│       ├── runtime.py
│       ├── docker_runtime.py
│       └── local_runtime.py
│
├── mind/
│   ├── mind.py
│   ├── identity.py
│   ├── brain.py
│   └── memory.py
│
├── dreams/
├── heart/
│   ├── beat.py
│   ├── vital_signs.py
│   └── diagnosis.py
├── hand/
├── pixy/
│   ├── pixy.py
│   ├── budget.py
│   ├── control.py
│   └── default_rules.yaml
└── senses/
```

`modules/` se migrará gradualmente a `app/`. Durante la transición podrán existir adaptadores de compatibilidad, pero el objetivo final no es mantener dos arquitecturas permanentes.

### 2.2 Datos variables

```text
data/
├── logs.sqlite3
│
├── voice/
│   └── secrets.env
│
├── mind/
│   ├── identity.yaml
│   └── memory.sqlite3
│
├── dreams/
│   └── dreams.sqlite3
│
├── heart/
│   └── heart.sqlite3
│
├── hand/
│   ├── hand.sqlite3
│   ├── get-weather/
│   │   ├── app/
│   │   ├── data/
│   │   └── other/
│   ├── get-weather001.zip
│   └── get-weather002.zip
│
├── pixy/
│   ├── rules.sqlite3
│   └── logs.sqlite3
│
└── senses/
    └── senses.sqlite3
```

### 2.3 Recursos publicados

Los recursos que Khai debe entregar al usuario no deben vivir dentro del `other/` privado de una skill. Se creará una carpeta pública de recursos en la raíz:

```text
other/
├── gallery/
│   └── <asset-id>/
├── exports/
└── quarantine/
```

Esta carpeta es un espacio de archivos, no una fuente de lógica ni configuración.

- `gallery/`: archivos aprobados y disponibles para el usuario.
- `exports/`: archivos preparados para una entrega concreta.
- `quarantine/`: archivos descargados o generados que todavía no han sido aprobados para exposición.

Los `other/` internos de una skill quedan reservados para recursos privados de esa skill:

```text
data/hand/get-video/
└── other/
    ├── plantillas/
    └── recursos-internos/
```

La publicación externa será responsabilidad de `Voice / AssetGateway`, no de Pixy. Pixy autoriza o rechaza la publicación y sus límites; Voice expone el recurso mediante Telegram, web u otro canal futuro. Así se conserva la separación:

```text
Skill/Hand produce el archivo
Pixy autoriza su exposición
Voice publica el archivo
```

Una skill nunca debe publicar directamente en Internet.

---

## 3. Decisión sobre las bases de datos SQLite

### 3.1 ¿Una sola base grande o varias bases?

Una sola base sería más sencilla para consultas globales y permitiría transacciones cruzadas, pero no es automáticamente más rápida ni más eficiente. También produciría:

- Mayor acoplamiento entre módulos.
- Más contención de escritura.
- Un único punto de corrupción o bloqueo.
- Backups y restauraciones menos selectivos.
- Dificultad para separar permisos de Pixy, Senses, Hand y Memory.
- Mayor riesgo de que un módulo modifique datos que no le pertenecen.

Muchas bases aisladas también serían un error si se crean sin una frontera clara. Por eso se adopta una solución híbrida:

```text
data/logs.sqlite3
  Base central para eventos y correlación transversal.

Bases de dominio
  Solo conservan datos que requieren propiedad, esquema o ciclo de vida propio.
```

No se utilizará una base distinta para cada tabla o para cada pequeño componente. La separación se justifica únicamente por dominio.

### 3.2 Propiedad de cada base

| Base | Propietario | Contenido |
|---|---|---|
| `data/logs.sqlite3` | sistema de eventos | Eventos resumidos de todos los módulos y correlación |
| `data/mind/memory.sqlite3` | Memory | Recuerdos, preferencias, evidencia y conocimiento |
| `data/hand/hand.sqlite3` | Hand | Skills, versiones y todas las operaciones de Hand |
| `data/pixy/rules.sqlite3` | Pixy | Reglas modificables y su ciclo de vida |
| `data/pixy/logs.sqlite3` | Pixy | Auditorías, aprobaciones, rechazos, presupuestos y decisiones |
| `data/senses/senses.sqlite3` | Senses | Observaciones, incidentes y snapshots |
| `data/heart/heart.sqlite3` | Heart | Estado operativo específico de Heart |
| `data/dreams/dreams.sqlite3` | Dreams | Consolidaciones, propuestas y resultados de aprendizaje |

Cada registro que cruce módulos usará identificadores comunes, como:

```text
correlation_id
task_id
execution_id
skill_name
version_id
```

Las bases de dominio no duplicarán el contenido completo del log central. Guardarán sus datos propios y referencias a los eventos relacionados.

### 3.3 Configuración SQLite común

Todas las bases deberán abrirse mediante un helper común que configure:

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;
```

Se añadirán migraciones versionadas por base y verificaciones de integridad. Las operaciones que afecten una sola base serán atómicas. Las operaciones que involucren varias bases usarán estados y eventos correlacionados, no falsas transacciones distribuidas.

---

## 4. Voice y Runtime

### 4.1 Contrato de Runtime

`app/voice/runtime/runtime.py` contendrá el contrato abstracto, no código específico de Docker.

El contrato debe cubrir como mínimo:

```text
prepare
execute
cancel
status
collect_result
cleanup
```

Implementaciones:

```text
docker_runtime.py
  Usa Docker para aislamiento y ejecución temporal.

local_runtime.py
  Permite ejecutar sin Docker en un entorno controlado.
```

Docker podrá seguir creando workers temporales, pero esos workers serán un detalle de `DockerRuntime`, no una entidad arquitectónica de Khai.

### 4.2 Acceso externo

Brain, Hand y las skills no tendrán Internet directo.

Todas las solicitudes externas seguirán este flujo:

```text
Brain o skill
    ↓
Voice Gateway
    ↓
validación de política
    ↓
proveedor externo
```

Voice validará:

- Servicio y operación permitidos.
- Dominio y método HTTP.
- Timeout.
- Tamaño de respuesta.
- Tipo de contenido.
- Límites de frecuencia.
- Sanitización de errores y respuestas.
- Credenciales aplicables.

Las skills no recibirán claves de API.

### 4.3 Secretos

Los secretos modificables de proveedores se almacenarán en:

```text
data/voice/secrets.env
```

Reglas:

- Nunca se versionará en Git.
- Nunca se copiará a una skill.
- Nunca se expondrá mediante `AssetGateway`.
- No se incluirá en ZIP de skills.
- Voice será el único módulo autorizado a leerlo.
- Los logs nunca registrarán sus valores.
- Se mantendrá un ejemplo sin secretos para despliegue.

---

## 5. Modelo de Hand y versionado de skills

### 5.1 Estructura activa

Una skill activa tendrá exactamente:

```text
data/hand/<skill_name>/
├── app/
├── data/
└── other/
```

Responsabilidades:

```text
app/
  Código ejecutable de la skill.

data/
  Configuración y estado variable propio de la skill.

other/
  Recursos auxiliares privados: imágenes, plantillas, archivos descargados
  o datos necesarios para funcionar.
```

Los logs, auditorías, métricas, reglas e historial no vivirán dentro de estas carpetas.

### 5.2 Archivo de versiones

Las versiones anteriores se almacenarán directamente en `data/hand/`:

```text
data/hand/
├── hand.sqlite3
├── get-weather/
├── get-weather001.zip
└── get-weather002.zip
```

Los ZIP:

- Contendrán la carpeta completa `get-weather/`.
- No contendrán logs centrales ni secretos.
- Serán inmutables después de verificarse.
- Tendrán numeración explícita de tres dígitos.
- Se registrarán como `archived` en `hand.sqlite3`.
- Incluirán hash, tamaño y fecha de creación en el registro.

La numeración visible seguirá el formato:

```text
get-weather001.zip
get-weather002.zip
```

El nombre no será la única identidad. La identidad real será la combinación de `skill_name`, `version_id` y hash.

### 5.3 Operaciones de Hand

Todas estas operaciones pertenecen a Hand y deben registrarse en `data/hand/hand.sqlite3`:

```text
create
edit
update
approve
reject
use
archive
restore
rollback
delete
publish
unpublish
```

La tabla `versions` conservará el inventario de versiones. Una tabla `operations` conservará el historial inmutable de operaciones.

Campos mínimos de `versions`:

```text
version_id
skill_name
version_number
status
artifact_hash
archive_name
archive_hash
archive_size
created_at
activated_at
archived_at
previous_version_id
pixy_audit_id
```

Campos mínimos de `operations`:

```text
operation_id
operation_type
skill_name
source_version_id
target_version_id
status
correlation_id
reason
created_at
completed_at
error
```

### 5.4 Estados y publicación de una skill

La creación utilizará una carpeta oculta temporal con el nombre final:

```text
data/hand/.get-weather/
```

Secuencia:

```text
1. Hand registra la versión como draft.
2. Hand crea .get-weather/.
3. Hand escribe app/, data/ y other/.
4. Pixy audita el contenido.
5. Se ejecutan validaciones.
6. Se registra el resultado de la auditoría.
7. Si todo es correcto, el estado pasa a approved.
8. Se renombra .get-weather/ a get-weather/.
9. El renombrado se valida.
10. La versión pasa a active.
```

No se utilizará una carpeta `.staging` separada ni se moverán archivos individualmente.

El renombrado debe hacerse dentro del mismo directorio y en el mismo sistema de archivos para aprovechar una operación atómica. Antes de renombrar se verificará que no exista una skill activa con el mismo nombre. Si el sistema operativo o el runtime no garantizan estas condiciones, la operación se detendrá y no se eliminará ningún dato.

### 5.5 Actualización y archivo

Para actualizar una skill activa:

```text
1. Hand registra la operación update.
2. Pixy aprueba la modificación.
3. Se crea y verifica un ZIP de la carpeta activa.
4. El ZIP se registra como archived.
5. Se valida el contenido nuevo dentro de .<skill_name>/.
6. Se elimina la carpeta activa anterior solo después de verificar el ZIP y
   tener el registro persistido.
7. Se renombra la nueva carpeta a <skill_name>/.
8. Se marca la nueva versión como active.
```

La eliminación de la carpeta activa nunca ocurrirá antes de:

```text
ZIP completo
ZIP legible
ZIP estructuralmente válido
hash calculado
registro de archivo persistido
```

### 5.6 Restauración y rollback

Restaurar una versión no reescribe la historia. Si la versión activa es `v2` y se restaura `v1`, la operación generará una nueva identidad interna, por ejemplo:

```text
v2 → rollback a v1 → v2.1
```

El nombre del ZIP original no cambiará. La nueva versión activa conservará:

```text
previous_version_id
restored_from_version_id
operation_type = rollback
```

La versión restaurada deberá volver a pasar por auditoría de Pixy antes de activarse.

---

## 6. Logs, métricas y aprendizaje

### 6.1 Log central

`data/logs.sqlite3` conservará eventos resumidos y consultables de todo Khai:

```text
event_id
created_at
module
event_type
level
correlation_id
task_id
execution_id
skill_name
version_id
message
metadata
```

Los resultados de una skill estarán vinculados a la versión exacta que los produjo. Nunca se calculará el rendimiento de una versión mezclando eventos de otra.

### 6.2 Separación de métricas

Toda ejecución debe incluir:

```text
skill_name
version_id
artifact_hash
execution_id
outcome
duration
resource_usage
```

Senses podrá resumir métricas y Dreams podrá aprender de ellas, pero:

- La versión activa se evalúa con sus propios resultados.
- Las versiones archivadas permanecen disponibles para comparación histórica.
- Un rollback genera una nueva versión lógica.
- No se borran resultados anteriores.

### 6.3 Docker

Docker puede producir stdout, stderr y estado técnico de sus contenedores, pero no será dueño de:

- Auditorías.
- Aprendizaje.
- Efectividad.
- Presupuestos.
- Decisiones.
- Versiones.

Voice/Runtime transformará los resultados técnicos en eventos de Khai y los asociará con `execution_id`, `skill_name` y `version_id`.

---

## 7. Pixy y reglas

### 7.1 Reglas inmutables

```text
app/pixy/default_rules.yaml
```

Contendrá límites que no pueden relajarse mediante una operación normal:

- Prohibiciones de seguridad.
- Acceso directo a Internet de skills.
- Acceso a secretos.
- Integridad de versiones.
- Requisitos de aprobación.
- Límites estructurales del sistema.

### 7.2 Reglas modificables

```text
data/pixy/rules.sqlite3
```

Registrará:

```text
rule_id
rule_type
content
source
version
status
created_at
approved_by
approved_at
replaced_rule_id
rollback_of
content_hash
```

Una regla modificable requerirá:

```text
propuesta
validación de Pixy
aprobación explícita del usuario
persistencia de la versión
posibilidad de rollback
```

Nunca podrá reducir las garantías de una regla inmutable.

---

## 8. Galería y acceso externo a archivos

### 8.1 Decisión

Se adopta una galería común en:

```text
other/gallery/
```

No se expondrán directamente carpetas internas de skills.

### 8.2 Flujo de un archivo descargado

Ejemplo: el usuario pide rescatar un vídeo.

```text
1. Mind interpreta la solicitud.
2. Pixy valida la operación, origen, tamaño y destino.
3. Hand crea o utiliza la skill aprobada.
4. Voice descarga el recurso mediante su gateway.
5. El archivo se guarda inicialmente en other/quarantine/.
6. Se verifican tipo, tamaño, hash y resultado.
7. Voice mueve o registra el archivo en other/gallery/<asset-id>/.
8. AssetGateway lo entrega por Telegram u otro canal.
9. El evento se registra en logs.sqlite3.
```

Pixy controla la autorización y Voice controla el transporte y la publicación. La galería será independiente del canal actual, por lo que Telegram será solo una implementación de entrega.

### 8.3 Seguridad de la galería

El AssetGateway deberá:

- No permitir traversal de rutas.
- Servir únicamente IDs registrados.
- No aceptar rutas arbitrarias del usuario.
- Validar tamaño y tipo MIME.
- Aplicar expiración si el recurso es temporal.
- Registrar quién solicitó y descargó el recurso.
- Evitar exponer `data/`, `app/`, secretos o bases SQLite.

---

## 9. Senses y observabilidad

Senses conservará observaciones resumidas en:

```text
data/senses/senses.sqlite3
```

Observadores iniciales:

```text
listen_voice
listen_mind
listen_brain
listen_memory
listen_dreams
listen_heart
listen_hand
listen_pixy
listen_runtime
listen_docker
listen_network
listen_storage
listen_database
listen_webhook
listen_security
```

`listen_network` revisará puertos, DNS, TLS, timeouts, conexiones autorizadas y errores de comunicación mediante interfaces limitadas. Senses no usará shell arbitrario ni tendrá acceso directo al socket Docker.

---

## 10. Migración desde la arquitectura actual

La migración será incremental:

### Fase 0: protección

- Respaldar datos actuales.
- Confirmar que `secrets.env` y la operación actual siguen funcionando.
- Añadir pruebas de regresión del flujo Telegram/LLM.
- No cambiar todavía los nombres públicos del despliegue.

### Fase 1: contratos

- Crear contratos de Voice, Runtime, Gateway, Hand y Pixy.
- Crear identificadores de correlación.
- Crear helper SQLite común.
- Mantener adaptadores para los módulos actuales.

### Fase 2: base de datos y logs

- Crear `data/logs.sqlite3`.
- Crear migraciones de bases de dominio.
- Activar WAL, claves foráneas y `busy_timeout`.
- Registrar eventos sin duplicar el contenido completo.

### Fase 3: Voice

- Migrar `orchestrator.py` hacia `app/voice/voice.py`.
- Extraer gateways externos.
- Centralizar acceso a secretos en `data/voice/secrets.env`.
- Implementar el contrato de Runtime.

### Fase 4: Runtime reemplazable

- Convertir el proxy actual en `DockerRuntime`.
- Mantener workers temporales como detalle interno.
- Crear `LocalRuntime` para pruebas y despliegues sin Docker.
- Eliminar dependencias de negocio desde Docker.

### Fase 5: Mind y Brain

- Separar Identity, Brain y Memory.
- Mover la identidad a `data/mind/identity.yaml`.
- Mantener el proveedor LLM detrás de Voice.
- Migrar historial, preferencias y evidencia a Memory.

### Fase 6: Pixy

- Integrar presupuesto, control, auditoría y decisiones.
- Separar reglas inmutables y modificables.
- Implementar aprobaciones, rechazos y rollback.

### Fase 7: Hand

- Separar creación, validación y versionado de skills.
- Implementar `draft` en `.skill-name/`.
- Implementar ZIP verificado e inmutable.
- Implementar `versions` y `operations` en `hand.sqlite3`.
- Implementar rollback con nueva versión lógica.

### Fase 8: galería

- Crear `other/gallery`, `other/exports` y `other/quarantine`.
- Implementar AssetGateway.
- Añadir validación Pixy para publicación.
- Añadir entrega desacoplada de Telegram.

### Fase 9: Heart, Dreams y Senses

- Separar Beat, VitalSigns y Diagnosis.
- Migrar consolidación a Dreams.
- Implementar observadores de Senses.
- Añadir `listen_network`.

### Fase 10: limpieza y documentación

- Migrar imports restantes de `modules/` a `app/`.
- Actualizar Dockerfiles, Compose y documentación.
- Retirar compatibilidad solo después de validar producción.
- Verificar despliegue en Docker y ejecución con LocalRuntime.

---

## 11. Validación obligatoria

Cada fase deberá conservar:

```text
pruebas unitarias
pruebas de integración
pruebas de migración
pruebas de rollback
pruebas de permisos
pruebas de no exposición de secretos
```

Casos críticos:

1. Crear una skill y dejarla en `draft`.
2. Rechazarla sin publicar la carpeta.
3. Aprobarla y activar mediante renombrado.
4. Actualizarla y verificar el ZIP antes de retirar la versión anterior.
5. Detectar un ZIP inválido y detener la operación.
6. Eliminar una skill sin perder sus ZIP archivados.
7. Restaurar una versión y crear `v2.1`.
8. Ejecutar dos versiones y no mezclar métricas.
9. Intentar que una skill use Internet directamente y rechazarlo.
10. Descargar un archivo a `quarantine` y publicarlo solo tras autorización.
11. Intentar acceder a secretos o bases desde un runtime y rechazarlo.
12. Ejecutar la misma skill con DockerRuntime y LocalRuntime.
13. Verificar que Telegram pueda reemplazarse por otro gateway.
14. Verificar integridad y recuperación de cada SQLite.

---

## 12. Resultado esperado

Al finalizar:

```text
Mind piensa.
Brain razona.
Memory recuerda.
Dreams consolida.
Pixy limita y autoriza.
Hand construye y versiona.
Voice comunica y ejecuta mediante Runtime.
Senses observa.
Heart mantiene vivo el sistema.
Docker solo encapsula una ejecución cuando se elige DockerRuntime.
```

Khai podrá cambiar Docker por otro runtime sin perder su lógica, sus reglas, sus skills, su memoria ni su identidad. Las skills no tendrán Internet directo, los secretos permanecerán bajo control de Voice y los recursos destinados al usuario estarán desacoplados de la estructura interna de cada skill.
