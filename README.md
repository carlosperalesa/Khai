# Khai

Khai es un agente autónomo y personal diseñado para investigar, construir, revisar, ejecutar y aprender capacidades reutilizables de forma segura, verificable y transparente.

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
