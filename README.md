# Khai

Continuidad ya comprobada en el servidor: Nano respondió durante la carga de low y una cancelación real invalidó el cambio en ~146 ms. Mid cargó en 35,8 s; high sigue agotando los 90 s durante tensores. La búsqueda reveló un binding opcional ausente y un plan incorrecto de Heart, que fue rechazado. Las correcciones y su nueva aceptación se registran en CHECKPOINT; no se declara reparado high ni completada la búsqueda sin sus recibos.

Continuidad de modelos implementada y en validación: `/mode <perfil>` devuelve un ID y prepara el cambio en segundo plano; `/mode cancel` o `/stop` lo cancelan. Brain conserva el servicio actual o confirma Nano antes de reemplazar la VM compartida. Si Nano falla, solicita elegir un perfil; no arranca otros automáticamente. El contrato de Mind usa el esquema nativo de Ollama y separa la orden actual del historial. Estado real de despliegue y pruebas en [CHECKPOINT](./CHECKPOINT.md).

Validación local consolidada: 373 pruebas y 39 subpruebas aprobadas. Todas aprobadas; incluye el enmascaramiento de mensajes internos, precisión diagnóstica y normalización canónica en descriptores de servicios, serialización universal de firmas de herramientas y fallback reactivo. Los fixtures de modelos no sustituyen la aceptación real.

El propietario aprobó el [diario común de interacciones](./docs/COMMON_JOURNAL.md). La implementación enlaza modelos, chat, Hand, Pixy, Heart, informes y uso de skills mediante ciclos y eventos persistentes; `/p informe <ID>` consulta una rama sin iniciar reparación. Validación y despliegue de esta continuación se registran en CHECKPOINT.

La versión del diario común pasó 329 pruebas y 30 subpruebas. Conserva evidencia de errores y acuses de entrega; no reconstruye registros históricos perdidos ni considera una reparación exitosa sin verificación.

Desplegado con `385f20d`: 13 comprobaciones sobre la imagen en el servidor y dos interacciones reales por webhook confirmaron persistencia, separación de ramas e informe Pixy hasta entrega Telegram. `/health` e integridad SQLite verificados; evidencias en CHECKPOINT.

Khai es un agente autónomo y personal diseñado para investigar, construir, revisar, ejecutar y aprender capacidades reutilizables de forma segura, verificable y transparente.

La [auditoría de siete ciclos de interacción](./docs/INTERACTION_AUDIT.md) conserva el estado previo de modelos, chat, creación/uso de skills, Pixy, reparación e informes. Encontró huecos de relación entre ramas, conservación de llamadas y cierre de operaciones, y añadió 16 pruebas (303 pruebas y 25 subpruebas entonces). Motivó la implementación aprobada del diario común descrita arriba.

Estado al 31 de octubre: `web-search` y el parser PDF/CSV/XLSX están activos y verificados. Voice prepara wheels fijados y comprobados; los workers reutilizan un entorno sin red. La recuperación real del parser cerró RECOVERED en Heart. Low rechazó inicialmente un turno; la repetición instrumentada produjo un contrato válido y guardó la preferencia Brave/web_search. La aceptación conversacional de `busca <consulta>` sigue en curso.

El registro durable de rechazos del modelo y su derivación automática a Heart/Pixy/Dreams están desplegados: evidencia por tarea, esquema y errores, con secretos redactados y sin prompts. La suite completa aprobó 286 pruebas y 25 subpruebas; la protección adicional de diagnósticos lentos pasó las 57 pruebas de Mind/Heart. El rechazo histórico no se puede reconstruir y no volvió a aparecer en la repetición. El disco de modelos está en `pd-balanced`, `us-west1-b`, conservando el original. High y mid siguen fallando; Cloud Monitoring permanece deshabilitado por decisión del propietario. Mediciones e hipótesis en [`docs/LOAD_TIMES.md`](./docs/LOAD_TIMES.md).

El diagnóstico se ejecuta con un único propietario por tarea para que una respuesta lenta de Heart no cause diagnósticos simultáneos ni cierre prematuro del objetivo.

Para probar el parser localmente: `pip install -r requirements-dev.txt` y `python -m pytest -q`. La ingesta de adjuntos Telegram y OCR siguen pendientes.

Heart evita asociar consultas sobre transiciones de modelo a tareas no relacionadas; si falta telemetría vinculada, Pixy comunica que la evidencia es insuficiente.

El contrato para conservar la conversación mediante nano durante cambios de VM está en [`docs/MODEL_TRANSITIONS.md`](./docs/MODEL_TRANSITIONS.md); la implementación y su aceptación se registran arriba. Hand dispone de herramientas locales de análisis sintáctico y plantillas no aprobadas, y de lectura de URLs HTTPS explícitas a través de Voice.

La skill [`web-search`](./skills/web-search/SKILL.md) usa Brave Search API mediante Voice y devuelve título, URL y descripción. Está activa y verificada en producción desde el 31 de octubre de 2026; requiere `BRAVE_SEARCH_API_KEY` exclusivamente en `data/voice/secrets.env`. La investigación de Hand usa el mismo servicio declarado en `config/research.yaml`, sin respaldo automático a Google u otros buscadores. Esta configuración de infraestructura no establece una preferencia predeterminada del usuario.

El código `f4c845d`, desplegado el 31 de octubre, incluye mejoras de diagnóstico y verificación, herramientas básicas de Hand y Brave. La suite local pasó 273 pruebas y 22 subpruebas; la salud HTTPS, SQLite y ejecución real de búsqueda se verificaron en producción. Las evidencias están en `CHECKPOINT.md` y `docs/VALIDATION.md`; las transiciones cancelables entre modelos permanecen pendientes de implementación.

