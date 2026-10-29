# Bloque 2: autoridad de ejecución y supervisión AGENT

Implementación local del 29 de octubre de 2026. Reutiliza Coordinator, Voice y
SQLite; no añade LangGraph, dependencias, servicios externos ni procesos nuevos.

## Cancelación y peticiones externas

Voice consulta la tarea vigente mediante `VoiceExecutionPort` antes de admitir
cada petición del mailbox. La admisión usa una transacción `BEGIN IMMEDIATE`
en la misma base de control que la cancelación. Comprueba ejecución, revisión del
objetivo, artefacto autorizado, plazo, capacidades y versión de la operación.
El gateway sin este puerto rechaza las peticiones de workers.

La admisión es el punto que ordena la carrera con la cancelación:

- Si la cancelación se registra primero, la petición se deniega.
- Si la admisión se registra primero, la petición queda en curso y conserva su
  recibo, aunque la cancelación llegue durante el transporte.
- Cada petición posterior vuelve a consultar SQLite. Una copia antigua de la
  tarea no permite continuar enviando el resto del mailbox.

Esto no revoca un efecto ya admitido ni garantiza que un proveedor pueda
cancelarlo. Los mensajes distinguen la solicitud de parada de su confirmación.
Un resultado `unknown` conserva la incertidumbre y bloquea nuevas peticiones.

El registro `voice_dispatches`, con migración propia, conserva identidad,
huella, alcance, efecto y resultado de cada admisión. Los recibos técnicos
detallados siguen perteneciendo a Voice. Las dos bases no forman una transacción
distribuida: ante una caída entre admisión y recibo no se repite el envío. Una
escritura admitida sin resolución bloquea nuevas escrituras de esa ejecución;
su reconciliación no se automatiza en este bloque.

El transporte comprueba otra vez la versión activa del servicio antes de abrir
la conexión. Cada archivo del worker incluye ejecución, artefacto y revisión;
los archivos de ejecuciones anteriores se rechazan. Una futura activación deberá
actualizar Core y el worker conjuntamente. No hay compatibilidad permisiva para
peticiones antiguas sin esa identidad.

## Objetivo aceptado y proceso activo

El estado del objetivo y el estado observado del proceso se guardan por separado:

| Hecho | Registro |
| --- | --- |
| El propietario acepta el resultado | `verification` y estado del objetivo |
| El proceso continúa o termina | `runtime_status`, `runtime_result` y fecha de observación |
| Se pide cancelar un servicio ya aceptado | Objetivo `COMPLETED`, proceso `cancelling` |
| El runtime confirma la parada | Estado observado de parada, sin borrar la aceptación |

Un AGENT recibe supervisión y gateway en `UNVERIFIED`, `WAITING_FEEDBACK`,
`VERIFYING` y `COMPLETED` mientras siga activo y dentro del plazo. Se consulta al
runtime antes de atender el mailbox. Una consulta fallida no habilita envíos ni
se interpreta como confirmación de parada. El plazo también se aplica a servicios
con resultado aceptado. `/stop` sin identificador incluye esos servicios.

Las observaciones del runtime no incrementan la revisión de las preguntas
pendientes. Las observaciones antiguas tampoco reactivan una ejecución cancelada.
`started` sigue sin demostrar que el objetivo se haya cumplido. La consulta de
estado informa por separado del objetivo y del servicio.

## Correcciones mientras el AGENT está activo

Una corrección guarda el texto del propietario y solicita la parada del servicio.
Hasta confirmarla conserva el identificador de ejecución y sus recibos. Después
retoma la revisión de acciones en el mismo objetivo mediante el flujo existente,
con sus comprobaciones y aprobaciones. Si la parada queda incierta, conserva la
corrección y no inicia otra ejecución. La transición pendiente persiste en SQLite.

La aceptación consulta los recibos actuales, incluyendo escrituras posteriores
al primer `started`; las escrituras sin confirmar impiden aceptar el resultado.
La aceptación describe la evidencia observada en ese momento, sin prometer que
un servicio vaya a permanecer sano indefinidamente.

## Validación y alcance

Se añadieron 20 pruebas con SQLite real y dobles de transporte/runtime: carreras
de cancelación, cambios de autoridad, identidad antigua, escritura incierta,
caída entre admisión y recibo, versión revocada, atención del AGENT tras aceptar,
plazos, preguntas, correcciones y confirmación de parada.

La suite completa de este corte pasó **232/232 pruebas** con Python 3.12.10 y las
dependencias existentes. `git diff --check` no detectó errores de whitespace.

Siguen pendientes los bloques de recuperación de Heart, consumo de órdenes y
aprendizaje, credenciales, presupuestos de proveedores y entrega incierta de
Telegram. No se hizo commit, push, purga, despliegue ni reinicio remoto. Falta la
validación real en Linux/Docker, Telegram y proveedores tras una activación
autorizada.
