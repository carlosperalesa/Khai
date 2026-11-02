# Manual Técnico y Arquitectura de Khai

Este documento consolida la arquitectura integral del sistema Khai, sus contratos, órganos especializados, almacenamiento transaccional, seguridad, decisiones de arquitectura (ADRs), procedimientos de operaciones, la infraestructura híbrida acelerada (Brain) y la arquitectura canónica de pruebas.

> [!NOTE]
> Para el registro histórico de despliegues, validaciones y cambios autorizados en producción, consultar [`CHECKPOINT.md`](../CHECKPOINT.md) y [`docs/VALIDATION.md`](./VALIDATION.md).

---

## 1. Visión General y Flujo de Órganos

Khai opera mediante órganos especializados con responsabilidades y límites estrictos:

```text
                  +-------------------------------------------------+
                  |               Usuario (Telegram)                |
                  +-----------------------+-------------------------+
                                          |
                                          v
                                +-------------------+
                                |       Voice       | (Red externa, secretos, webhooks)
                                +---------+---------+
                                          |
                                          v
                                +-------------------+
                                |   Mind / Brain    | (Intención, capacidad, razonamiento,
                                +---------+---------+  modos VM y fallback a Nano)
                                          |
                     +--------------------+--------------------+
                     |                                         |
                     v                                         v
           +-------------------+                     +-------------------+
           |       Pixy        | (Gobernanza)        |       Hand        | (Creación y ejecución)
           +---------+---------+                     +---------+---------+
                     |                                         |
                     |         +-------------------+           |
                     +-------->|       Heart       |<----------+
                               |   (Diagnóstico)   |
                               +---------+---------+
                                         |
                                         v
                               +-------------------+
                               |      Dreams       | (Aprendizaje y notas de episodios)
                               +-------------------+
```

### Roles y Responsabilidades:
- **Mind / Brain:** Mind interpreta el lenguaje natural extrayendo la intención (`Intent`), la capacidad requerida y el proveedor opcional. Incorpora **Brain** (`app/mind/brain/`), que actúa como la fachada unificada de razonamiento, selección de modelos, controlador de infraestructura VM Spot (`ModelController`) y orquestador del fallback inmediato a Nano con selección interactiva.
- **Hand:** Ciclo activo de herramientas persistente; investiga, genera código, valida esquemas y ejecuta órdenes en contenedores seguros.
- **Pixy:** Custodia de gobernanza, autoridad, secretos y contratos; solicita confirmación binaria (`y`/`n`) y emite `DecisionReceipt`.
- **Heart:** Diagnóstico objetivo de fallos reiterados apoyado en telemetría real y lectura de logs del contenedor vía `CoreEyes`. Las consultas sobre transiciones de modelo no se vinculan a una tarea arbitraria; sin telemetría correlacionada se informa evidencia insuficiente sin generar un plan de reparación ajeno.

La recuperación de tareas distingue postcondiciones verificadas, entrega de una respuesta conversacional confirmada y evidencia insuficiente. La entrega no verifica la calidad de la respuesta. Las plantillas de Hand son propuestas no aprobadas y el lector de Voice acepta solo URLs explícitas del propietario, con límites y contenido no confiable. El diseño pendiente de continuidad con nano está en `docs/MODEL_TRANSITIONS.md`.

La skill `skills/web-search` llama a `brave_search.search` mediante Voice y devuelve resultados estructurados. Incluye SkillSpec, código sin dependencias y descriptor para Brave Search API. Voice resuelve `BRAVE_SEARCH_API_KEY` y la envía mediante `X-Subscription-Token`; el worker no recibe el secreto. Fue activada en producción el 31 de octubre tras revisión de Pixy y una ejecución aislada con recibo real; las actualizaciones futuras requieren repetir revisión, autorización y verificación.

La investigación de infraestructura de Hand usa `config/research.yaml` mediante `ResearchSearch` y el transporte existente de `ExternalGateway`: DNS público fijado, TLS, límites de tiempo, tamaño y frecuencia. El proveedor se declara en configuración; no se sustituyen proveedores al fallar. No modifica la inferencia de nano ni las preferencias de búsqueda del usuario. El candidato anterior de Google y su plantilla de descriptor fueron retirados del código local; los registros históricos de producción permanecen pendientes de migración.
- **Voice:** Concentrador único de conectividad externa con verificación TLS estricta, DNS pineado, resolución multihoming con priorización IPv4 y tolerancia a fallos de ruta IPv6, y aislamiento absoluto de secretos (`secrets.env`).
- **Dreams:** Persiste notas de episodios (`episode_notes`), consolida experiencias y formula recomendaciones de evolución de catálogo.

