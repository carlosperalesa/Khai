# Registro de despliegue de Khai

Fecha de ejecución: 2026-10-24  
VM: `vm-carlosperales`  
Ruta remota: `/opt/khai`

## Activación actual

La revisión documentada en este archivo quedó activada en la VM con:

- IP pública: `20.237.211.250`.
- Webhook: `https://20.237.211.250:8443/webhook`.
- Core atendiendo HTTPS en `8443`.
- Proxy Docker accesible únicamente dentro de la red de Compose.
- Regla NSG `allow-khai-webhook-8443` para permitir el webhook de Telegram.

El webhook anterior en `https://165.232.142.89:8443/webhook` quedó obsoleto
después del cambio de IP y fue reemplazado en Telegram.

## Acciones realizadas

1. Verificada la VM en Azure:
   - Ubuntu 24.04 LTS.
   - Arquitectura ARM64 (`aarch64`).
   - 2 vCPU.
   - Aproximadamente 1 GiB de RAM y 2 GiB de swap.
   - 64 GiB de disco, con aproximadamente 49 GiB libres después de la instalación.
2. Instalados `docker.io`, Docker Compose v2, Git y Make mediante APT.
3. Habilitado e iniciado el servicio Docker.
4. Transferido el proyecto a `/opt/khai` mediante SSH.
5. Excluidos del paquete de código:
   - `.git`
   - `data/openclaw.db`
   - Logs locales
   - `publisher.tar.gz`
6. Transferido `secrets.env` por SCP/SSH, sin imprimir su contenido.
7. Aplicados permisos `600` a `/opt/khai/secrets.env`.
8. Creado `.env` con `KHAI_HOST_ROOT` y secretos de runtime generados
   aleatoriamente; los valores no se registraron.
9. Construidas correctamente las imágenes Docker para ARM64.
10. Iniciados los servicios:
    - `openclaw_core`
    - `openclaw_docker_proxy`
11. Recreados los contenedores después de actualizar `secrets.env`.
12. Generados `PROXY_TOKEN` y `TELEGRAM_WEBHOOK_SECRET` en `.env`, con
    permisos restringidos.
13. Generados `data/certs/cert.pem` y `data/certs/key.pem` para HTTPS en la
    IP pública actual.
14. Abierto el puerto TCP `8443` en el NSG de la VM.
15. Actualizado el webhook de Telegram a la IP pública actual.

## Verificaciones

- Docker Compose reportó ambos servicios en estado `running`.
- El orquestador FastAPI inició correctamente en el puerto `8000`.
- El endpoint `/docs` respondió HTTP 200.
- El endpoint `/openapi.json` respondió HTTP 200.
- El servicio Docker quedó habilitado para iniciar con el sistema.
- La API de Telegram respondió correctamente a `getMe`.
- El bot identificado por Telegram es `Khaiiabot`.
- `getWebhookInfo` respondió correctamente:
  - Webhook configurado en `https://20.237.211.250:8443/webhook`.
  - Actualizaciones pendientes: `0`.
  - Certificado personalizado configurado.

## Estado actual

```text
openclaw_core          running
openclaw_docker_proxy  running
```

## Observaciones

- La VM tiene solamente aproximadamente 1 GiB de RAM; es adecuada para pruebas y carga ligera.
- Las imágenes construidas ocupan aproximadamente 4 GiB.
- No se imprimieron ni registraron tokens, claves API ni el contenido de `secrets.env`.
- El Core inicia en HTTPS por la presencia de certificados en `data/certs`.
- Los puertos publicados por Compose son `8000` y `8443`; el webhook utiliza
  `8443` y el acceso externo depende de las reglas de red de Azure.
- Para producción o tareas pesadas se recomienda una VM con al menos 4 GiB de RAM.

## Verificación de proveedores de IA

Se verificaron las variables dentro de `openclaw_core` y se realizaron llamadas mínimas sin registrar claves ni respuestas sensibles:

- `GEMINI_API_KEY`: operativo, HTTP 200.
- `GEMINI_API_KEY_FALLBACK_2`: operativo, HTTP 200.
- `GEMINI_API_KEY_FALLBACK`: comentada por ser inválida.
- Azure OpenAI: variables comentadas; no existe un deployment operativo con la
  cuota disponible actual.
- OpenAI: `OPENAI_API_KEY` no está cargada dentro del contenedor.

Khai dispone de dos claves Gemini operativas para el fallback.

## Recurso Azure AI `khaipixy`

Se creó el recurso `khaipixy` en `RG-CARLOSPERALES`, región `westus`, tipo `OpenAI`, SKU `S0`.

- Endpoint: `https://khaipixy.openai.azure.com/`
- Estado del recurso: `Succeeded`.
- No se desplegó ningún modelo porque la suscripción tiene cuota `0` TPM para `gpt-4.1-nano` en `GlobalStandard`.
- El intento alternativo con SKU `Standard` no es compatible con esa versión del modelo.
- No se conectó Khai a este recurso porque todavía no existe un deployment
  utilizable.

Azure OpenAI no ofrece un nivel gratuito garantizado para este recurso; el consumo de modelos se factura por uso. Se debe solicitar cuota desde Azure antes de desplegar el modelo y conviene configurar un presupuesto/alerta de consumo.

Se volvió a solicitar el deployment mínimo `gpt-4.1-nano` con capacidad 1K TPM para dejar Azure OpenAI como fallback de Gemini. Azure volvió a rechazarlo porque la cuota disponible continúa en `0 TPM`. Las variables de Azure OpenAI quedaron comentadas en `secrets.env`, por lo que no se intenta utilizar ese proveedor.

## Validación posterior a la activación

- `docker compose config --quiet`: correcto.
- Imágenes ARM64 reconstruidas correctamente.
- `openclaw_core`: `Up`.
- `openclaw_docker_proxy`: `Up`.
- `/docs` y `/openapi.json`: HTTP 200.
- Gemini probado desde el contenedor Core: HTTP 200 con candidatos.
- Telegram actualizado al webhook de la IP actual.
- `pending_update_count`: `0` después de abrir el puerto `8443`.

Antes de la activación se creó un respaldo remoto en
`/opt/khai/backups/khai-20261024T175214Z.tgz`, junto con copias de
`secrets.env` y `.env`. Los respaldos contienen secretos y deben conservarse
con permisos restringidos; no deben incorporarse al repositorio.
