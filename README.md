# Khai

Actualización del 31 de octubre, en validación: la invocación de búsqueda pasa a `busca <consulta>`; Brave fue autorizado y falta aceptar su preferencia en el recorrido conversacional. Voice prepara una caché reutilizable de wheels fijados y verificados; los workers instalan sin red. El nuevo parser extrae texto de PDF, CSV y XLSX ya incluidos en los datos de una skill; todavía no recibe adjuntos de Telegram ni hace OCR. Para ejecutar las pruebas con esos formatos: `pip install -r requirements-dev.txt` y `python -m pytest -q`.

La migración autorizada del disco de modelos a `pd-balanced` conserva el original para volver atrás. GCP rechazó la creación en `us-west1-a` por falta de capacidad; el avance y las pruebas están en `CHECKPOINT.md`. Brain incorpora telemetría para Heart; la continuidad mediante nano y las transiciones cancelables siguen pendientes.

La copia `pd-balanced` ya está configurada en `us-west1-b`. High sigue agotando la precarga, por lo que se medirán almacenamiento y GPU antes de cambiar el plazo. La recuperación de dependencias ahora distingue un fallo anterior al despacho y permite a Pixy/Heart reanudar el código ya aprobado, conservando la evidencia del intento anterior.

Por decisión del propietario, Cloud Monitoring permanece deshabilitado; la revisión de modelos continúa solo con registros existentes.

Low conectó con una precarga de30,81 s; high y mid siguen pendientes de diagnóstico. La corrección del contrato del proxy conserva el lock de dependencias y la revisión del objetivo al despachar recuperaciones.

Khai es un agente autónomo y personal diseñado para investigar, construir, revisar, ejecutar y aprender capacidades reutilizables de forma segura, verificable y transparente.

Heart evita asociar consultas sobre transiciones de modelo a tareas no relacionadas; si falta telemetría vinculada, Pixy comunica que la evidencia es insuficiente.

El diseño propuesto para conservar la conversación mediante nano durante cambios de VM está en [`docs/MODEL_TRANSITIONS.md`](./docs/MODEL_TRANSITIONS.md); todavía no está implementado. Hand dispone de herramientas locales de análisis sintáctico y plantillas no aprobadas, y de lectura de URLs HTTPS explícitas a través de Voice.

La skill [`web-search`](./skills/web-search/SKILL.md) usa Brave Search API mediante Voice y devuelve título, URL y descripción. Está activa y verificada en producción desde el 31 de octubre de 2026; requiere `BRAVE_SEARCH_API_KEY` exclusivamente en `data/voice/secrets.env`. La investigación de Hand usa el mismo servicio declarado en `config/research.yaml`, sin respaldo automático a Google u otros buscadores. Esta configuración de infraestructura no establece una preferencia predeterminada del usuario.

El código `f4c845d`, desplegado el 31 de octubre, incluye mejoras de diagnóstico y verificación, herramientas básicas de Hand y Brave. La suite local pasó 273 pruebas y 22 subpruebas; la salud HTTPS, SQLite y ejecución real de búsqueda se verificaron en producción. Las evidencias están en `CHECKPOINT.md` y `docs/VALIDATION.md`; las transiciones cancelables entre modelos permanecen pendientes de implementación.

La infraestructura del sistema se valida mediante pruebas de integridad locales en 8 suites canónicas (264 pruebas unitarias), mientras que su aceptación funcional se rige por recorridos conversacionales reales en producción documentados en [`docs/VALIDATION.md`](./docs/VALIDATION.md).

---

## Órganos del Sistema

- **Mind / Brain:** Mind interpreta el lenguaje natural (intención, capacidad requerida y proveedor opcional). Incorpora **Brain** (`app/mind/brain/`) para coordinar el razonamiento profundo, la selección de modelos, la cascada de inferencia y el ciclo de vida de nodos acelerados.
- **Hand:** Ciclo activo de herramientas persistente; investiga, construye código, valida esquemas y repara skills.
- **Pixy:** Custodia de gobernanza, autoridad, secretos y contratos; solicita aprobación explícita (`y`/`n`) para efectos externos o costos.
- **Heart:** Diagnóstico profundo ante fallos reiterados o sistémicos utilizando evidencia real de telemetría y logs (`CoreEyes`).
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
make test       # Ejecuta las 8 suites de pruebas unitarias canónicas (264 pruebas)
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
