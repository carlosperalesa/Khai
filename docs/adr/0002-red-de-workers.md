# ADR-0002: Mantener workers sin red directa

- Estado: aceptado
- Fecha: 2026-10-26

## Decisión

Los workers continúan con red directa deshabilitada. Las operaciones externas pasan por Voice mediante servicios declarativos, políticas verificables y recibos idempotentes.

Una capacidad de red en el manifiesto permite usar el SDK de gateway; no concede Internet general. Los secretos permanecen en Voice y nunca se incorporan al bundle, prompt, historial o entorno del worker.

## Consecuencias

- Hand puede proponer descriptores, pero Pixy autoriza su alcance y Voice registra el transporte.
- Una skill no puede convertir correlación, disponibilidad de una clave o una dependencia instalada en permiso de red.
- Los recibos de frontera se conservan fuera de la transacción del Core y se reconcilian mediante identidades estables.

