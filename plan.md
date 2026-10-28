# Plan de recuperación coordinada: Pixy, Heart, Senses y Hand

> **Vigencia (28 de octubre de 2026):** Este documento conserva el diagnóstico
> y las decisiones del plan de recuperación F4 tal como se redactaron. La sección
> «Situación actual del proyecto» y las referencias al commit desplegado describen
> ese corte histórico. El estado operativo vigente es Beta V4 desplegada en
> `vm-carlosperales` (con piloto Dolphin en GCP por WireGuard y comandos
> simplificados `/m`, `/p`, `/health`, `/cancel`|`/stop`, `/restart`,
> `/us`|`/pas`|`/api`) y F5 en curso, documentado en [README.md](./README.md),
> [GEMINI.md](./.agents/GEMINI.md) y
> [el roadmap](./docs/ESTADO_MIGRACION_Y_ROADMAP.md). En el flujo médico vigente,
> Pixy le indica a Heart que hay un problema; Heart investiga con Senses y
> entrega el resultado con la solución propuesta a Pixy para su aprobación (que
> puede o no requerir intervención del usuario).

## 1. Problema

La interfaz actual permite consultar a Pixy con `/pixy` o `/p`, pero la
responsabilidad de salud y recuperación está distribuida de forma incorrecta:

- Pixy consulta directamente a Heart y Senses.
- Pixy genera la explicación del diagnóstico mediante el LLM.
- Heart obtiene signos vitales, revisa integridad y guarda diagnósticos, pero
  no diseña ni coordina una reparación.
- Senses observa, pero no existe un flujo completo de verificación posterior.
- `y/n` se reconoce solo mediante un estado en memoria (`pending_approval`) que
  no está conectado a una orden de reparación concreta.
- Hand es el propietario de la creación y ejecución de skills, pero todavía no
  recibe órdenes de recuperación emitidas por Heart con una autorización
  durable de Pixy.

Como consecuencia, Pixy puede explicar y proponer una solución, pero una
respuesta afirmativa no garantiza que se cree ni ejecute una corrección. Una
reinicialización del proceso también puede perder una aprobación pendiente.

## 2. Situación actual del proyecto

### Código y responsabilidades existentes

| Componente | Situación actual | Dirección acordada |
| --- | --- | --- |
| Pixy | Revisa artefactos, atiende `/pixy` y `/p`, consulta diagnóstico y maneja un `y/n` en memoria. | Ser la interfaz con el usuario, consolidar preguntas, presentar la propuesta de Heart y emitir autorizaciones limitadas. |
| Heart | `run_diagnostic_fast()`, signos vitales, integridad SQLite y persistencia de diagnósticos en `heart.sqlite3`. | Ser dueño de salud, diagnóstico, plan de recuperación, verificación y registro del ciclo completo. |
| Senses | Observación acotada de logs, contenedores y recursos. | Mantenerse en solo lectura y proporcionar evidencia antes y después. |
| Hand | Crea versiones, hashes, checkpoints y ejecuta skills bajo controles existentes. | Ser el único dueño de artefactos y ejecución, incluyendo skills de recuperación solicitadas por Heart. |
| Mind/Brain | Interpreta entradas y produce decisiones estructuradas para el flujo general. | No sustituir el diagnóstico de Heart ni autorizar cambios de infraestructura. |
| Coordinator/ControlStore | Tiene estados, aprobaciones por tarea y recuperación durable para objetivos normales. | Reutilizar patrones de estados, tokens, expiración y auditoría cuando sea adecuado. |
| Docker Proxy/Runtime | Ejecuta operaciones dentro de sus límites. | Ser el canal técnico de ejecución; Heart no debe ejecutar comandos arbitrarios directamente. |

### Implementación publicada

