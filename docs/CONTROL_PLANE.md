# Control plane: contratos y mapa de consolidación

Estado de esta revisión: contratos y primer corte integrado implementados. El despliegue en Linux/Azure todavía requiere validación separada.

## Flujo de autoridad

```text
órgano → puerto acotado → servicio de control / unidad de trabajo
                              ↓
                    repositorios lógicos
                              ↓
                    data/control.sqlite3
```

Pixy resuelve una decisión y crea la orden exacta para Hand en el mismo commit. Heart conserva el caso y entrega las referencias. Hand reclama orden y autorización atómicamente antes del primer efecto.

## Propiedad lógica

| Datos | Propietario | Consumidores de lectura |
| --- | --- | --- |
| Objetivos, intentos y presupuesto | Coordinator | Mind, Heart, Pixy, Senses |
| Casos, diagnósticos y planes | Heart | Pixy, Hand, Coordinator |
| Decisiones y autorizaciones | Pixy | Heart y Hand mediante puertos diferentes |
| Órdenes, artefactos y versiones | Hand | Heart, Coordinator y Pixy |
| Reglas propuestas/activas | Pixy; Dreams solo propone | Mind, Hand |
| Eventos y outbox | propietario del evento | despachadores y observabilidad |

## Invariantes mínimas

1. Aprobar una decisión y habilitar su orden es atómico.
2. Un ID aislado no concede permisos.
3. Cancelación o revisión diferente invalida una orden obsoleta.
4. Una autorización se consume una vez; el replay del mismo consumidor devuelve el mismo recibo.
5. `UNKNOWN` exige reconciliación y no se reintenta.
6. Extender presupuesto conserva los consumos y la causa anteriores.
7. Ningún órgano recibe SQL arbitrario sobre el control store.
8. Stores externos se coordinan con inbox/outbox y recibos, no mediante una falsa transacción distribuida.

## Captura privada de credenciales

Pixy crea una solicitud durable mediante su puerto y solo persiste metadatos:
usuario, chat privado, tipo, proveedor, variable permitida, propósito,
versión esperada y expiración. Voice intercepta `/us`, `/pas` y `/api` antes
de Mind y del LLM. Solo acepta una solicitud pendiente compatible; una captura
ambigua, vencida o de otra identidad se rechaza sin revelar la causa exacta.

El valor se escribe atómicamente en `data/voice/secrets.env`, nunca en
`control.sqlite3`, eventos, trazas, memoria o respuestas. Las versiones se
marcan con comentarios no sensibles `KHAI_VERSION` para impedir que una
rotación basada en una versión antigua sobrescriba una nueva. La solicitud se
marca como consumida y genera un evento `SecretCaptured` en `secret_outbox`.
Los eventos quedan pendientes hasta que un despachador los confirma, por lo
que una caída entre captura y entrega se recupera sin pedir nuevamente el
secreto.

Telegram puede conservar el mensaje original en su historial; la garantía de
esta frontera es que el valor no cruza hacia los módulos internos de Khai.

## Mapa del estado anterior

| Store anterior | Destino |
| --- | --- |
| `pixy/logs.sqlite3`, `openclaw.db` | objetivos, auditoría y control a `control.sqlite3` |
| `mind/memory.sqlite3` | memoria estructurada a `control.sqlite3` |
| `heart/heart.sqlite3` | casos a `control.sqlite3`; signos crudos pueden permanecer externos |
| `hand/hand.sqlite3` | catálogo, versiones y órdenes a `control.sqlite3`; archivos permanecen en `data/hand/` |
| `dreams/dreams.sqlite3`, `pixy/rules.sqlite3` | observaciones y reglas a `control.sqlite3` |
| `voice/voice.sqlite3` | permanece como frontera de Voice |
| stores de Runtime | permanecen como recibos técnicos externos |
| `senses/senses.sqlite3` | permanece como evidencia cruda de solo lectura |

No existe importador automático de bases antiguas. Encontrar una base legacy después del corte debe considerarse una condición operacional que requiere inspección, no una señal para mezclarla automáticamente.