La infraestructura del sistema se valida mediante pruebas de integridad locales en 8 suites canónicas (384 pruebas unitarias y 39 subpruebas aprobadas), mientras que su aceptación funcional se rige por recorridos conversacionales reales en producción documentados en [`docs/VALIDATION.md`](./docs/VALIDATION.md).

---

## Órganos del Sistema

- **Mind / Brain:** Mind interpreta el lenguaje natural y actúa como el **wrapper humano bidireccional** del sistema: erradica la amnesia conversacional en preguntas de alcance (`scope`), controla el perfil y la base de conocimiento del usuario (idioma, ciudad base, unidades métricas), deduce parámetros contextuales evitando preguntas redundantes, humaniza las consultas de alcance y traduce las salidas técnicas (eliminando coletillas de auditoría como `(y/n)`) en respuestas fluidas. Además, conecta con Heart mediante la herramienta `report_issue` para registrar quejas y discrepancias semánticas como incidentes cognitivos. Incorpora **Brain** (`app/mind/brain/`) para coordinar el razonamiento profundo, la selección de modelos, la cascada de inferencia y el ciclo de vida de nodos acelerados.
- **Hand:** Ciclo activo de herramientas persistente; investiga, construye código, valida esquemas, repara y evoluciona skills (creando versiones incrementales v2, v3 en sandbox sin red bajo el Protocolo de Evolución de Khai).
- **Pixy:** Custodia de gobernanza, autoridad, secretos, contratos y administración de nivel Root (personalidad en `agent.md` y directivas en `dynamic_guidelines`); solicita confirmación binaria explícita (`y`/`n`) para cualquier modificación root, efecto externo o costo.
- **Heart:** Diagnóstico profundo ante fallos técnicos (`CoreEyes`) y autopsias cognitivas ante discrepancias semánticas (`cognitive_discrepancy`), emitiendo órdenes de evolución de habilidades (`evolve_skill`) hacia Hand.
- **Voice:** Concentra el acceso a redes externas, gestión segura de secretos y puntos de enlace de mensajería (Telegram).
- **Dreams:** Registra episodios de ejecución, extrae aprendizajes técnicos y propone mejoras al catálogo.

---

## Modos de Inferencia y Cascada

Khai opera bajo una arquitectura híbrida con una escala de respaldo estricta (`high > mid > low > nano`) administrada por Brain:

| Modo | Comando | Plataforma | Hardware | Modelo | Propósito |
| --- | --- | --- | --- | --- | --- |
| **High** | `/mode high` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM + NVIDIA L4 (24 GB) | `qwen2.5-32b-abliterated` | Razonamiento técnico y generación estructurada de skills |
| **Mid** | `/mode mid` | Google Cloud (Spot) | 4 vCPU, 16 GB RAM + NVIDIA L4 (24 GB) | `dolphin3:8b` | Respuestas rápidas aceleradas por GPU |
| **Low** | `/mode low` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM (CPU) | `dolphin3:8b` | Inferencia privada en CPU de respaldo |
| **Nano** | `/mode nano` | Google Cloud API | Cloud Serverless ($0 cómputo GCE) | Gemini API | Coordinación general, clasificación rápida y fallback |

*Consulta de estado y saldo de créditos:* `/mode ?` o `/m ?`.

---

## Entornos y Separación Operativa

Khai opera bajo un esquema desacoplado y complementario:
- **Host de Desarrollo Local (Windows / PowerShell):** Utilizado para edición de código, refactorización, curaduría documental y ejecución de suites de pruebas unitarias canónicas que verifican contratos, esquemas y lógica interna sin requerir infraestructura externa activa ni incurrir en costos de cómputo.
- **Host de Ejecución y Producción (Azure Linux / Docker):** Aloja los contenedores en producción (`khai_core` y `khai_docker_proxy`), el webhook TLS seguro de Telegram (puerto 8443), la pasarela de red aislada para workers y la orquestación directa con nodos GPU Spot en Google Cloud Platform. Las validaciones sintéticas de extremo a extremo se ejecutan directamente en este entorno.

---

## Comandos Operativos

```bash
make start      # Inicia el sistema Khai
make stop       # Detiene el sistema Khai
make test       # Ejecuta las 8 suites de pruebas unitarias canónicas (362 pruebas)
make stats      # Verifica la integridad de las bases de datos SQLite
make clean      # Limpia archivos temporales y logs respetando .venv
```

---

## Mapa Canónico de Documentación

Toda la documentación del proyecto se mantiene unificada en los siguientes documentos canónicos:

1. **[`.agents/GEMINI.md`](./.agents/GEMINI.md) — Constitución y Reglas de Comportamiento:**  
   Reglas éticas, límites de seguridad (workers sin red), directrices de inferencia, cascada y gobierno del agente.
2. **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — Manual Técnico Integral:**  
   Arquitectura de órganos (Mind/Brain, Hand, Pixy, Heart, Voice, Dreams), contratos, almacén transaccional (`control.sqlite3`), suites de pruebas canónicas, Decisiones Arquitectónicas Canónicas (ADRs 0001 a 0005), guía de operaciones e infraestructura híbrida acelerada (GCE Spot / WireGuard).
3. **[`docs/VALIDATION.md`](./docs/VALIDATION.md) — Validación Sintética y Evidencia:**  
   Protocolo de prueba sintética en producción (Pasos 1 al 7), suites de certificación y registro observable de ejecuciones y resoluciones.
4. **[`CHECKPOINT.md`](./CHECKPOINT.md) — Estado y Hoja de Ruta:**  
   Situación viva del repositorio, commits desplegados en Azure, suites canónicas verificadas y próximo paso inmediato a ejecutar.
