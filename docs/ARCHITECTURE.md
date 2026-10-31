# Manual Técnico y Arquitectura de Khai

Este documento consolida la arquitectura del sistema, contratos, almacenamiento, seguridad, decisiones históricas (ADRs), procedimientos de operaciones, la infraestructura híbrida acelerada (Brain) y la arquitectura canónica de pruebas.

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
                                +---------+---------+  cascada de inferencia y modos VM)
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
- **Mind / Brain:** Mind interpreta el lenguaje natural extrayendo la intención (`Intent`), la capacidad requerida y el proveedor opcional. Incorpora **Brain** (`app/mind/brain/`), que actúa como la fachada unificada de razonamiento, selección de modelos, controlador de VM Spot (`ModelController`) y orquestador de la cascada de inferencia (`high > mid > low > nano`).
- **Hand:** Ciclo activo de herramientas persistente; investiga, genera código, valida esquemas y ejecuta órdenes en contenedores seguros.
- **Pixy:** Custodia de gobernanza, autoridad, secretos y contratos; solicita confirmación binaria (`y`/`n`) y emite `DecisionReceipt`.
- **Heart:** Diagnóstico objetivo de fallos reiterados apoyado en telemetría real y lectura de logs del contenedor vía `CoreEyes`.
- **Voice:** Concentrador único de conectividad externa con verificación TLS estricta, DNS pineado y aislamiento absoluto de secretos (`secrets.env`).
- **Dreams:** Persiste notas de episodios (`episode_notes`), consolida experiencias y formula recomendaciones de evolución de catálogo.

### Ciclo de Vida de una Solicitud:
1. **Interpretación (Mind):** Convierte el mensaje en un `Intent` (objetivo, capacidad requerida y proveedor opcional).
2. **Filtrado de Catálogo:** Busca skills activas que satisfagan la capacidad. Si no hay proveedor explícito, usa una preferencia aprobada persistente; si no existe, consulta al usuario.
3. **Creación o Reparación (Hand):** Si la capacidad no existe o falló, abre un `agent_run` durable con herramientas persistentes (LLM, análisis de código, tests locales).
4. **Gobernanza y Autorización (Pixy):** Revisa descriptores de servicio y artefactos. Si se requieren permisos externos, secretos o costos, solicita confirmación binaria (`y`/`n`) al usuario y emite un `DecisionReceipt`.
5. **Ejecución Segura (Voice + Worker):** Voice procesa llamadas externas mediante servicios declarativos. El worker ejecuta la skill en un contenedor Docker con red directa deshabilitada (`--network none`).
6. **Verificación y Aprendizaje (Dreams):** Evalúa postcondiciones observables. Si el resultado es exitoso, promueve la skill en el catálogo y registra notas en `episode_notes`. Si falla de forma idéntica dos veces, transfiere el diagnóstico a Heart.

---

## 2. Contratos y Estructura de Skills

### Contratos Fundamentales:
- **`Intent`:** Contiene `goal`, `capability` y `provider` (opcional).
- **`CapabilitySpec`:** Declara la capacidad técnica, proveedor, alias, operaciones permitidas, requerimientos y estado de ciclo de vida.
- **`DecisionReceipt`:** Registra la decisión del usuario, alcance autorizado, firma/hash, vigencia y procedencia.
- **`SkillSpec`:** Vincula esquemas, entorno de ejecución, dependencias, servicios autorizados y verificador de postcondición.

### Anatomía de una Skill (`data/skills/<nombre>/`):
- `SKILL.md`: Documento canónico de instrucciones con frontmatter YAML (`capability`, `provider`, `aliases`).
- `app/`: Código ejecutable (`main.py`) que implementa la lógica determinista.
- `data/`: Almacén de datos local y aislado de la skill.
- `other/`: Archivos auxiliares, plantillas y recursos estáticos.

---

## 3. Almacenamiento Central y Puertos de Control

Todo el estado transaccional del Core se consolida en una base de datos SQLite común: **`data/control.sqlite3`**.

### Puertos Acotados y Propietarios Lógicos:
Cada órgano accede a través de un puerto formal con operaciones de escritura restringidas:

| Puerto | Escrituras Permitidas | Propietario Lógico |
| --- | --- | --- |
| **Coordinator** | Objetivos, intentos, transiciones de estado, presupuestos, comandos de usuario | Coordinación global |
| **Pixy** | Solicitudes de aprobación, decisiones tomadas, autorizaciones emitidas | Gobernanza y contratos |
| **Hand** | Artefactos, versiones de skills, órdenes de trabajo, recibos de ejecución | Construcción y ejecución |
| **Heart** | Casos de recuperación, evidencias referenciadas, diagnósticos, planes correctivos | Salud y diagnóstico |

### Principios del Almacén de Control:
- **Sin bearer tokens:** Los identificadores (`authorization_id`, `order_id`) son referencias contextuales; requieren coincidencia exacta de operación, caso, hash y vigencia para ser consumidos.
- **Transacciones atómicas:** La aprobación en Pixy y la habilitación de la orden en Hand ocurren en una transacción atómica. Hand consume la orden de forma idempotente.
- **Archivos excluidos de `control.sqlite3`:**
  - `data/voice/secrets.env`: Administrado exclusivamente por Voice.
  - `data/voice/dolphin_usage.sqlite3`: Sesiones y estado de modo de inferencia local administrado por Brain (`ModelController`).
  - `data/mind/identity.yaml`: Identidad estática del agente.
  - Recibos externos de gateways de red.

