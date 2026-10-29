# Revisión de interacciones del 29 de octubre de 2026

Este documento describe el corte anterior de 190 pruebas. La implementación
posterior del bloque 1 y su validación de 212/212 están en
[BLOQUE1_CONVERSACION_PERSISTENTE.md](BLOQUE1_CONVERSACION_PERSISTENTE.md).
El diagnóstico inicial de doble clasificación fue sustituido por un solo router
conversacional y una planificación posterior del objetivo ya resuelto.

## Evidencia remota

Lectura SSH de Azure, sin modificar el servicio: checkout `/opt/khai` en
`6a47f3d`; `khai_core` y `khai_docker_proxy` en ejecución. Las horas siguientes
corresponden a America/Santiago.

- 08:16: Khai ofreció obtener el clima; «Sí» no encontró aprobación pendiente.
- «Hazlo» pasó de `ROUTING` a `COMPLETED`, sin generación ni ejecución.
- La memoria guardó la promesa de consultar el pronóstico como conversación.
- Pixy/Heart propusieron reintentar una tarea anterior cuyo objetivo era «hola».
- A las 08:22 el log registró un ReadTimeout de Dolphin antes del respaldo.

## Causas y corrección local

El planificador dependía de que Mind emitiera `ACTION_REQUIRED`. Una clasificación
como `GENERAL_CHAT` o `STATE_MACHINE_INPUT`, o una acción desconocida no validada,
podía terminar como conversación completada. Las promesas del modelo se
persistían antes de decidir si la tarea requería una skill.

- Las acciones del router se validan contra valores permitidos.
- El planificador comprueba también charla y respuestas cortas, con historial y
  estado durable de tareas. Una propuesta inválida falla explícitamente.
- Solo se conserva como respuesta de Khai la charla que efectivamente se entrega;
  las respuestas preliminares de acción no se incorporan como promesas al historial.
- Una skill nueva sigue requiriendo aprobación de configuración; se conservan
  confirmación de entradas, revisión de Pixy, recibos y verificación del propietario.
- Un saludo no selecciona una skill por sí mismo. Una respuesta corta sin
  pregunta pendiente ni plan válido informa que no tiene una decisión vinculada.
- La solicitud de aprobación usa el estado actualizado, incluyendo descripción
  de la nueva skill y proveedor, también cuando el mensaje actual es «Hazlo».

Heart buscaba fallos históricos entre las tareas recientes sin fijar el sujeto
de la consulta. Ahora una consulta ordinaria se centra en la tarea más reciente
por creación; un ID explícito limita la selección a ese ID. «Último error»
selecciona expresamente un fallo pendiente, excluyendo tareas ya recuperadas.
Los logs de una tarea se filtran por su traza antes del análisis, y Pixy recibe
su estado y evidencia de ejecución. `COMPLETED` sin ejecución no acredita que
se haya ejecutado una skill.

## Ayuda de Telegram

`/?` y `/help` muestran los comandos completos `/mode low`, `/mode mid`,
`/mode high` y `/mode ?`. El transporte añade botones con esos comandos como
`callback_data`, conforme a la [API de Telegram](https://core.telegram.org/bots/api#inlinekeyboardbutton).
Los callbacks conservan autenticación del propietario, persistencia y
deduplicación del inbox. El webhook confirma el callback después de registrar
su entrada. La ayuda sigue pasando por el outbox durable.

## Validación y límites

Suite completa: **190/190 pruebas**, Python 3.12.10. Se usó el intérprete Python
3.12 disponible con las dependencias existentes en `.venv/Lib/site-packages`:
el lanzador de `.venv` conserva una ruta de Python de otro equipo.

Las regresiones cubren una petición mal clasificada que ejecuta una receta y
conserva recibos, «Hazlo» con contexto, clima sin skill que pide aprobación,
saludos, planes y acciones inválidos, diagnóstico sin reintentar otro objetivo,
selección explícita del último error y callbacks de modo autenticados sin repetir
la operación. Se actualizó una expectativa antigua que todavía exigía un ID
visible en una respuesta; también fallaba con el Coordinator original.

No se probó una consulta meteorológica real ni se modificaron credenciales,
infraestructura, datos o tareas remotas. Los timeouts/disponibilidad de Dolphin
son evidencia separada: este cambio no afirma repararlos. La verificación
adicional del planificador puede añadir una llamada LLM a la charla general.

No hubo commit, push, purga ni despliegue. La activación requiere levantar
explícitamente el congelamiento descrito en `.agents/GEMINI.md`, respaldar el
estado y validar Docker/Telegram tras reiniciar Core y proxy. La web existente
debe permanecer operativa.
