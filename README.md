# Khai

Khai es un agente autónomo y personal diseñado para investigar, construir, revisar, ejecutar y aprender capacidades reutilizables de forma segura, verificable y transparente.

La infraestructura del sistema se valida mediante pruebas de integridad, mientras que su aceptación funcional se rige por recorridos conversacionales reales en producción documentados en [`docs/VALIDATION.md`](./docs/VALIDATION.md).

---

## Órganos del Sistema

- **Mind:** Interpreta el lenguaje natural, extrayendo la intención, la capacidad requerida y el proveedor opcional.
- **Hand:** Ciclo activo de herramientas persistente; investiga, construye código, valida esquemas y repara skills.
- **Pixy:** Custodia de gobernanza, autoridad, secretos y contratos; solicita aprobación explícita (`y`/`n`) para efectos externos o costos.
- **Heart:** Diagnóstico profundo ante fallos reiterados o sistémicos utilizando evidencia real de telemetría y logs (`CoreEyes`).
- **Voice:** Concentra el acceso a redes externas, gestión segura de secretos y puntos de enlace de mensajería (Telegram).
- **Dreams:** Registra episodios de ejecución, extrae aprendizajes técnicos y propone mejoras al catálogo.

---

## Modos de Inferencia y Cascada

Khai opera bajo una arquitectura híbrida con una escala de respaldo estricta (`high > mid > low > nano`):

| Modo | Comando | Plataforma | Hardware | Modelo | Propósito |
| --- | --- | --- | --- | --- | --- |
| **High** | `/mode high` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM + NVIDIA L4 (24 GB) | `qwen2.5-32b-abliterated` | Razonamiento técnico y generación estructurada de skills |
| **Mid** | `/mode mid` | Google Cloud (Spot) | 4 vCPU, 16 GB RAM + NVIDIA L4 (24 GB) | `dolphin3:8b` | Respuestas rápidas aceleradas por GPU |
| **Low** | `/mode low` | Google Cloud (Spot) | 8 vCPU, 32 GB RAM (CPU) | `dolphin3:8b` | Inferencia privada en CPU de respaldo |
| **Nano** | `/mode nano` | Google Cloud API | Cloud Serverless ($0 cómputo GCE) | Gemini API | Coordinación general, clasificación rápida y fallback |

*Consulta de estado y saldo de créditos:* `/mode ?` o `/m ?`.

---

## Comandos Operativos

```bash
make start      # Inicia el sistema Khai
make stop       # Detiene el sistema Khai
make test       # Ejecuta la suite de pruebas unitarias
make stats      # Verifica la integridad de las bases de datos SQLite
make clean      # Limpia archivos temporales y logs respetando .venv
```

---

## Mapa Canónico de Documentación

Toda la documentación del proyecto se mantiene unificada en los siguientes documentos canónicos:

1. **[`.agents/GEMINI.md`](./.agents/GEMINI.md) — Constitución y Reglas de Comportamiento:**  
   Reglas éticas, límites de seguridad (workers sin red), directrices de inferencia, cascada y gobierno del agente.
2. **[`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — Manual Técnico Integral:**  
   Arquitectura de órganos, contratos, almacén transaccional (`control.sqlite3`), seguridad, Decisiones Arquitectónicas Canónicas (ADRs 0001 a 0005), guía de operaciones/despliegue e infraestructura híbrida Dolphin.
3. **[`docs/VALIDATION.md`](./docs/VALIDATION.md) — Validación Sintética y Evidencia:**  
   Protocolo de prueba sintética en producción (Pasos 1 al 7) y registro observable de ejecuciones y resoluciones.
4. **[`CHECKPOINT.md`](./CHECKPOINT.md) — Estado y Hoja de Ruta:**  
   Situación viva del repositorio, commits desplegados en Azure y próximo paso inmediato a ejecutar.
