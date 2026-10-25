# Operación y migración

## Configuración necesaria

El despliegue usa un host Linux con Docker Engine y Compose. Los contenedores se identifican como `khai_core` y `khai_docker_proxy`. Ejecute un Core y un proxy por directorio de datos.

## Despliegue activo

La instancia actualmente activa utiliza:

| Elemento | Valor |
| --- | --- |
| VM Azure | `vm-carlosperales` |
| Ruta remota | `/opt/khai` |
| IP pública actual | `20.237.211.250` |
| Core HTTPS | `https://20.237.211.250:8443` |
| Webhook de Telegram | `https://20.237.211.250:8443/webhook` |
| Proveedores activos | Gemini (`GEMINI_API_KEY` y `GEMINI_API_KEY_FALLBACK_2`) |
| Proveedores desactivados | Azure OpenAI; `GEMINI_API_KEY_FALLBACK` |

El NSG de la VM permite SSH por el puerto `22`, tráfico web existente y el puerto
TCP `8443` mediante la regla `allow-khai-webhook-8443`. El puerto `8001` del
proxy no se publica externamente.

El Core utiliza los certificados presentes en `data/certs/cert.pem` y
`data/certs/key.pem` para atender HTTPS. El certificado activo es autofirmado
para la IP pública actual y se entrega a Telegram al registrar el webhook.
Si la IP pública cambia, genere un certificado nuevo, actualice el webhook y
compruebe la regla de red antes de reiniciar el Core.

| Variable | Uso |
| --- | --- |
| `KHAI_HOST_ROOT` | Ruta absoluta del proyecto en el host Linux. El registro anterior indica `/opt/khai`; confirme su instalación. |
| `PROXY_TOKEN` | Secreto compartido por Core y proxy, mínimo 32 caracteres. |
| `TELEGRAM_WEBHOOK_SECRET` | Secreto del webhook, mínimo 32 caracteres; también debe registrarse en Telegram. |
| `TELEGRAM_BOT_TOKEN` | Token del bot, en `secrets.env`. |
| `TELEGRAM_USER_ID` | Único usuario autorizado, en `secrets.env`. |
| `KHAI_DB_PATH` | SQLite del Core; Compose fija `/app/data/openclaw.db`. |
| `PROXY_URL` | Base del proxy; Compose usa `http://docker_proxy:8001`. |

Las claves Gemini, Azure y OpenAI permanecen en `secrets.env`. El proxy solo recibe su token; el worker no recibe ninguno de esos secretos.

Genere dos secretos independientes con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Registre cada resultado en su variable correspondiente de `.env`; no los incluya en commits, capturas o informes. `.dockerignore` excluye `.env`, archivos `*.env` y `data` de las imágenes.

El Core utiliza HTTPS en 8443 si encuentra `data/certs/cert.pem` y `data/certs/key.pem`; en caso contrario escucha HTTP en 8000. Para Telegram hace falta una URL HTTPS válida, mediante esos certificados o un reverse proxy correctamente configurado. No basta con publicar HTTP.

## Actualización de la instancia activa

La revisión actual se activó en la VM de Azure. Core y proxy se reconstruyen y
reinician juntos; no se debe reemplazar solo uno de ellos porque comparten el
protocolo de ejecución.

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl -kfsS https://127.0.0.1:8443/docs >/dev/null
curl -kfsS https://127.0.0.1:8443/openapi.json >/dev/null
```

Antes de actualizar, cree un respaldo coherente de `data`, `.env`, `secrets.env`,
certificados y código. No copie una SQLite en caliente ignorando sus archivos
WAL/SHM. Mantenga `data/voice/secrets.env` con permisos restringidos después de
la migración.

## Migración desde la revisión anterior

Esta sección es un procedimiento para un despliegue futuro; la edición local de código no ejecuta estos pasos.

1. Detenga la entrada de nuevas tareas y examine los agentes históricos y sus efectos. Los contenedores antiguos no pasan a estar administrados por el nuevo proxy automáticamente.
2. Detenga Core y proxy anteriores antes de copiar datos. Con todos los escritores parados, respalde `data`, `.env`, `secrets.env`, certificados y la revisión de código. Si usa SQLite en caliente, utilice su API de backup; no copie solo el archivo principal ignorando WAL.
3. Actualice código del Core y del proxy juntos. Complete `.env` con `KHAI_HOST_ROOT`, `PROXY_TOKEN` y `TELEGRAM_WEBHOOK_SECRET`; conserve las credenciales existentes de `secrets.env`.
4. Compruebe `docker compose config --quiet`. Este comando valida configuración, no construye ni inicia servicios. Evite imprimir la configuración expandida si contiene secretos.
5. Tras aprobar la activación, construya e inicie con `docker compose up -d --build`. Compruebe logs, montajes y conectividad privada antes de habilitar el bot.
6. Registre en Telegram el mismo `TELEGRAM_WEBHOOK_SECRET`. `scripts/set_webhook.py` envía `secret_token` y usa el certificado de `data/certs/cert.pem`; ese script hace una llamada real a Telegram y se ejecuta deliberadamente al activar.
7. Compruebe `/tasks` y revise los objetivos históricos `UNKNOWN`. Ejecute primero una tarea offline con un resultado conocido y verifique su artefacto.

Para una comprobación posterior, use:

```bash
docker compose ps
curl -kfsS https://127.0.0.1:8443/docs >/dev/null
curl -kfsS https://127.0.0.1:8443/openapi.json >/dev/null
```

Desde un equipo externo, verifique también que `https://IP_PUBLICA:8443/docs`
responda y consulte `getWebhookInfo` en Telegram. Un `pending_update_count` en
cero confirma que Telegram pudo entregar las actualizaciones pendientes; no
sustituye una prueba funcional desde el bot.

