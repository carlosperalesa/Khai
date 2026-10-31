# Diseño propuesto: continuidad de inferencia

Estado: propuesta, no implementada ni validada en producción.

Brain conserva los perfiles `high > mid > low > nano` y Voice conserva credenciales y transporte. Cuando dos perfiles Spot necesitan la misma VM o el mismo disco, Brain utiliza nano como puente: anuncia el cambio, confirma que nano puede atender solicitudes, conmuta el tráfico a nano y solo entonces reemplaza la VM. Si nano no responde, no se destruye el servicio actual automáticamente. Una vez preparado el perfil destino, una inferencia mínima confirma su disponibilidad antes de publicar el nuevo modo. Las solicitudes en curso terminan en su proveedor o registran su resultado incierto; no se repiten efectos externos sin recibos.

Política acordada: conservar el modelo efectivo mientras esté disponible; cuando no pueda mantenerse, recurrir a nano como puente. Si nano falla, informar al usuario y pedirle que elija el perfil al que cambiar, sin cambiar automáticamente a un proveedor distinto. Esta decisión se aplica al diseño de transición y requiere coordinarla con la cascada de inferencia existente antes de implementar. Si ningún modelo responde, Brain mantiene accesibles los comandos deterministas y notificaciones cuando el transporte esté disponible, sin prometer respuesta generativa.

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
