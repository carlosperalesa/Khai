# Revisión de interacciones del 30 de octubre de 2026

## Evidencia remota

La inspección SSH fue de solo lectura. Azure ejecutaba `0708f12`; `khai_core` y
`khai_docker_proxy` estaban activos y HTTPS 8443 respondía 200. Las dos claves
Gemini respondieron 200 y una generación mínima funcionó. Dolphin no respondió
en su API privada. De 13 tareas persistidas, siete estaban `FAILED` y una
`EXHAUSTED`; los fallos recientes incluían rechazo del proveedor presentado
como `AllLLMsBusy`, timeouts de Dolphin y una aprobación vencida.

## Correcciones locales

- Voice elimina el sufijo opcional `@botname` antes de clasificar o despachar
  comandos; `/health@Khaiiabot` conserva la ruta urgente de `/health`.
- Las operaciones numeradas de conversaciones se ordenan por inserción durable,
  con `rowid` como desempate cuando Windows entrega timestamps iguales. Cambiar
  de chat ya no renumera la lista.
- Coordinator aplica el FIFO por el orden durable devuelto por ControlStore, no
  mediante una comparación estricta de timestamps que podían empatar.
- El cooldown excluye temporalmente proveedores fallidos en vez de reintentarlos
  en cada turno.
- Los rechazos terminales de contenido se distinguen de indisponibilidad. Cuando
  todos los proveedores disponibles rechazan la solicitud, no se arranca
  Dolphin y se informa que no se ejecutó ninguna acción.
- Memory usa fechas UTC conscientes de zona horaria y elimina la advertencia de
  `datetime.utcnow()`.

## Validación y límites

Pasaron 60/60 pruebas dirigidas de incidentes, conversación, multichat y piloto
Dolphin. `git diff --check` no encontró errores. No se ejecutó la suite completa,
no se hizo commit, push, despliegue, reconstrucción ni reinicio remoto. La VM
continúa en la revisión anterior hasta la validación de cierre y una autorización
separada de despliegue.