La inicialización agrega tablas sin borrar las tareas y los eventos anteriores. Si encuentra el antiguo esquema de reglas sin `topic`, conserva esa tabla como legado antes de crear la actual. Las reglas antiguas no se convierten automáticamente en evidencia validada.

Para volver a la versión anterior: detenga ambos servicios nuevos, restaure la revisión y el respaldo coherente de sus datos, restablezca el webhook correspondiente y después reinicie. No ejecute simultáneamente dos versiones sobre el mismo directorio.

## Flujo de una tarea

### Comandos de usuario

`/help` y `/?` muestran la ayuda completa. `/pixy <mensaje>` y `/p <mensaje>`
abren el canal directo con Pixy. Por ejemplo:

```text
/p diagnóstico
/p explica el último error
```

Pixy consulta `Heart` para el estado y `Senses` para logs u otra evidencia
observacional; el diagnóstico se documenta en Heart antes de responder. Si una
acción requiere autorización, Pixy termina con `apruebas: y/n`; responde
simplemente `y` o `n`.

1. Envíe `/make objetivo` o una solicitud natural.
2. Consulte `/tasks` para obtener el `task_id`.
3. Si conoce un archivo esperado, registre antes de ejecutar `/expect TASK_ID ruta_relativa SHA256`. Si la ejecución ya empezó se rechaza el cambio del criterio.
4. Cuando se solicite permiso, lea `/artifact TASK_ID`. Apruebe o deniegue usando el token exacto de esa propuesta. El token vence en hasta 15 minutos y nunca supera el plazo del objetivo.
5. Si falta un criterio automático, examine el output y los archivos del workspace. Envíe `/verify TASK_ID EXECUTION_ID success` o `failure`. La verificación se atribuye al usuario y a esa ejecución.
6. Use `/cancel TASK_ID` para detener el trabajo. `CANCELLING` significa que falta confirmar la cancelación; `CANCELLED` confirma el resultado. Un servicio AGENT puede cancelarse incluso si su resultado ya se verificó. Cancelar detiene el proceso; no revierte efectos ya realizados.

El criterio automático actual compara SHA256 de archivos de hasta 64 MiB. No hay un verificador HTTP o semántico automático. Un servicio iniciado necesita verificación del usuario y puede detenerse posteriormente; ambas observaciones se conservan.

Los outputs están en `data/workspaces/TASK_ID/SHA256/`. Ya no se expone globalmente `data/public` por HTTP. `/artifact` muestra el código; para revisar outputs se utiliza el acceso del operador al workspace.

## Límites y recuperación

- Por objetivo: 4 generaciones, 4 ejecuciones, 16 intentos de proveedor y 30 minutos de plazo por defecto.
- Por llamada LLM: hasta 60 segundos, 100000 caracteres de prompt y hasta 8192 tokens de salida. Cada intento de failover consume el mismo presupuesto; los SDK no añaden reintentos ocultos.
- SAFE y GOD: hasta 120 segundos de ejecución, sujetos también al plazo del objetivo.
- AGENT: permanece supervisado hasta el plazo del objetivo; no es un daemon indefinido ni publica puertos automáticamente.
- Worker: 1 CPU, 512 MiB de RAM, 128 procesos, raíz de solo lectura, tmpfs de 64 MiB y rotación de logs.
- Construcción de dependencias: timeout de 180 segundos, en una cola de lanzamiento independiente para no bloquear la supervisión de contenedores activos.