- Repositorio: [carlosperalesa/Khai](https://github.com/carlosperalesa/Khai).
- Rama activa: `main`.
- Último commit desplegado al crear este documento:
  `965affd feat: modernizar interfaz directa de Pixy y Mind`.
- La suite local validada en esa revisión fue de 70 pruebas exitosas.
- La interfaz HTTPS expone el webhook en `/webhook`; `/docs` y
  `/openapi.json` sirven para comprobaciones de salud.

### Alojamiento local

- Workspace de desarrollo:
  `D:\OneDrive\Documentos\ChatGPT\Carlos\Azure`.
- Proyecto:
  `D:\OneDrive\Documentos\ChatGPT\Carlos\Azure\Khai`.
- Intérprete de pruebas:
  `Khai\.venv\Scripts\python.exe`.
- Ejecutar pruebas desde la raíz de `Khai`, por ejemplo:

  ```powershell
  .\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
  ```

No incluir en Git `.env`, `secrets.env`, certificados privados, `data/` ni
artefactos de despliegue. Las credenciales locales y remotas deben permanecer
fuera de esta documentación.

### Alojamiento Azure

- VM: `vm-carlosperales`.
- Usuario SSH operativo: `carlos`.
- Ruta remota del proyecto: `/opt/khai`.
- IP pública observada: `20.237.211.250`.
- Core HTTPS: `https://20.237.211.250:8443`.
- Webhook: `https://20.237.211.250:8443/webhook`.
- Contenedor Core: `khai_core`.
- Contenedor proxy: `khai_docker_proxy`.
- Puerto público del Core: `8443`.
- Puerto interno del proxy: `8001`; no debe publicarse externamente.

Antes de cualquier despliegue futuro:

1. Confirmar que la IP pública y el certificado siguen vigentes.
2. Crear un respaldo remoto coherente de código, `data`, `.env`,
   `secrets.env` y certificados sin imprimir secretos.
3. Actualizar Core y proxy juntos.
4. Ejecutar `docker compose config --quiet`.
5. Ejecutar `docker compose up -d --build`.
6. Verificar `docker compose ps`, `/docs`, `/openapi.json` y los logs recientes.

La guía operativa completa está en
[docs/OPERATIONS.md](docs/OPERATIONS.md). Esta sección no reemplaza esa guía.

## 3. Solución acordada

El flujo de recuperación será:

```text
Usuario
  -> Pixy consolida la pregunta
  -> Heart recibe el caso
  -> Heart pide antecedentes a Senses
  -> Senses observa en solo lectura
  -> Heart diagnostica y diseña una solución
  -> Pixy presenta evidencia, solución, riesgo e impacto
  -> Usuario aprueba o rechaza
  -> Pixy emite una autorización limitada y durable
  -> Heart entrega la orden autorizada a Hand
  -> Hand crea la skill en data/hand/heart/<case_id>
  -> Hand valida y ejecuta la skill
  -> Hand informa el resultado a Heart
  -> Heart pide a Senses una comprobación posterior
  -> Heart registra el resultado y responde a Pixy
  -> Pixy informa al usuario
```

### Responsabilidades

#### Pixy

Pixy será la frontera conversacional y de autorización:

- consolidar la pregunta del usuario;
- pedir a Heart un diagnóstico;
- explicar la respuesta de Heart sin inventar evidencia;
- solicitar aprobación solo cuando el riesgo o el impacto lo exija;
- emitir un permiso limitado a las capacidades propuestas por Heart;
- entregar a Heart la decisión del usuario;
- comunicar el resultado final.

Pixy no debe diagnosticar por su cuenta, diseñar reparaciones ni crear skills.

#### Heart

Heart será el propietario del caso de salud:

- crear el caso y su estado durable;
- solicitar antecedentes a Senses;
- correlacionar evidencia, signos vitales y estado de servicios;
- diseñar una solución estructurada;
- indicar capacidades, objetivo, impacto, reversibilidad y riesgo;
- decidir si una reparación requiere aprobación humana;
- recibir y validar la autorización emitida por Pixy;
- enviar una orden declarativa a Hand;
- solicitar verificación posterior a Senses;
- registrar evidencia, decisiones, autorizaciones, ejecución y resultado.

Heart decide qué solución corresponde, pero no crea artefactos ni ejecuta
comandos arbitrarios.

#### Senses

Senses solo observa. Debe proporcionar, según el caso:

- estado de contenedores y servicios;
- logs recientes;
- métricas de recursos;
- disponibilidad de endpoints;
- estado antes y después de la reparación.

#### Hand

Hand continúa siendo el único propietario de:

- creación de la skill;
- almacenamiento bajo `data/hand/heart/<case_id>`;
- validación del código;
- capacidades y manifiesto;
- hash y versionado;
- checkpoints;
- ejecución;
- resultado y aviso a Heart.

La autorización no debe ser una bandera de texto confiada ciegamente. Hand
debe validar una referencia durable a la autorización de Pixy.

## 4. Contratos de autorización y estados

La autorización de Pixy debe incluir, como mínimo:

```json
{
  "authorization_id": "...",
  "case_id": "correccion-flujo001",
  "issued_by": "pixy",
  "authorized_for": "hand",
  "requested_by": "telegram:<update_id>",
  "capabilities": ["DockerRestart", "HealthCheck"],
  "target": "core",
  "expires_at": "...",
  "nonce": "...",
  "status": "pending"
}
```

Debe almacenarse de forma durable y validar:

- usuario autorizado;
- caso y orden correspondientes;
- capacidades exactas;
- objetivo exacto;
- expiración;
- uso único;
- integridad de la skill;
- ausencia de permisos adicionales.

Estados sugeridos para el caso:

```text
RECEIVED
COLLECTING_EVIDENCE
DIAGNOSED
SOLUTION_PROPOSED
WAITING_USER_APPROVAL
AUTHORIZED
HAND_CREATING
HAND_EXECUTING
VERIFYING
RECOVERED
FAILED
DENIED
UNKNOWN
```

Heart debe registrar las transiciones y conservar la relación entre el caso,
la evidencia, la autorización, la skill, su hash, la ejecución y la
verificación posterior.

## 5. Plan de implementación posterior

1. Mapear los contratos actuales de `Beat`, `Senses`, `Hand`, `ControlStore`,
   `Coordinator` y `Pixy`, preservando compatibilidad con las tareas normales.
2. Crear un almacén durable de casos de Heart y autorizaciones de Pixy en la
   base de datos apropiada, con expiración, estado y uso único.
3. Implementar en Heart la solicitud de evidencia a Senses y el diagnóstico
   estructurado.
4. Implementar en Heart el plan de recuperación, incluyendo capacidades,
   objetivo, riesgo, reversibilidad y necesidad de aprobación.
5. Cambiar Pixy para que solo consolide, explique, solicite aprobación y emita
   la autorización; retirar de Pixy la generación de diagnósticos propios.
6. Conectar `y/n` con la autorización durable de Pixy. `y` debe autorizar
   exactamente el plan presentado; `n` debe cerrar el caso como `DENIED`.
7. Añadir al contrato de Hand una entrada de recuperación emitida por Heart,
   con el identificador de autorización verificable en el encabezado.
8. Crear skills de recuperación exclusivamente mediante Hand en
   `data/hand/heart/<case_id>`.
9. Implementar la notificación Hand -> Heart y la verificación Heart ->
   Senses.
10. Registrar en Heart el ciclo completo y entregar a Pixy un resultado
    estructurado.
11. Añadir pruebas unitarias y de integración para:
    - evidencia solo lectura;
    - diagnóstico y plan de Heart;
    - aprobación y rechazo `y/n`;
    - reinicio entre aprobación y ejecución;
    - expiración y reutilización de autorizaciones;
    - coincidencia exacta de capacidades;
    - creación y ejecución bajo Hand;
    - verificación posterior;
    - resultados `RECOVERED`, `FAILED`, `DENIED` y `UNKNOWN`.
12. Ejecutar `git diff --check`, la suite completa y pruebas focalizadas.
13. Publicar por HTTPS, crear respaldo de la VM, desplegar Core y proxy juntos
    y verificar API, contenedores y logs.

## 6. Criterios de aceptación

- Una consulta del usuario llega a Pixy y termina en un caso durable de Heart.
- Heart obtiene evidencia exclusivamente mediante Senses antes de proponer.
- Pixy muestra una solución concreta con alcance, riesgo e impacto.
- Una aprobación `y` sobrevive al reinicio del proceso y autoriza solo el plan
  presentado.
- Una respuesta `n` no ejecuta ninguna modificación.
- Heart no puede crear artefactos ni saltarse Hand.
- Hand rechaza autorizaciones vencidas, reutilizadas, alteradas o con permisos
  adicionales.
- Toda skill de recuperación vive bajo `data/hand/heart/<case_id>` y queda
  vinculada a un hash y a un caso de Heart.
- Senses verifica el estado posterior sin modificar el sistema.
- Heart registra evidencia, autorización, ejecución, verificación y resultado.
- Pixy informa al usuario si el sistema quedó `RECOVERED`, `FAILED`, `DENIED`
  o `UNKNOWN`, sin afirmar éxito sin evidencia.

## 7. Regla de seguridad

No colocar tokens, claves, identificadores privados de Telegram, certificados
privados ni contenido de `secrets.env` en este archivo. Para retomar el trabajo
se deben consultar los archivos de configuración locales y la VM mediante el
procedimiento operativo, sin copiar secretos al repositorio.
