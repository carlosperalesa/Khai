# ADR-0004: Puertos acotados, repositorios lógicos y recibos

- Estado: aceptado
- Fecha: 2026-10-26

## Decisión

Cada órgano recibe desde bootstrap un puerto con las operaciones que puede solicitar. El origen no se acepta como un string declarable por el llamador. Los repositorios por órgano son internos al servicio de control y una unidad de trabajo coordina mutaciones entre propietarios.

Los IDs son referencias, no bearer tokens. Hand no ejecuta por poseer `authorization_id` u `order_id`: debe presentar la combinación exacta de operación, caso, destinatario, capacidades, target, hash, nonce, vigencia y revisión esperada.

La aprobación de Pixy y la habilitación de la orden de Hand ocurren en una transacción. Hand reclama la orden y consume la autorización en otra transacción atómica e idempotente. Un replay del mismo consumidor recupera el recibo; otro consumidor o términos diferentes se rechazan.

`trace_id` sirve exclusivamente para correlación y nunca concede autoridad.

## Puertos

| Puerto | Escrituras permitidas |
| --- | --- |
| Coordinator | objetivos, intentos, transiciones, presupuestos y comandos del propietario |
| Heart | casos, evidencia referenciada, diagnóstico, plan y verificación de recuperación |
| Pixy | solicitudes, decisiones y autorizaciones |
| Hand | artefactos, versiones, reclamación de órdenes y recibos de ejecución |

Los órganos no reciben conexiones SQLite ni repositorios de otro propietario.

