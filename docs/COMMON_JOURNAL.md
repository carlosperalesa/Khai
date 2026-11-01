# Diario común de interacciones

El propietario aprobó este diseño después de la auditoría de siete caminos. Se implementa sobre el SQLite de control, sin un servicio externo. Los recibos específicos de cada órgano se conservan.

## Contrato

`app/kernel/journal.py` mantiene:

- `journal_cycles`: identidad propia, tipo, objeto de dominio, raíz, padre, traza, fuente, estado y fechas.
- `journal_events`: secuencia global, ID idempotente, ciclo, etapa, estado y datos técnicos acotados. No admite actualizar ni borrar eventos.
- `journal_links`: enlaces inmutables entre interacciones independientes, preguntas, decisiones e informes.

Cada ciclo tiene un `cycle_id` interno estable por tipo/objeto. IDs externos del proveedor son atributos y no claves del historial de herramientas. Cada intento/revisión de una tarea tiene una rama distinta. Una recuperación autorizada deja un evento explícito antes de continuar el objetivo; no sobrescribe el intento fallido.

Estados: `active`, `waiting`, `completed`, `failed`, `rejected`, `cancelled`, `unknown`, `exhausted`. El cierre de recepción no acredita ejecución ni entrega. Cada rama conserva su propio resultado; un padre cerrado puede tener hijos todavía activos o esperando una decisión. Heart solo marca una reparación completada cuando llega a RECOVERED; un diagnóstico sin reparación no se presenta como reparación exitosa.

Los datos del diario admiten únicamente campos técnicos de una lista cerrada: IDs, modelo/proveedor/perfil, revisión/intento, etapa, estado HTTP, clase de error, hashes, tiempos y acuses. No incluye texto de mensajes, prompts, código, parámetros, cuerpos HTTP, claves, tokens ni términos de autorización como el nonce. Los rechazos acotados/redactados de modelos siguen en el almacén privado de Voice; el diario guarda su referencia y hash.

## Los siete caminos

| Camino | Ramas y cierre |
|---|---|
| Cambio de modelo | Interacción → cambio → candidato/operación remota. Nano, parada, rollback y fallos quedan registrados; se conserva el modo efectivo. |
| Chat | Interacción → tarea/intento → enrutamiento → llamada de modelo/validación. Entrega con ID propio y acuses por fragmento. |
| Creación de skill | Tarea/intento → creación → Hand/agente → herramientas → revisión/operación de biblioteca. Rechazos y excepciones tienen resultado explícito. |
| Pixy | Solicitud/revisión → autorización → orden. Decisiones, expiración y consumo quedan enlazados; un nuevo mensaje de aprobación es una interacción independiente relacionada con la pregunta. |
| Reparación | Intento objetivo → caso → evidencia/diagnóstico → autorización → Hand → verificación. La evidencia se guarda antes del LLM y se limita al intento exacto. |
| Informe Pixy | Rama `pixy_report` y enlaces `reports_on` hacia el sujeto. Consultar un ID no autoriza ni inicia reparación. |
| Uso de skill | Intento → uso/ejecución → petición de gateway → intento de petición → transporte. Verificación, Dreams y entrega conservan la ejecución correspondiente. |

## Consulta

`/p informe <ID>` acepta el ID de ciclo, tarea, ejecución, autorización, orden, caso, fuente o traza. Devuelve un resumen determinista del árbol y los estados/errores técnicos disponibles. Si el ID no tiene registros, lo informa; no sustituye el sujeto por el último error de otra rama.

La API interna permite `Journal.find(reference)` para coincidencias exactas y `Journal.tree(cycle_id)` para la rama y sus descendientes. Los enlaces hacia otras ramas se muestran como referencias, sin mezclar sus eventos. El árbol se limita por defecto a 200 ciclos/1.000 eventos y marca el recorte; los límites máximos son 500/2.000. El resumen conversacional también informa recortes.

Ejemplo ilustrativo:

```text
interaction
└─ task
   └─ task_attempt (revisión 0, intento 1)
      ├─ routing
      │  └─ model_call → model_validation (rechazado)
      └─ repair (diagnóstico sin plan: sin reparación exitosa)

otra interaction
└─ pixy_report ─reports_on→ task_attempt anterior
```

## Transacciones, reinicio y efectos inciertos

Cuando estado y diario comparten SQLite, se escriben con la misma conexión/transacción. Se verifica rollback conjunto en pruebas. Las decisiones y órdenes del ControlPlane también registran su evento dentro de la transacción que cambia su estado.

Voice guarda cada acuse Telegram y su evento juntos. Rechaza respuestas sin `message_id` válido o con `ok=false`; conserva fragmentos entregados aunque falle el siguiente. Una entrega incierta no se retransmite automáticamente. Un `TypeError` interno del emisor tampoco provoca un segundo envío.

El dueño del proceso, después de adquirir su lock, marca ciclos activos interrumpidos como `unknown`; conserva las esperas de decisión. Heart cierra diagnósticos interrumpidos conservando su evidencia. Un agente de Hand inconcluso requiere reconciliación antes de reutilizar su ejecución. Ninguno de esos registros ordena repetir una VM, una herramienta ni un efecto externo.

Las bases privadas de Voice y de uso de modelos siguen separadas del SQLite común. **No hay una transacción distribuida entre esas bases.** El diario registra inicio antes de la operación y el resultado después; una interrupción entre recibo privado y evento común puede dejar el ciclo incierto y exige reconciliar contra el recibo. No se presenta esa ventana como atomicidad garantizada. Las peticiones de gateway mantienen sus IDs/recibos idempotentes y cada invocación tiene un intento propio, preservando el éxito original ante una repetición alterada.

## Migración y límites

El diario comienza con esta versión. No reconstruye respuestas antiguas perdidas ni inventa eventos históricos. Las tareas antiguas observadas registran una instantánea actual sin alterar su revisión por el mero hecho de auditarlas.

La política de inferencia y las autorizaciones de Pixy siguen siendo independientes de la auditoría. Esta implementación no introduce cancelación de transiciones de VM, el puente nano ni el esquema nativo de Ollama consultado anteriormente. Registra cancelaciones solo cuando realmente ocurren. La búsqueda conversacional con low requiere todavía resolver su contrato/interferencia del historial.

No hay purga automática del diario común. La antigua retención de 500 eventos de transición se retira para conservar evidencia. Una futura política de archivo deberá preservar IDs, referencias y verificabilidad.

## Validación

Pruebas de árbol e aislamiento concurrente, rollback, inmutabilidad, límites, reinicio incierto, cierres de Hand, IDs externos repetidos, decisiones/órdenes, informes sin reparación, evidencia antes de diagnóstico lento, rechazo exacto, transporte HTTP, acuses parciales y ausencia de replay. Resultado conjunto y aceptación de producción se registran en CHECKPOINT al completar la verificación.

Aceptación completada con código `385f20d`: 329 pruebas y 30 subpruebas locales; 13 pruebas seleccionadas sobre la imagen desplegada en contenedor desechable, sin red ni datos productivos. Dos interacciones reales por webhook (`/help` e informe Pixy por su ID) cerraron con entrega Telegram confirmada, árboles separados y enlace `reports_on`, sin inferencia ni reparación. Salud HTTPS e integridad de las cuatro bases verificadas. Los IDs y reportes reproducibles figuran en CHECKPOINT.
