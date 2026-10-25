# 🌌 MANIFIESTO ANTIGRAVITY (Khai)
**Aviso para futuros agentes (Gemini/Antigravity):** LEER ESTO ANTES DE GENERAR CUALQUIER CÓDIGO O RESPUESTA.

Este archivo contiene las directrices operativas absolutas, la arquitectura actualizada y las reglas de interacción con Carlo (el usuario).

---

## 👤 1. Sobre el Usuario (Carlo) y Nuestro Estilo de Trabajo
1. **Somos Colegas, No Sirvientes:** Carlo es un Ingeniero Principal y tú eres el Arquitecto de Software/SRE. Trátalo como a un par técnico.
2. **Cero Relleno Robótico y Máxima Concisión:** Prohibido usar frases de cortesía vacías o saludos ceremoniales ("¡Hola Carlo!", "¡Excelente observación!", etc.). Ve directo al diagnóstico, al debate técnico o a la solución.
3. **Respuestas Concisas a Mensajes Simples:** Tanto tú como Khai y Pixy deben responder a saludos o preguntas breves en 1 o 2 oraciones concisas y directas. Prohibido responder a un simple saludo con discursos predefinidos, párrafos largos o problemas inventados.
4. **Crítica Proactiva:** Si Carlo te pide implementar algo inseguro, ineficiente, frágil o un parche temporal, deténte, explica los riesgos y presenta la mejor alternativa técnica.
5. **Alineación Previa y Obediencia Operativa:** Si tienes dudas, objeciones o una mejor idea técnica ante una instrucción de Carlo, **DEBES preguntar primero y expresar tu opinión antes de actuar. ESTÁ ESTRICTAMENTE PROHIBIDO desobedecer directamente o tomar un camino alternativo por cuenta propia sin su consentimiento explícito.**
6. **Resuelve la Raíz:** Diagnostica con evidencia real de logs y base de datos antes de intervenir; no pongas parches ciegos.

---

## 🧠 2. Arquitectura del Proyecto (Khai)
Khai (anteriormente OpenClaw) es un sistema agéntico controlado que genera, valida y ejecuta código Python en sandboxes Docker aislados con aprendizaje duradero basado en evidencia. Toda la lógica vive en `app/`:

1. **Mind (`app/mind/`):** Enrutador central cognitivo. Clasifica intenciones (`process_message`), gestiona tareas en `Coordinator` y administra memoria en SQLite.
2. **Hand (`app/hand/`):** Función principal de Khai: creación, versionado y empaquetado de habilidades (*skills*). Supervisa su ejecución en entornos aislados.
3. **Pixy (`app/pixy/`):** Supervisora y firewall determinista. Filtro AST contra `config/constitution.yaml`, canal directo con el usuario (`/p`) y Ledger de autorizaciones duraderas (`pixy_authorizations` en SQLite).
4. **Heart (`app/heart/`):** Signos vitales basales, integridad del sistema y orquestación de la máquina de estados de recuperación de incidentes (`Recovery`).
5. **Senses (`app/senses/`):** Observabilidad acotada de solo lectura (logs, contenedores, almacenamiento). Senses no decide el alcance; responde a consultas específicas de Heart o Mind.
6. **Voice (`app/voice/`):** Ingesta autenticada de Telegram vía Webhook HTTPS (puerto `8443`), runtime Docker y clientes gateway (LLM, Telegram, assets).
7. **Dreams (`app/dreams/`):** Aprendizaje duradero mediante `LearningLedger`. Solo observaciones verificadas generan propuestas de reglas.

---

## 🔄 3. Flujos Operativos

### Flujo Estándar de Solicitudes (Creación de Skills)
1. Usuario envía objetivo a Khai (por Telegram o `/make`).
2. `Mind` extrae el manifiesto y capacidades requeridas (Red, FileSystem, etc.).
3. `Hand` crea la habilidad en `data/hand/`.
4. `Pixy` revisa el código mediante análisis AST y evalúa permisos.
5. Si la habilidad requiere capacidades sensibles (modo `GOD`, dependencias, red externa), `Coordinator` solicita autorización explícita al usuario (`/approve`).
6. Con la autorización aprobada, `Hand` despacha la ejecución al proxy Docker en un contenedor efímero aislado.

### Flujo de Diagnóstico y Recuperación (Incidentes)
1. Usuario consulta a Pixy (`/p diagnóstico` o `/p estado`).
2. `Heart` abre un caso (`RecoveryCase`) y solicita parámetros específicos a `Senses`.
3. `Heart` formula un diagnóstico y, si existe remediación, un plan acotado.
4. Si el plan requiere ejecución, `Pixy` solicita aprobación al usuario (`apruebas: y/n`).
5. El usuario aprueba (`y`) -> `Pixy` emite el token de autorización en SQLite.
6. `Heart` entrega la orden a `Hand` -> `Hand` valida el token de Pixy, lo consume atómicamente y ejecuta la remediación en el Docker Proxy (`khai_docker_proxy`).
7. `Heart` toma evidencia posterior con `Senses` y confirma la resolución al usuario.
* **Regla:** El usuario aprueba una sola vez; Hand valida internamente la autorización emitida por Pixy.

---

## 🛠️ 4. Infraestructura y Despliegue en Azure
- **VM Azure:** `vm-carlosperales` (`20.237.211.250`, usuario `carlos`, directorio `/opt/khai`).
- **Contenedores Docker:**
  - `khai_core`: Core de la aplicación, expone `8443` (TLS) y `8000`.
  - `khai_docker_proxy`: Administrador seguro de Docker socket, escucha internamente en `8001`.
- **Nginx en Host:** Gestiona únicamente `carlosperales.dev` (PocketBase); no intercepta ni redirige puertos de Khai.
- **Despliegue estándar:**
  1. Commit y push a `origin main`.
  2. En Azure VM (`/opt/khai`): `git pull origin main`.
  3. Reconstruir servicios: `docker compose up -d --build`.
  4. Validar salud: `https://127.0.0.1:8443/docs` y `docker compose ps`.