---

## 4. Aislamiento de Red y Modelo de Seguridad

- **Workers Desconectados:** Los contenedores de ejecución de skills carecen de acceso a interfaces de red externas (`--network none`).
- **Pasarela Centralizada (Voice):** Cualquier salida HTTP pasa por Voice mediante descriptores de servicio validados que definen dominios, rutas, métodos y esquemas de parámetros autorizados.
- **Aislamiento de Secretos:** Las claves de API y credenciales residen en el host y se inyectan en tiempo de ejecución en Voice. Nunca se graban en imágenes de Docker, historiales de chat, prompts ni descriptores de skills.

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
- **Servidor:** Azure VM (`carlos@20.237.211.250`, Ubuntu 24.04 aarch64 en `/opt/khai`).
- **Contenedores:**
  - `khai_core` (aplicación principal y webhook HTTPS en `:8443`).
  - `khai_docker_proxy` (controlador seguro del socket de Docker en `:8001`).

### Procedimiento de Despliegue:
1. Conectar por SSH a `carlos@20.237.211.250`.
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
  Telegram Webhook (:8443) ---> Azure VM (vm-carlosperales, West US)
                                   |
                  Túnel WireGuard privado (UDP 51820)
                  10.200.0.1 (Azure) <-> 10.200.0.2 (GCP) (RTT ~22 ms)
                                   |
                                   v
             Nodo Efímero GCE Spot (us-west1-a, Oregón)
             Disco Persistente Único: dolphin-boot-disk (100 GB pd-standard, auto-delete=no)
             Ubuntu 24.04 LTS, Ollama (:11434), Drivers NVIDIA L4 (580-server-open-gcp)
```

### Perfiles de Inferencia:
1. **Modo High (`high`):** GCE Spot `g2-standard-8` (8 vCPU, 32 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `qwen2.5-32b-abliterated` (19 GB, 32B parámetros, ~$550 CLP/hora). Perfil prioritario para razonamiento técnico y generación estructurada.
2. **Modo Mid (`mid`):** GCE Spot `g2-standard-4` (4 vCPU, 16 GB RAM + 1x NVIDIA L4 24 GB VRAM). Modelo: `dolphin3:8b` (~$418 CLP/hora). Respuestas rápidas aceleradas por GPU.
3. **Modo Low (`low`):** GCE Spot `e2-standard-8` (8 vCPU, 32 GB RAM, CPU). Modelo: `dolphin3:8b` (~$152 CLP/hora). Inferencia privada en CPU de respaldo.
4. **Modo Nano (`nano`):** Gemini Cloud API ($0 cómputo GCE). La VM Spot se apaga/elimina conservando el disco persistente en reposo. Modo de coordinación general y fallback.

### Cascada y Resiliencia:
- Cascada automática en orden estricto: `high > mid > low > nano`.
- Mensajes limpios y concisos durante transiciones:
  - Al iniciar conexión o cambio de modo: `[Conectando con Modo <nombre>]`
  - Al completar la conexión exitosamente: `[Modo <nombre> conectado]`
  - Al fallar un perfil: `[Modo <nombre> falló por: <motivo>]` (ej. `spot no disponible`) continuando inmediatamente con el siguiente perfil.
  - Al agotar la escala local y entrar a Gemini: `[Modo nano conectado]`
- Inactividad controlada: temporizador que apaga la VM tras 10 minutos sin tráfico para minimizar costos.
- Control de presupuestos en GCP con alarmas escalonadas cada $1.000 CLP.

---

## 8. Arquitectura Canónica de Pruebas y Certificación

Toda modificación al código fuente puede ser certificada localmente antes de cualquier despliegue por peticion del usuario mediante las **8 suites canónicas** (`tests/test_*.py`):

| Suite de Prueba | Órgano / Componente | Foco de Validación |
| --- | --- | --- |
| **`test_brain.py`** | Mind / Brain | Enrutamiento LLM, cascada `high>mid>low>nano`, ModelController, ciclo de VM Spot, banners y timeouts. |
| **`test_dreams.py`** | Dreams | Aprendizaje experiencial, registro de `episode_notes` y políticas de promoción de skills. |
| **`test_hand.py`** | Hand | Ciclo de construcción de código, orden de trabajo, validación de esquemas, recetas y aislamiento Docker. |
| **`test_heart.py`** | Heart | Detección de fallos repetidos, diagnóstico con `CoreEyes` y logs reales, y planes de recuperación. |
| **`test_kernel.py`** | Control Store / Kernel | Almacén transaccional `control.sqlite3`, atomicidad de autorizaciones, puertos y buzón de voz. |
| **`test_mind.py`** | Mind | Extracción de intenciones (`Intent`), resolución de capacidades y proveedores, y flujo conversacional. |
| **`test_pixy.py`** | Pixy | Gobernanza estricta, confirmación binaria (`y`/`n`), emisión de `DecisionReceipt` y revisión de contratos. |
| **`test_voice.py`** | Voice | Restricción de red externa, DNS pineado, validación TLS, gestión de `secrets.env` y cliente Telegram. |

**Ejecución estándar:**
```bash
python -m pytest -v
# o bien
make test
```
**Resultado canónico actual:** 264 pruebas pasando al 100% (0 errores, 0 fallos).