### 1.1. Capa de Humanización y Experiencia de Usuario: Mind como Wrapper
Mind opera como la capa cognitiva y el envoltorio humano (*Human Interface Wrapper*) bidireccional entre la maquinaria técnica determinista de Khai (Heart, Hand, Workers y Skills) y el usuario:

```text
Usuario (Lenguaje Natural Humano)
   ▲                       │
   │ [Respuesta Empática]  │ [Solicitud Conversacional]
   │                       ▼
┌──────────────────────────────────────────────┐
│             MIND / BRAIN WRAPPER             │
│  - Inbound: Deducción contextual y perfil    │
│  - Outbound: Síntesis fluida y humana        │
│  - Incidentes: report_issue hacia Heart      │
└──────────────────────────────────────────────┘
   ▲                       │
   │ [Datos Estructurados] │ [Parámetros Inyectados]
   │                       ▼
Órganos Técnicos y Workers Deterministas
(Ejecuta código,            (Diagnósticos, auditorías,
 recipes y APIs)             red y seguridad)
```

1. **Inbound Wrapper (Ingreso y Deducción Contextual):**
   - Controla las preferencias y el perfil persistente del usuario (`user_profile` en `MemoryEngine`), incluyendo idioma (`es`), ubicación base (`Santiago, Chile`), sistema métrico (`units: metric`) y zona horaria.
   - En el enrutamiento y planificación de recetas (`ActionPlanner`), infiere y enlaza automáticamente entidades geográficas y preferencias conocidas a los pasos correspondientes, evitando preguntas redundantes al usuario.
   - Si un parámetro obligatorio falta y no existe en el perfil, formula preguntas en español natural y empático (ej. *"¿Podrías indicarme la ciudad para continuar?"*) erradicando consultas técnicas de variables como `¿Qué valores corresponden a city?`.

2. **Outbound Wrapper (Síntesis Humana y Despacho Orgánico):**
   - Transforma resultados crudos de herramientas, códigos de salida y estructuras JSON complejas en respuestas humanas coherentes, empáticas y redactadas con estilo profesional.
   - Aplica el principio de *mínimo hardcoding*: las respuestas no usan plantillas estáticas rígidas; el modelo redacta la explicación basándose en los hechos devueltos.
   - Erradica coletillas mecánicas predecibles (como *"¿Es correcto el resultado completo? Responde y, n..."*). Cuando un resultado es concluyente, lo entrega directamente de forma orgánica.
   - Despacha notificaciones intermedias de cortesía de forma asíncrona cuando una tarea requiere ejecución en segundo plano, evitando la percepción de lentitud o congelamiento.

3. **Canal de Incidentes Cognitivos hacia Heart (`report_issue`):**
   - Si durante la interacción el usuario expresa inconformidad, corrección o frustración respecto a la calidad semántica de una respuesta previa (ej. *"eso no es lo que te pedí"*, *"me diste el clima actual y te pedí el pronóstico"*), Mind no entra en bucle defensivo.
   - Invoca la herramienta `report_issue(complaint, severity="cognitive")` para abrir formalmente un caso de discrepancia cognitiva (`cognitive_discrepancy`) en Heart.
   - Heart audita la discrepancia contrastando la intención original, la evidencia obtenida y la queja del usuario, emitiendo un diagnóstico para evolucionar la receta o ajustar la skill sin requerir reintentos a ciegas.

4. **Interacción Orgánica en Decisiones de Gobernanza:**
   - Para aprobaciones de cambios root o nuevos servicios gestionados por Pixy, el wrapper integra botones táctiles inline (`InlineKeyboardMarkup`) con opciones binarias claras (`Sí, proceder` / `No, cancelar`).
   - Ante respuestas mixtas o clarificaciones durante la aprobación (ej. "sí, y además dame el viento", "¿sabes? ahora prefiero el clima de Valparaíso"), `Coordinator.answer_question` no emite bloqueos mecánicos de "responde sí o no"; ejecuta `revise_goal` adaptando el alcance y actualizando los requerimientos en caliente.

---

## 2. Contratos Canónicos del Sistema

Los contratos se definen como modelos Pydantic inmutables:

- **`Goal` (`app/contracts/goal.py`):** Define el objetivo atómico del usuario.
- **`SkillSpec` (`app/contracts/skill.py`):** Define la especificación formal de una habilidad. El versionado es correlativo de tres dígitos (`.001`, `.002`, etc.) derivado del último registro en SQLite `versions`.
- **`ServiceDescriptor` (`app/contracts/service_descriptor.py`):** Especificación declarativa de servicios externos consumibles por workers.
- **`DecisionReceipt` (`app/contracts/decision.py`):** Comprobante firmado de autorización emitido por Pixy.

---

## 3. Almacenamiento Transaccional Central (`data/control.sqlite3`)

