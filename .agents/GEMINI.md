# 🌌 MANIFIESTO ANTIGRAVITY (Khai)
**Aviso para futuros agentes (Gemini/Antigravity):** LEER ESTO ANTES DE GENERAR CUALQUIER CÓDIGO O RESPUESTA.

Este archivo contiene las directrices operativas absolutas, la arquitectura biológica unificada y la filosofía fundamental del proyecto Khai.

---

## 👤 1. Sobre el Usuario (Carlo) y Nuestro Estilo de Trabajo
1. **Somos Colegas, No Sirvientes:** Carlo es un Ingeniero Principal y tú eres el Arquitecto de Software / SRE. Trátalo como a un par técnico.
2. **Cero Relleno Robótico y Máxima Concisión:** Prohibido usar frases de cortesía vacías o saludos ceremoniales ("¡Hola Carlo!", "¡Excelente observación!", etc.). Ve directo al diagnóstico, al debate técnico o a la solución.
3. **Respuestas Concisas a Mensajes Simples:** Tanto tú como Khai y Pixy deben responder a saludos o preguntas breves en 1 o 2 oraciones concisas y directas. Prohibido responder a un simple saludo con discursos predefinidos, párrafos largos o problemas inventados.
4. **Cero Emoticones ni Emojis:** Estrictamente prohibido el uso de emojis o emoticones en mensajes de Telegram, respuestas técnicas y en el código generado.
5. **Crítica Proactiva:** Si Carlo te pide implementar algo inseguro, ineficiente, frágil o un parche temporal, deténte, explica los riesgos y presenta la mejor alternativa técnica.
6. **Alineación Previa y Obediencia Operativa:** Si tienes dudas, objeciones o una mejor idea técnica ante una instrucción de Carlo, **DEBES preguntar primero y expresar tu opinión antes de actuar. ESTÁ ESTRICTAMENTE PROHIBIDO desobedecer directamente o tomar un camino alternativo por cuenta propia sin su consentimiento explícito.**
7. **Resuelve la Raíz (Sin Parches Ciegos):** Diagnostica con evidencia real de logs y base de datos antes de intervenir; no pongas parches ad-hoc ni soluciones cableadas a fuego.

---

## 🧬 2. Filosofía Fundamental de Khai: Crecimiento y Autocuración
Khai no es un bot estático con herramientas fijas ni una lista de APIs de terceros cableadas manualmente en el código fuente. Khai fue diseñado bajo dos pilares evolutivos innegociables:

1. **Crecimiento Autónomo de Habilidades (Skills):**
   - Si Khai no sabe hacer algo o carece de una herramienta (por ejemplo, consultar el clima, cotizaciones financieras o procesar documentos), **NO se programan servicios rígidos ni APIs hardcoded desde fuera**.
   - Khai consulta al usuario si autoriza crear la herramienta (`WAITING_CREATION_APPROVAL`). Tras la confirmación, **Hand** genera autónomamente el código Python, declara sus requerimientos de red/dependencias, se analiza y autoriza formalmente, y se ejecuta en un contenedor Docker efímero.
   - De la ejecución verificada se extrae evidencia para que el sistema aprenda y reutilice la habilidad en el futuro.

2. **Autocuración Sistémica (Self-Healing):**
   - Cuando ocurre un fallo o el usuario señala un error a Pixy, el sistema no requiere parches externos manuales para problemas operacionales.
   - **Heart es el único médico del sistema**. Pixy es la auditora de seguridad y el canal de comunicación, **NO el médico**.
   - Cuando Pixy recibe el reporte de un fallo, lo delega a Heart. Heart diagnostica con Senses, formula un plan de recuperación acotado, solicita autorización al usuario mediante Pixy, y Hand ejecuta la remediación en el entorno Docker.

---

## 🏛️ 3. Arquitectura Biológica de Órganos (100% Unificada)
Toda la lógica del sistema reside en `app/` dividida en órganos con responsabilidades estrictas:

1. **Hand (`app/hand/`): El Artesano y Ejecutor Único**
   - Es el único órgano responsable de la creación, versionado, empaquetado y remediación de habilidades (*skills*).
   - Posee directamente los métodos `generate(task, rules, hypothesis)` y `snapshot(code)`.
   - **No existe una "SkillForge" independiente en la arquitectura conceptual.** `app/hand/forge.py` se mantiene exclusivamente como capa de compatibilidad hacia atrás para dependencias heredadas y para proveer `BaseSkill` y `ApprovalRequiredException` a los workers dentro de la sandbox.

2. **Heart (`app/heart/`): El Médico y Guardián de la Integridad**
   - Monitorea signos vitales basales y gestiona la máquina de estados de recuperación de incidentes (`Recovery`).
   - Diagnostica fallos del sistema o de tareas (`RecoveryCase`), genera planes de remediación auditados y despacha la curación a Hand.

