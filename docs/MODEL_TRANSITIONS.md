# Continuidad de inferencia y transiciones cancelables

Estado: implementación local autorizada el 31 de octubre; validación y aceptación real en curso. El resultado desplegado se registra en CHECKPOINT.

`TransitionManager` es el propietario de las mutaciones de VM. `/mode <perfil>` crea una solicitud durable y devuelve su ID inmediatamente. `/mode cancel` y `/stop` invalidan la generación sin esperar la llamada GCP. El SQLite de control conserva generación, modo efectivo, solicitudes y operaciones remotas; la base de uso de Voice es solo un espejo de compatibilidad. La inferencia nunca inicia una VM ni recorre una cascada de perfiles: usa el efectivo o intenta Nano, anuncia un fallback confirmado y pide elegir perfil si Nano falla.

Antes de reemplazar una VM existente, una inferencia JSON real confirma Nano y se publica el puente. Una elección explícita de perfil local puede arrancar sin Nano cuando GCP confirma que no existe VM o está TERMINATED; no hay servicio sano que retirar. La ausencia de un recibo de mutación GCP queda incierta y bloquea nuevas mutaciones hasta reconciliación manual. Un nombre de operación pendiente se consulta por GET antes de otro cambio: nunca se repite el POST incierto. Cancelar no borra por nombre ni deshace llamadas ya enviadas. Tras reiniciar, las transiciones activas pasan a unknown y no se reejecutan.

Mind separa instrucción de sistema, contexto de referencia y MENSAJE_ACTUAL. Ollama recibe el esquema Pydantic AssistantTurn como objeto `format`, temperatura cero y contexto 8192; se conserva validación local y evidencia de rechazo. No se envuelve automáticamente un JSON mal formado ni se confunde el esquema correcto con exactitud semántica del objetivo.

La precarga sigue limitada a 90 segundos. Sus duraciones y contadores Ollama se guardan desde la misma respuesta; no se hace otra generación para medir. Monitoring permanece deshabilitado. Pruebas concurrentes usan barreras reales entre hilos para comprobar cancelación, peticiones sucesivas, respuestas tardías, reinicio y resultados remotos inciertos. No equivalen a haber cargado una GPU real.

Brain conserva los perfiles `high > mid > low > nano` y Voice conserva credenciales y transporte. Cuando dos perfiles Spot necesitan la misma VM o el mismo disco, Brain utiliza nano como puente: anuncia el cambio, confirma que nano puede atender solicitudes, conmuta el tráfico a nano y solo entonces reemplaza la VM. Si nano no responde, no se destruye el servicio actual automáticamente. Una vez preparado el perfil destino, una inferencia mínima confirma su disponibilidad antes de publicar el nuevo modo. Las solicitudes en curso terminan en su proveedor o registran su resultado incierto; no se repiten efectos externos sin recibos.

Política acordada: conservar el modelo efectivo mientras esté disponible; cuando no pueda mantenerse, recurrir a nano como puente. Si nano falla, informar al usuario y pedirle que elija el perfil al que cambiar, sin cambiar automáticamente a un proveedor distinto. Esta política reemplaza la cascada automática en la composición de producción. Si ningún modelo responde, Brain mantiene accesibles los comandos deterministas y notificaciones cuando el transporte esté disponible, sin prometer respuesta generativa.

Cada transición tiene ID durable, generación monotónica, modo anterior, solicitado y efectivo, etapas, tiempos y resultado. Un cambio posterior invalida el anterior; `/stop` y `/mode` deben poder cancelar sin esperar la cola de trabajo. Las llamadas GCP ya enviadas pueden continuar en el proveedor: cancelar significa impedir la activación obsoleta, observar el resultado y reconciliar recursos. La limpieza solo afecta recursos identificados como pertenecientes a esa transición; una operación sin resultado confirmado queda incierta para reconciliar, no se repite a ciegas.

El cambio de modo y la asignación de generación deben ser atómicos. El trabajo bloqueante de GCP se ejecuta fuera de la cola de mensajes; las notificaciones se deduplican por transición y estado, y no se publica un modo conectado sin prueba. Reintentos acotados por etapa distinguen capacidad temporal, tiempo agotado, credenciales, configuración y error de solicitud. Tras reinicio, reconciliar estado persistido y operación remota antes de liberar o activar recursos.

La respuesta debe preservar sesión, historial, llamadas a herramientas y recibos sin suponer compatibilidad completa de formatos entre modelos. No migrar secretos al contexto. Cada proveedor y cadena de respaldo requieren autorización existente y límites de costo; no introducir proveedores nuevos de forma silenciosa. Las respuestas parciales y las escrituras externas no se reejecutan automáticamente. La continuidad depende de que nano esté disponible y tenga cuota.

## Referencias y decisiones

- [OpenClaw: model failover](https://docs.openclaw.ai/model-failover): reintentos acotados, clasificación de fallos, continuidad del historial y cancelación.
- [Hermes: fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers): pares proveedor/modelo configurados, respaldo por turno y restauración del principal.
- [OpenRouter: failover](https://openrouter.ai/blog/insights/reliability-failover/): separación entre respaldo de proveedor y de modelo; el gateway también puede fallar.
- [OpenRouter: enrutamiento](https://openrouter.ai/blog/announcements/how-we-rebuilt-provider-routing/): métricas de latencia, errores y costo, y afinidad de sesión.

## Pruebas de aceptación propuestas

Con nano saludable, simular arranque lento, error y cancelación durante cada etapa y verificar que el chat continúa y solo el candidato vigente puede activarse. Con nano no disponible, confirmar que el servicio anterior no se elimina. Probar reinicios durante operación GCP y resultado incierto, `/mode` consecutivos, una sola notificación por estado y aislamiento de efectos ya ejecutados. Finalmente validar en Azure y GCP con cuotas y costos autorizados.