Toda la persistencia transaccional reside en un único almacén SQLite central con aislamiento lógico por repositorios:

- `control_tasks`: Tareas, estado y resultados estructurados.
- `recovery_cases`: Casos de diagnóstico y planes emitidos por Heart.
- `authorizations`: Registros de gobernanza, decisiones y aprobaciones de Pixy.
- `skills`: Catálogo de especificaciones activas.
- `versions`: Historial de versiones y archivos zip inmutables de código.
- `common_journal`: Ciclos, etapas y eventos estructurados del sistema.
- `voice_outbox`: Mensajes salientes hacia el cliente de Telegram.
- `data/voice/secrets.env`: Administrado exclusivamente por Voice.

---

## 4. Límites de Seguridad y Aislamiento

1. **Workers Docker sin Red:** Los contenedores worker se ejecutan con flag `--network none`.
2. **Salida Exclusiva por Voice:** Ningún worker accede a la red; las solicitudes pasan por `khai_gateway.request` hacia la pasarela de Voice.
3. **Gestión de Secretos:** Los secretos nunca se envían al prompt ni a los workers. Voice inyecta tokens en cabeceras HTTP de forma privada.
4. **Gobernanza y Autoridad:** Solo el usuario autorizado (`TELEGRAM_USER_ID`) puede autorizar cambios root o nuevos servicios.

---

## 5. Decisiones Arquitectónicas Canónicas (ADRs)

### ADR-0001: Separar Aprendizaje, Reparación y Evolución
- **Decisión:** Tratar aprendizaje, reparación y evolución como finalidades distintas con criterios de aceptación independientes.
- **Consecuencias:** `COMPLETED`, `RECOVERED` y entrega confirmada representan hechos diferentes. Una reparación genera evidencia pero no demuestra aprendizaje hasta su reutilización exitosa. Dreams propone; no activa reglas ni despliega cambios sin supervisión.

### ADR-0002: Mantener Workers sin Red Directa
- **Decisión:** Mantener deshabilitada la red directa en los workers. Las operaciones externas pasan exclusivamente por Voice mediante servicios declarativos y recibos idempotentes.
- **Consecuencias:** Hand propone descriptores, Pixy autoriza su alcance y Voice registra el transporte. Los secretos permanecen aislados en Voice.

### ADR-0003: Control Store Común en `data/control.sqlite3`
- **Decisión:** Consolidar el estado operacional del Core en `data/control.sqlite3` manteniendo propietarios lógicos por repositorio.
- **Consecuencias:** Esquema estructurado y transaccional único. Migraciones versionadas por componente para prevenir colisiones.

### ADR-0004: Puertos Acotados, Repositorios Lógicos y Recibos
- **Decisión:** Cada órgano interactúa únicamente mediante un puerto formal inyectado en bootstrap con validación estricta de procedencia.
- **Consecuencias:** Desacoplamiento estricto. Los órganos no comparten conexiones SQLite directas entre sí. `trace_id` se reserva para correlación y no confiere autoridad.

### ADR-0005: Capacidades, Proveedores y Transparencia
- **Decisión:** Representar solicitudes bajo la tríada (capacidad, proveedor opcional, objetivo). Proveedores registrados en metadatos y resueltos por orden explícito, preferencia aprobada o consulta.
- **Consecuencias:** Agregar un proveedor nuevo no altera Mind ni Coordinator. Prohibidas las sustituciones silenciosas de servicios.

---

## 6. Operaciones, Despliegue y Ciclo de Vida

### Entorno de Producción:
- **Servidor:** Azure VM (`[AZURE_USER]@[AZURE_SERVER_IP]`, Ubuntu 24.04 aarch64 en `/opt/khai`).
- **Contenedores:**
  - `khai_core` (aplicación principal y webhook HTTPS en `:8443`).
  - `khai_docker_proxy` (controlador seguro del socket de Docker en `:8001`).

### Procedimiento de Despliegue:
La transferencia de secretos actualiza exclusivamente la clave necesaria en `data/voice/secrets.env`, conservando las demás credenciales y sin incluir valores en comandos, Git, logs o imágenes. La imagen excluye `data/` y archivos `.env`. Tras reconstruir el Core, verificar `/health`, SQLite y una operación real del servicio configurado. Una skill candidata solo entra al catálogo después de revisión de Pixy, ejecución aislada y evidencia del resultado.

1. Conectar por SSH a `[AZURE_USER]@[AZURE_SERVER_IP]`.
2. Actualizar código fuente: `cd /opt/khai && git pull origin main`.
3. Reconstruir y reiniciar contenedores: `sudo docker compose up -d --build bot`.
4. Verificar logs de inicio: `sudo docker logs --tail 30 khai_core`.
5. Comprobar integridad del sistema: `make stats` o consulta a `m.heart.check_integrity()`.