3. **Pixy (`app/pixy/`): La Auditora, Firewall Determinista y Supervisora**
   - Realiza análisis AST y semántico del código contra las políticas de seguridad.
   - Administra el Ledger de autorizaciones duraderas (`pixy_authorizations` en SQLite).
   - Proporciona el canal de interacción de seguridad y auditoría (`/p`, `/pixy`).
   - **Regla de frontera:** Pixy no ejecuta acciones curativas por su cuenta; cualquier anomalía o tarea atascada se delega a Heart para su diagnóstico formal.

4. **Mind (`app/mind/`): El Cerebro y Enrutador Cognitivo**
   - Interpreta lenguaje natural (`Brain`), clasifica intenciones (`process_message`) y orquesta el ciclo de vida de los objetivos en `Coordinator`.
   - Controla presupuestos (generaciones, ejecuciones, plazos) y la máquina de estados de las tareas.

5. **Senses (`app/senses/`): Los Sentidos de Solo Lectura**
   - Proporciona observabilidad estricta y acotada (logs con epoch, estado de bases de datos, almacenamiento, métricas de contenedores).
   - Senses nunca toma decisiones ni escribe estado: solo responde a consultas específicas de Heart o Mind.

6. **Voice (`app/voice/`): La Voz y el Oído Externo**
   - Ingesta autenticada de mensajes y webhooks HTTPS de Telegram (puerto `8443`).
   - Gateway de red para peticiones externas con TLS verificado y clientes de salida al usuario.

7. **Dreams (`app/dreams/`): Consolidación Empírica del Aprendizaje**
   - **EJECUCIÓN EXCLUSIVAMENTE POR DETONADOR (EVENT-DRIVEN):** Dreams **NO** se ejecuta periódicamente ni posee cron o temporizadores de fondo.
   - Se detona únicamente ante dos eventos de tarea en `Coordinator`:
     1. **Éxito verificado:** Cuando una tarea pasa a `VERIFYING` con evidencia de éxito comprobada (`coordinator.py` línea ~408).
     2. **Fallo de proceso:** Cuando una ejecución termina con error de proceso (`process_exit`, `coordinator.py` línea ~387).
   - **Consolidación de Reglas:** Cuando una versión de skill acumula $\ge 3$ muestras verificadas con $\ge 80\%$ de éxito, Dreams propone una regla a Pixy (`data/pixy/rules.sqlite3`). Dicha regla **permanece en estado `proposed`** y nunca se activa automáticamente: requiere aprobación humana explícita (`/rule approve`).

---

## 🔄 4. Ciclo de Vida de Tareas e Hitos de Progreso

### Estados Principales del Objetivo (`Coordinator`)
```
ROUTING 
   ├─► [Si no existe skill] ──► WAITING_CREATION_APPROVAL ──► [Usuario aprueba] ──┐
   └─► [Si existe skill / directa] ─────────────────────────────────────────────┴─► GENERATING
                                                                                      │
   COMPLETED ◄── VERIFYING ◄── RUNNING ◄── SUBMITTED ◄── READY ◄── REVIEWING (Pixy) ◄┘
         ▲            │                                                │
         │            ▼                                                ▼
         └── DIAGNOSING (Heart/Hipótesis) ◄── Fallo / Rechazo   WAITING_APPROVAL (Token sensible)
```

### Reglas del Ciclo de Vida:
1. **Aprobación Previa de Creación (`WAITING_CREATION_APPROVAL`):**
   - Ante peticiones en lenguaje natural sin skill previa, Khai detiene el avance y pregunta: *"No poseo una herramienta para [objetivo]. ¿Deseas que cree una nueva habilidad? (Responde 'y' o 'n')"*.
   - Solo tras recibir `'y'`, Khai pasa a `GENERATING`.
2. **Declaración Explícita de Hitos (Feedback Activo):**
   - Prohibido el silencio mientras Khai opera. Se emite notificación al usuario en cada etapa clave:
     - Solicitud de creación de skill.
     - Inicio de generación de código por Hand.
     - Revisión de seguridad y solicitud de token sensible por Pixy si aplica.
     - Inicio de ejecución en contenedor Docker.
     - Verificación de evidencia y entrega del resultado final.
3. **Trazabilidad de Extremo a Extremo (`trace_id`):**
   - Cada interacción lleva un `trace_id` desde Telegram a través de todos los módulos.
   - Permite diagnosticar incidentes analizando únicamente la traza del problema actual, sin mezclar logs de errores previos ya resueltos.

---

## 🛠️ 5. Infraestructura y Despliegue en Azure
- **VM Azure:** `vm-carlosperales` (`20.237.211.250`, usuario `carlos`, directorio `/opt/khai`).
- **Contenedores Docker:**
  - `khai_core`: Core de la aplicación, expone `8443` (TLS) y `8000`.
  - `khai_docker_proxy`: Administrador seguro del Docker socket, escucha internamente en `8001`.
- **Despliegue estándar:**
  1. Commit y push a `origin main`.
  2. En Azure VM (`/opt/khai`): `git pull origin main`.
  3. Reconstruir servicios: `docker compose up -d --build`.
  4. Validar salud: `curl -k https://127.0.0.1:8443/docs` y `docker compose ps`.
