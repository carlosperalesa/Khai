# ADR-0003: Control store común en `data/control.sqlite3`

- Estado: aceptado
- Fecha: 2026-10-26

## Decisión

El estado transaccional del Core se consolida físicamente en `data/control.sqlite3`. La ubicación es neutral: no pertenece a Pixy ni crea un órgano Control.

El store contiene objetivos, intentos, presupuestos, casos, decisiones, autorizaciones, órdenes, catálogo y versiones, reglas, eventos y outbox. Los repositorios mantienen propietarios lógicos aunque compartan archivo.

Permanecen fuera:

- `data/voice/secrets.env`, cuyo valor solo administra la ruta determinista Pixy→Secrets.
- `data/mind/identity.yaml`, que define identidad y no estado operacional.
- Recibos técnicos de Runtime y gateways cuando pertenecen a una frontera externa.
- Evidencia cruda de Senses; el control store conserva referencias y resultados derivados.

Las migraciones usan versiones por componente para impedir colisiones dentro del mismo archivo.

## Migración

Se adopta un corte limpio porque el despliegue anterior no está operativo. No se importan bases legacy. El reset autorizado preserva únicamente Secrets e Identity y elimina el resto de `data/` antes del bootstrap nuevo.

