# Validación sintética real

La salud y las suites no acreditan autonomía. Cada despliegue registrará commit, hora, IDs de tarea/run, estados, tool calls, decisiones y resultado.

## Recorrido Establecido

1. Inyectar: `crea una skill para conectarse a internet y buscar en Google. Cuando diga "busca en Google <consulta>", devuelve una lista con título, enlace y descripción de cada resultado`.
2. Observar Mind, Hand, Pixy, Heart, Voice y Dreams. Ante secreto, permiso o decisión, detenerse y registrar texto exacto, motivo y opciones.
3. Ante fallo, inyectar `/p corrige el ultimo error`; verificar que Heart use el error, Hand cambie la solución y Dreams guarde el episodio.
4. Inyectar `busca en Google OpenClaw skills`; exigir resultados reales y proveedor Google.
5. Rechazar un predeterminado automático e inyectar `busca OpenClaw skills`; debe preguntar proveedor.
6. Inyectar `usa Google como buscador predeterminado`, repetir la consulta genérica y comprobar el recibo.
7. Repetir para comprobar reutilización sin recreación.

## Evidencia y Estado al Cierre de Sesión (30 de Octubre de 2026)

### 1. Pasos 1 y 2: Gobernanza y Aprobación de Pixy
- **Commit evaluado en producción:** `126fc0d` (en contenedor `khai_core`).
- **Comportamiento observado:**
  - Mind interpretó correctamente la solicitud como `ACTION_REQUIRED` con `creation_requested: true`.
  - Pixy interrumpió de forma estricta solicitando confirmación explícita para la operación `list` de Google Custom Search (`www.googleapis.com`).
  - Tras responder `y`, Pixy registró la aprobación y dio paso a la construcción.
  - Se confirmó el parche que evita el error de descriptor duplicado (`36f843b`) y la propagación de servicios autorizados a los workers (`360dabd`).

### 2. Detección del Cuello de Botella en Hand (Paso 2 -> 3)
- **Tarea:** `task_eedf074e67934b448f265ddfb4611f96`
- **Error:** `Presupuesto agotado: llm_calls` (16 llamadas).
- **Evidencia:**
  - `agent_runs` (`agent_3a3c49f0f1bb4aacada0eb0a2824bbe6`) registró 14 turnos sucesivos llamando a `web_search`.
  - La búsqueda con Google Grounding falló por cuota (`HTTP 429`).
  - El scraping de DuckDuckGo en Azure devolvió `HTTP 202` (bot block de datacenter).
  - El fallback de Wikipedia arrojó artículos inconexos sobre GitHub/Markdown, dejando a Hand sin acceso a la documentación oficial para redactar los 15 campos del `ServiceDescriptor`.

### 3. Resoluciones Aprobadas para la Próxima Sesión
- Transición a **Modo High** con modelo **`qwen2.5-32b-abliterated`** en GCE Spot GPU L4.
- Dotar a Voice/Hand de búsqueda web respaldada por créditos de GCP / SearXNG con principio de fallback.
- Extractor de documentación con truncado inteligente.
- Reanudar el recorrido desde el Paso 1 con el entorno fortalecido.
