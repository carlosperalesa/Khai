# Khai

La reparación del 4 de noviembre incorpora decisiones activadas tras la entrega, verificación de objetivos con estado indeterminado, diagnósticos tipados y procedencia de habilidades. Búsqueda web y lectura de documentos son primitivas protegidas; Hand puede crear derivadas con otra identidad. Las suites completas de desarrollo y de la imagen Linux están verificadas; la aceptación de despliegue se registra en CHECKPOINT.

El reinicio administrativo conserva identidad, secretos, configuración, preferencias, conocimiento depurado, habilidades y aprobación técnica de sus hashes. Elimina historial operativo y respaldos. La limpieza de Telegram usa únicamente la API del bot: sus límites pueden dejar mensajes antiguos en Telegram.

Khai es un agente autónomo y personal diseñado para investigar, construir, revisar, ejecutar y aprender capacidades reutilizables de forma segura, verificable y transparente. Opera bajo una arquitectura modular de órganos especializados (Mind, Hand, Pixy, Heart, Voice y Dreams) con aislamiento riguroso en Docker y salida exclusiva de red por Voice.

> [!NOTE]
> Para el registro histórico de despliegues, validaciones y cambios autorizados, consultar [`CHECKPOINT.md`](./CHECKPOINT.md) y [`docs/VALIDATION.md`](./docs/VALIDATION.md).

---

## Órganos del Sistema

- **Mind / Brain:** Interpreta el lenguaje natural y actúa como el **wrapper humano bidireccional** del sistema: erradica la amnesia conversacional en preguntas de alcance (`scope`), controla el perfil y preferencias del usuario (idioma, ciudad, unidades métricas), deduce parámetros contextuales evitando preguntas redundantes, humaniza las consultas y traduce las salidas técnicas en respuestas fluidas. Además, conecta con Heart mediante la herramienta `report_issue` para registrar quejas y discrepancias semánticas como incidentes cognitivos. Incorpora **Brain** (`app/mind/brain/`) para coordinar el razonamiento profundo, la selección de modelos, el ciclo de vida de nodos acelerados y el fallback inmediato a Nano con selección interactiva.
- **Hand:** Ciclo activo de herramientas persistente; investiga, construye código, valida esquemas, repara y evoluciona skills bajo el estándar canónico de versionado de 3 dígitos (`.001`, `.002`, `.003` y rollback correlativo desde zip en sandbox sin red bajo el Protocolo de Evolución de Khai).
- **Pixy:** Custodia de gobernanza, autoridad, secretos, contratos y administración de nivel Root (personalidad en `agent.md` y directivas en `dynamic_guidelines`); solicita confirmación explícita con opciones claras para cualquier modificación root, efecto externo o costo.
- **Heart:** Diagnóstico profundo ante fallos técnicos (`CoreEyes`) y autopsias cognitivas ante discrepancias semánticas (`cognitive_discrepancy`), emitiendo órdenes de evolución de habilidades (`evolve_skill`) hacia Hand.
- **Voice:** Concentra el acceso a redes externas, gestión segura de secretos y puntos de enlace de mensajería (Telegram).
- **Dreams:** Registra episodios de ejecución, extrae aprendizajes técnicos y propone mejoras al catálogo.

---

## Modos de Inferencia y Fallback a Nano

Khai opera bajo una arquitectura híbrida con perfiles dedicados en GCE Spot y fallback inmediato a Modo Nano (Gemini) administrado por Brain:

| Modo | Comando | Plataforma | Hardware | Modelo | Propósito |
| --- | --- | --- | --- | --- | --- |
| **High** | `/mode high` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM + NVIDIA L4 (24 GB) | `qwen2.5-32b-abliterated` | Razonamiento técnico prioritario y generación estructurada de skills |
| **Mid** | `/mode mid` | Google Cloud (Spot) | 4 vCPU, 16 GB RAM + NVIDIA L4 (24 GB) | `dolphin3:8b` | Respuestas rápidas aceleradas por GPU |
| **Low** | `/mode low` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM (CPU) | `dolphin3:8b` | Inferencia privada en CPU de respaldo |
| **Nano** | `/mode nano` | Google Cloud API | Cloud Serverless ($0 cómputo GCE) | Gemini API | Coordinación general, clasificación rápida y fallback inmediato |

- *Política de Fallback:* Si un perfil Spot falla o no está disponible, Khai cambia inmediatamente a Modo Nano para mantener la conversación sin interrupciones, emite `[Modo <nombre> falló por: <motivo>]` y despliega la botonera interactiva (`MODE_SELECTION_BUTTONS`) para que el usuario decida.
- *Consulta de estado y saldo de créditos:* `/mode ?` o `/m ?`.

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
make test       # Ejecuta la suite de pruebas unitarias canónicas mediante pytest
make stats      # Verifica la integridad de las bases de datos SQLite
make clean      # Limpia archivos temporales y logs respetando .venv
```

---

## Mapa Canónico de Documentación

Toda la documentación del proyecto se mantiene unificada en los siguientes documentos canónicos:

1. **[`.agents/GEMINI.md`](./.agents/GEMINI.md) — Constitución y Reglas de Comportamiento:**  
   Reglas éticas, límites de seguridad (workers sin red), directrices de inferencia y gobierno del agente.
2. **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — Manual Técnico Integral:**  
   Arquitectura de órganos (Mind/Brain, Hand, Pixy, Heart, Voice, Dreams), contratos, almacén transaccional (`control.sqlite3`), suites de pruebas canónicas, Decisiones Arquitectónicas Canónicas (ADRs 0001 a 0005), guía de operaciones e infraestructura híbrida acelerada (GCE Spot / WireGuard).
3. **[`docs/VALIDATION.md`](./docs/VALIDATION.md) — Validación Sintética y Evidencia:**  
   Protocolo de prueba sintética en producción (Pasos 1 al 7), suites de certificación y registro observable de ejecuciones y resoluciones.
4. **[`CHECKPOINT.md`](./CHECKPOINT.md) — Estado y Hoja de Ruta:**  
   Situación viva del repositorio, bitácora histórica de avances, commits desplegados en Azure, suites canónicas verificadas y próximo paso inmediato a ejecutar.