### Procedimiento de Purga y Mantenimiento:
- **Limpieza rutinaria:** Ejecutar `make clean` (elimina archivos temporales, logs antiguos y caché preservando `.venv`).
- **Purga de estado:** Se realiza resolviendo rutas absolutas para eliminar exclusivamente SQLite de runtime, borradores y workspaces generados, preservando siempre certificados, identidad y secretos (`secrets.env`).
- **Rollback:** Si un despliegue presenta problemas de arranque, se detiene el contenedor, se hace checkout al commit sano previo en Git, se reconstruye el contenedor y se documenta el incidente con `/p corrige el ultimo error`.

---

## 7. Infraestructura Acelerada de Inferencia y Razonamiento (Brain)

Khai cuenta con una arquitectura híbrida para inferencia local privada de alta velocidad y razonamiento avanzado sin depender exclusivamente de APIs comerciales externas.

### Unificación en Brain (`app/mind/brain/`):
- `Brain` consolida la fachada de razonamiento, selección de modelos, controlador de infraestructura (`ModelController`) y soporte de scripting en un único subsistema cohesivo.
- Se eliminó el directorio raíz obsoleto `/Dolphin/`, consolidando sus herramientas de aprovisionamiento en `app/mind/brain/gcloud-ollama.ps1`.
- Para preservar compatibilidad con código existente, `DolphinController` permanece definido como un alias directo de `ModelController`.

### Topología de Red e Infraestructura:
```text
  Telegram Webhook (:8443) ---> Azure VM ([AZURE_VM_NAME], West US)
                                   |
                  Túnel WireGuard privado (UDP 51820)
                  10.200.0.1 (Azure) <-> 10.200.0.2 (GCP) (RTT ~22 ms)
                                   |
                                   v
             Nodo Efímero GCE Spot (us-west1-b, Oregón)
             Disco Persistente Único: dolphin-boot-disk (100 GB pd-balanced, auto-delete=no)
             Ubuntu 24.04 LTS, Ollama (:11434), Drivers NVIDIA L4 (580-server-open-gcp)
```

### Perfiles de Inferencia:
1. **Modo High (`high`):** GCE Spot `g2-standard-8` (8 vCPU, 32 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `qwen2.5-32b-abliterated` (19 GB, 32B parámetros, ~$550 CLP/hora). Perfil prioritario para razonamiento técnico y generación estructurada.
2. **Modo Mid (`mid`):** GCE Spot `g2-standard-4` (4 vCPU, 16 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `dolphin3:8b` (~$418 CLP/hora). Respuestas rápidas aceleradas por GPU.
3. **Modo Low (`low`):** GCE Spot `e2-standard-8` (8 vCPU, 32 GB RAM, CPU). Modelo: `dolphin3:8b` (~$152 CLP/hora). Inferencia privada en CPU de respaldo.
4. **Modo Nano (`nano`):** Gemini Cloud API ($0 cómputo GCE). La VM Spot se apaga/elimina conservando el disco persistente en reposo. Modo de coordinación general y fallback.

### Resiliencia y Fallback Inmediato a Nano (Constitución GEMINI.md):
- Exactamente el perfil solicitado por el usuario; sin cascada ciega que consuma presupuesto en VMs secundarias.
- Ante la caída o fallo de un perfil Spot (high, mid, low), Khai conmuta inmediatamente a **Modo Nano (Gemini)** para garantizar la continuidad conversacional sin demoras.
- Mensajería estricta durante transiciones:
  - Al iniciar conexión o cambio de modo: `[Conectando con Modo <nombre>]`
  - Al completar la conexión exitosamente: `[Modo <nombre> conectado]`
  - Al fallar un perfil: `[Modo <nombre> falló por: <motivo>]` (ej. `tiempo de espera agotado`, `spot no disponible`) seguido de `[Modo nano conectado]` y el despliegue interactivo de la botonera (`MODE_SELECTION_BUTTONS`) para que el usuario decida si reintentar o cambiar de modelo.
- Inactividad controlada: temporizador que apaga la VM tras 10 minutos sin tráfico para minimizar costos.
- Control de presupuestos en GCP con alarmas escalonadas cada $1.000 CLP.

---

## 8. Arquitectura Canónica de Pruebas y Certificación

Toda modificación al código fuente debe ser verificada localmente antes de cualquier despliegue mediante la suite de pruebas unitarias canónicas (`tests/test_*.py`).

**Ejecución estándar única:**
```bash
make test
```
*(Internamente invoca `pytest -q` de forma rápida y determinista).*

El estado vivo de certificación, la bitácora de ejecuciones y el conteo canónico de pruebas se mantienen exclusivamente en [`CHECKPOINT.md`](../CHECKPOINT.md).
