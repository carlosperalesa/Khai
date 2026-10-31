# Validación Sintética y Protocolo de Evidencia

## Despliegue y aceptación Brave — 31 de octubre de 2026

- Código desplegado: `f4c845d`; 273 pruebas y 22 subpruebas locales aprobadas.
- Core reconstruido en Azure; `/health` HTTP 200 con `{"status":"ok"}` y certificado validado contra el certificado del servidor. Integridad `ok` en `control.sqlite3` y `voice.sqlite3`.
- Consulta de infraestructura real `Brave Search API documentation`, 3 resultados. La clave se mantuvo en Voice, sin valores en Git ni en la imagen.
- Aprobación del descriptor `brave_search.search@v1` por el puerto de Pixy y auditoría real del artefacto: `382aa529279c48068c3b2d5924551351`.
- Tarea: `task_b09af793c9b742a4979ccdd7b7df535d`; worker: `run_5f381e6200bd45c686ffd4f9ce422150`; versión: `c5ec15303ee848c1868be9a6611e0727`.
- Recibo real de Voice: `run_5f381e6200bd45c686ffd4f9ce422150:.voice-a23f4ff05f774400b1938924054a7706.request.json`.
- Verificación independiente: proceso finalizado con código 0, recibo completado ligado a la ejecución/operación, JSON del proveedor acorde al criterio fijado antes de ejecutar, salida de la skill con proveedor Brave y resultados con título, URL y descripción. Incluyó `https://brave.com/search/api/`, `https://api-dashboard.search.brave.com/documentation` y `https://api-dashboard.search.brave.com/api-reference/web/search/get`.
- Resultado: tarea `COMPLETED`; versión y SkillSpec `active`. La aceptación de despliegue no establece una preferencia persistente del usuario. Se registró el episodio en Dreams; reporte operativo en `/opt/khai/data/voice/deployment-web-search.json`.
- Respaldo previo: `/opt/khai/data/backups/deploy-20261031T204147Z`. Las transiciones cancelables de modelos y los escenarios conversacionales completos de Heart/Pixy siguen pendientes; esta prueba no acredita esos recorridos.

La salud del sistema y las suites de pruebas unitarias acreditan integridad estructural, pero no sustituyen la autonomía conversacional. Por ello, la aceptación de Khai se rige por un protocolo de dos niveles:

1. **Nivel 1 — Certificación de Integridad Local:** Ejecución limpia de las **8 suites canónicas** (264 pruebas unitarias en verde cubriendo Brain, Mind, Hand, Heart, Pixy, Voice, Dreams y Kernel).
2. **Nivel 2 — Validación Sintética en Producción:** Ejecución del recorrido conversacional de 7 pasos en el entorno real (Azure / Telegram / VM Spot en GCP), registrando de forma observable commit, fecha, IDs de tarea/run, tool calls, decisiones de Pixy y evidencias tangibles.

---

## Recorrido Canónico en Producción

Actualización del 31 de octubre: la nueva capacidad de búsqueda es el candidato `skills/web-search` con Brave. Para validar la nueva implementación: configurar `BRAVE_SEARCH_API_KEY` exclusivamente en Voice, revisar/aprobar su descriptor y código con Pixy, ejecutar una consulta real y verificar el recibo de Voice y los campos título, URL y descripción antes de promoverla al catálogo. Repetir la consulta debe reutilizar la skill; una falla de Brave no debe activar otro proveedor. La preferencia de búsqueda del usuario requiere aprobación aparte. Los pasos siguientes y la evidencia del 30 de octubre documentan el recorrido histórico de Google y no acreditan la nueva integración.

1. **Paso 1 (Creación con Gobernanza):** Inyectar:  
   `crea una skill para conectarse a internet y buscar en Google. Cuando diga "busca en Google <consulta>", devuelve una lista con título, enlace y descripción de cada resultado`
2. **Paso 2 (Observabilidad y Autorización):** Observar Mind, Hand, Pixy, Heart, Voice y Dreams. Ante secreto, permiso o decisión, detenerse y registrar texto exacto, motivo y opciones (`y`/`n`).
3. **Paso 3 (Reparación Asistida):** Ante fallo, inyectar `/p corrige el ultimo error`; verificar que Heart diagnostique basándose en logs reales (`CoreEyes`), Hand cambie la solución y Dreams guarde el episodio.
4. **Paso 4 (Ejecución Aislada con Proveedor):** Inyectar `busca en Google OpenClaw skills`; exigir resultados reales atribuidos al proveedor Google ejecutados en contenedor desconectado.
5. **Paso 5 (Neutralidad de Proveedores):** Inyectar `busca OpenClaw skills` (sin proveedor); verificar que Khai no sustituya automáticamente y pregunte neutralmente por el proveedor.
6. **Paso 6 (Preferencia Persistente):** Inyectar `usa Google como buscador predeterminado`, comprobar persistencia en SQLite (`control.sqlite3`) y re-ejecutar sin preguntas.
7. **Paso 7 (Reutilización de Artefactos):** Repetir para comprobar reutilización directa de la skill sin recompilación ni duplicación.

---

## Evidencia Observable y Estado de Validación (30 de Octubre de 2026)

### 1. Certificación Unitaria Local (Nivel 1)
- **Suites ejecutadas:** 8 suites canónicas (`test_brain.py`, `test_dreams.py`, `test_hand.py`, `test_heart.py`, `test_kernel.py`, `test_mind.py`, `test_pixy.py`, `test_voice.py`).
- **Resultado:** 264 pruebas pasadas al 100% (0 fallos, 0 errores).

### 2. Recorrido Sintético en Producción (Nivel 2)

#### Pasos 1 y 2: Gobernanza y Aprobación de Pixy
- **Commit evaluado en producción:** `126fc0d` (en contenedor `khai_core`).
- **Comportamiento observado:**
  - Mind interpretó correctamente la solicitud como `ACTION_REQUIRED` con `creation_requested: true`.
  - Pixy interrumpió de forma estricta solicitando confirmación explícita para la operación `list` de Google Custom Search (`www.googleapis.com`).
  - Tras responder `y`, Pixy registró la aprobación y dio paso a la construcción.
  - Se validó la no duplicidad de descriptores aprobados (`36f843b`) y la propagación de servicios autorizados a los workers (`360dabd`).

#### Detección del Cuello de Botella en Hand (Paso 2 -> 3)
- **Tarea:** `task_eedf074e67934b448f265ddfb4611f96`
- **Error:** `Presupuesto agotado: llm_calls` (16 llamadas).
- **Evidencia:**
  - `agent_runs` (`agent_3a3c49f0f1bb4aacada0eb0a2824bbe6`) registró 14 turnos sucesivos llamando a `web_search`.
  - La búsqueda con Google Grounding en Gemini arrojó cuota agotada (`HTTP 429`).
  - El scraping de DuckDuckGo en Azure devolvió `HTTP 202` (bot block de datacenter).
  - El fallback de Wikipedia arrojó artículos inconexos sobre GitHub/Markdown, dejando a Hand sin acceso a la documentación oficial para redactar los 15 campos del `ServiceDescriptor`.

#### Resoluciones Aprobadas para la Próxima Sesión
- Transición obligatoria a **Modo High** con modelo **`qwen2.5-32b-abliterated`** en GCE Spot GPU NVIDIA L4.
- Dotar a Voice/Hand de motor de búsqueda de infraestructura financiado con créditos de GCP / SearXNG con principio de fallback.
- Incorporar extractor de documentación web con truncado inteligente (~4.000 tokens priorizando esquemas y parámetros).
- Reanudar el recorrido desde el Paso 1 con el entorno fortalecido.
