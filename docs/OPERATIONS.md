# Operación y migración

## Configuración necesaria

El despliegue usa un host Linux con Docker Engine y Compose. Los contenedores se identifican como `khai_core` y `khai_docker_proxy`. Ejecute un Core y un proxy por directorio de datos.

## Despliegue remoto y política de activación

El despliegue de la revisión actual está congelado mientras continúan las
mejoras. No ejecutar los comandos de activación de esta guía hasta completar la
validación de cierre y comprobar que el servicio de la revisión anterior no se
rompe. `README.md` y `.agents/GEMINI.md` son las fuentes de verdad principales;
esta guía describe el procedimiento técnico futuro.

Los datos siguientes describen la última revisión remota documentada, **no una
verificación de la revisión local del 26 de octubre de 2026**. Los cambios
locales de las fases F0-F5 no se han activado en Azure. Confirmar estado de Git,
configuración y salud de la VM solo como parte de una futura activación aprobada.

La última instancia documentada utiliza:

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
| `KHAI_DB_PATH` | Override para herramientas aisladas; el Core usa `DATA_DIR/control.sqlite3`. |
| `PROXY_URL` | Base del proxy; Compose usa `http://docker_proxy:8001`. |

Las claves Gemini, Azure y OpenAI permanecen en `secrets.env`. El proxy solo recibe su token; el worker no recibe ninguno de esos secretos.

Genere dos secretos independientes con `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Registre cada resultado en su variable correspondiente de `.env`; no los incluya en commits, capturas o informes. `.dockerignore` excluye `.env`, archivos `*.env` y `data` de las imágenes.

El Core utiliza HTTPS en 8443 si encuentra `data/certs/cert.pem` y `data/certs/key.pem`; en caso contrario escucha HTTP en 8000. Para Telegram hace falta una URL HTTPS válida, mediante esos certificados o un reverse proxy correctamente configurado. No basta con publicar HTTP.

## Actualización de la instancia activa

La revisión anterior se activó en la VM de Azure. Core y proxy se reconstruyen y
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

1. Detenga la entrada y ambos servicios. Este corte no importa tareas ni autorizaciones históricas.
2. Preserve únicamente `data/voice/secrets.env` y `data/mind/identity.yaml` si aplica el reset aprobado. No traslade autorizaciones antiguas al store nuevo.
3. Retire bases legacy y ejecute `python -m scripts.migrate_architecture`; debe crear `data/control.sqlite3` o rechazar el corte si detecta una base antigua.
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

La inicialización es limpia y no adopta tablas antiguas. Las reglas o permisos históricos no se convierten en evidencia ni autoridad nueva.

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

En el código local, una tarea Network sin servicio declarado puede quedar en
`WAITING_SERVICE_APPROVAL`. Revise el destino, método, efecto y referencia de
credencial con `/service show HASH`; `/service approve HASH` y
`/service deny HASH` resuelven la propuesta. Por ahora también las lecturas
requieren esta aprobación de descriptor. Si falta la clave, `WAITING_SECRET`
solicita `/api VALOR` en un chat privado del propietario (o `/us` y `/pas`
según el tipo). La captura se intercepta antes del inbox ordinario y no debe
enviarse como mensaje libre a Mind. El recibo reanuda la misma tarea; el valor
no aparece en el estado del objetivo. Evite copiar valores reales en reportes.

El inbox y outbox ordinarios son durables. Una entrega Telegram que quedó
`unknown` tras una caída no se reenvía automáticamente: el resultado de la
tarea y el recibo de entrega se inspeccionan por separado.

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

### Autorizaciones del control común
`trace_id` solo correlaciona eventos: no es una credencial ni concede autoridad.
Las decisiones de Pixy se ligan a un sujeto, operación, versión o hash concreto
mediante `authorization_id` y recibos durables en `data/control.sqlite3`.
Heart propone planes, Pixy autoriza y Hand consume órdenes acotadas; un estado
`UNKNOWN` requiere reconciliación antes de reintentar.

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

Tras los últimos cambios solo pasaron pruebas acotadas: 30 de arquitectura y
12 de contratos F3. La suite completa se reservó para el cierre, por petición
del propietario. No se midieron embeddings ni se probó composición ejecutada.
En un entorno Linux de pruebas, antes de usar credenciales reales, compruebe:
montajes y usuario efectivos; red deshabilitada; límites CPU/RAM/procesos;
rechazo de secretos; timeout con eliminación; reinicio Core/proxy; recibos
duplicados; aprobación GOD y cierre de AGENT. Confirme la cuota de disco y las
reglas de salida de red del host.