Los valores operativos indicados están definidos en el código de esta revisión. No los confunda con los intervalos históricos de `config.yaml`, que no configuran el nuevo coordinador.

Si el Core pierde la respuesta de un envío, consulta o repite el mismo intento. Si falta un contenedor cuya ejecución pudo empezar, registra `UNKNOWN`; inspeccione el sistema externo antes de crear otro objetivo. Si hay efectos parciales posibles, no se reintenta de forma automática.

Los checkpoints `run_step` son archivos locales de la habilidad. Un cierre entre el efecto externo y el guardado del checkpoint no es una transacción distribuida. La aprobación de una operación vuelve a ejecutar el script y conserva esos checkpoints; revise los efectos antes de autorizarla.

Los objetivos `UNKNOWN` y los registros históricos se inspeccionan fuera del agente. No existe un comando que los vuelva a ejecutar ignorando la incertidumbre.

Los targets históricos `make backupdb` y `make restoredb` solo abarcan el formato anterior del Core y no constituyen un respaldo completo de esta revisión. Utilice el procedimiento coherente de copia con los servicios detenidos descrito arriba.

## Observabilidad y mantenimiento

Consulte `/tasks`, `/rules`, los logs de ambos servicios y `control_events`. El diagnóstico basal de Heart permanece activo; el antiguo ciclo de promoción automática de reglas se sustituye por LearningLedger. El historial de evidencia no se borra con `/new` ni `/compact`; esos comandos muestran ayuda.

### Trazabilidad Distribuida (`trace_id`)
Cada solicitud entrante por Telegram u otro transporte recibe inmediatamente un `trace_id` único (ej: `req_<update_id>-<timestamp>-<rand>`). Mediante `contextvars`, este identificador se propaga automáticamente por toda la tubería (`Voice` -> `Mind` -> `Coordinator` -> `Hand` -> `Senses` -> `Pixy` -> `Heart`).
Los logs de `openclaw.log` se escriben con el formato `%(asctime)s %(levelname)s [%(trace_id)s] %(name)s %(message)s`, permitiendo aislar el camino completo de cada ejecución e incidente.

### Autorizaciones Unificadas (Security Context Broker)
Pixy actúa como la única fuente de verdad para autorizaciones sensibles, utilizando el `trace_id` como clave unificada:
- Las autorizaciones se emiten y consultan directamente con `trace_id`.
- Hand y Heart consultan a Pixy (`pixy.check_authorization(trace_id, capability)`).
- Al ejecutarse la acción mutante, el permiso se marca atómicamente como `consumed` para prevenir ataques de repetición (anti-replay).

### Diagnóstico por Sesión y Boot Epoch (Heart)
Heart clasifica los errores de `openclaw.log` discriminando por el hito de arranque (`boot_time`):
- Los errores ocurridos en sesiones o despliegues anteriores no mantienen el sistema en estado falso `degraded` tras un reinicio.
- Las fallas a nivel de lógica o tarea (que no requieren reinicio de contenedores Docker) son analizadas por Pixy para ofrecer al usuario una explicación técnica clara y pasos de solución accionables (reintentar tarea, ajustar parámetros o revisar artefactos) en lugar de un críptico `Plan: None`.

### Normalización Resiliente de Manifiestos
El coordinador sanitiza automáticamente los manifiestos generados por el LLM:
- Remueve delimitadores markdown (```` ```json ````).
- Desempaqueta estructuras anidadas (`manifest`, `data`, `result`).
- Convierte capacidades expresadas como cadenas separadas por comas a listas normalizadas (`Network`, `FileSystem`, `Database`).
- Emplea el objetivo de la solicitud como fallback seguro para evitar excepciones `ValueError: Manifiesto inválido`.

Los reinicios del host y el control arbitrario de Docker se realizan fuera del agente. Los marcadores LLM de Pixy (`DOCKER_CONTROL`, `INJECT_ENV`, `ADD_RULE`) ya no ejecutan mutaciones.

No elimine archivos de ejecuciones activas. La retención automática de imágenes, workspaces y registros sigue pendiente; supervise el disco y defina cuotas y respaldos del host antes de un uso sostenido. Los montajes no implementan por sí solos una cuota de disco.

## Validación antes de producción

La suite local simula Docker, Telegram y modelos. En un entorno Linux de pruebas, antes de usar credenciales reales, compruebe: montajes y usuario efectivos; red deshabilitada; límites CPU/RAM/procesos; rechazo de secretos; timeout con eliminación; reinicio Core/proxy; recibos duplicados; aprobación GOD y cierre de AGENT. Confirme la cuota de disco y las reglas de salida de red del host.

