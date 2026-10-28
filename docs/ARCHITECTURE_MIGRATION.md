# Migración de arquitectura de Khai

La implementación actual vive en `app/`. Los imports de `modules/` y
`orchestrator.py` son adaptadores de despliegue; no deben usarse para código nuevo.
Se conservan porque todavía forman parte del contrato de compatibilidad del
despliegue.

El trabajo local del 26 de octubre de 2026 no está confirmado en la VM. Antes
de migrar, revisar los cambios sin commit, ejecutar la validación final y
comprobar explícitamente la revisión que corre en el host.

## Propiedad y ejecución

- Mind interpreta y coordina; Brain recibe una interfaz de Voice para el LLM.
- Identity carga `data/mind/identity.yaml`; la migración conserva literalmente
  el contenido anterior de `data/memory/agent.md`.
- Pixy realiza preflight, revisión y auditoría de artefactos. Las reglas nuevas
  son orientación aprobada por el usuario; no modifican `default_rules.yaml`.
- Hand conserva borradores `.nombre`, versiones activas, ZIP numerados,
  hashes y operaciones registradas en `data/control.sqlite3`. Una restauración se audita
  otra vez y crea una identidad nueva; rollback de v2 a v1 produce v2.1.
- Voice posee credenciales, transportes, Runtime y AssetGateway. El motor Docker
  ahora está en `app/voice/runtime/docker_engine.py`; el proxy solo lo expone.
- Dreams registra evidencia por ejecución, skill, versión y hash. Tres resultados
  verificados con tasa suficiente pueden producir una propuesta, nunca activarla.
- Heart separa Beat, VitalSigns y Diagnosis; Senses usa adaptadores de lectura
  para sus quince observadores. No recibe shell ni socket Docker.

Los eventos centrales son resúmenes con IDs. Decisiones, presupuestos y órdenes
comparten `data/control.sqlite3`, pero conservan propietarios lógicos y puertos
acotados. Un outbox durable conecta el Core con stores externos sin fingir una
transacción distribuida.

## Migrar y arrancar

1. Detener bot y proxy. No ejecutar dos versiones contra los mismos datos.
2. Instalar las dependencias de `requirements-dev.txt` para ejecutar las pruebas.
3. Preservar `data/voice/secrets.env` y `data/mind/identity.yaml`; retirar las bases legacy según el corte limpio autorizado.
4. Ejecutar `python -m scripts.migrate_architecture` desde la raíz y comprobar `data/control.sqlite3`.
5. Confirmar `data/voice/secrets.env`; se copia una vez desde `secrets.env`, sin
   sobrescribir un archivo existente. No imprimir su contenido.
6. Mantener `.env` para `PROXY_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` y `KHAI_HOST_ROOT`.
   Los secretos de proveedores se leen desde Voice, no se inyectan a workers.
7. Ejecutar las pruebas y reconstruir ambos contenedores con el Compose actualizado.

No existe importación automática de bases heredadas ni doble escritura. El
inicializador rechaza bases legacy para impedir fuentes de verdad mezcladas.

Para volver a la revisión anterior, detener ambos servicios y recuperar código y
configuración compatibles. Este corte no conserva datos legacy: el rollback de
código crea estado vacío salvo que el operador mantenga una copia externa propia.

## Docker y LocalRuntime

`KHAI_RUNTIME=docker` utiliza el proxy autenticado. Los workers usan siempre
`--network none`, raíz de solo lectura y únicamente mounts de artefacto, entrada,
runner y workspace. Conservan IDs de ejecución y recibos ante reinicios.

`KHAI_RUNTIME=local` requiere Linux, `/usr/bin/python3`, Bubblewrap y user namespaces
habilitados. Ejecuta en namespaces separados, con entorno vacío, sin red ni
directorios del host, salvo bibliotecas del sistema y el workspace de esa ejecución.
No instala dependencias a petición. No hay fallback a Python sin aislamiento.
En Windows, o sin Bubblewrap, la ejecución local se rechaza expresamente.

Ambos implementan `prepare`, `execute`, `cancel`, `status`, `collect_result` y
`cleanup`. El protocolo de checkpoints y aprobaciones del worker es compartido.
Un reinicio de LocalRuntime marca ejecuciones no confirmadas como `unknown`.

## Servicios externos y archivos

Configurar operaciones en `config/gateways.yaml`. La configuración inicial está
vacía para no conceder servicios implícitos. Cada operación declara URL HTTPS fija,
método GET/POST, parámetros permitidos, tipos de contenido, límite de bytes, timeout
y frecuencia. Las credenciales se nombran por clave de Voice, nunca por valor.
Voice valida DNS público y fija la IP del socket, verifica TLS y rechaza redirects.

Además del YAML heredado, Hand propone descriptores en el registro versionado.
Pixy los presenta con `/service show HASH` y decide con `/service approve HASH`
o `/service deny HASH`; Voice solo expone versiones aprobadas. La tarea fija
servicio, operación y versión, y el gateway verifica esa referencia al atender
el buzón privado del worker. La política futura de lectura automática y
escritura con autorización humana aún no reemplaza este flujo manual.

Una skill usa `from khai_gateway import request` y
`request(service, operation, params)`. El worker intercambia JSON acotado por su
workspace; no recibe claves ni conectividad. Las descargas binarias se realizan
mediante AssetGateway y se guardan en cuarentena.

Pixy revisa el uso de las librerías con el objetivo y las referencias de
servicios; no veta un import de red solo por su nombre. El aislamiento del
worker mantiene `network none`: cualquier comunicación efectiva pasa por Voice.
Los parámetros de una skill reutilizada llegan mediante `khai_input.params`.
El catálogo SkillSpec exige evidencia verificada para promocionar versiones.
La composición tipada solo valida recetas; su ejecución sigue pendiente.

Comandos del propietario:

```text
/fetch SERVICIO OPERACION archivo.ext [JSON_DE_PARAMETROS]
/export TASK_ID archivo.ext
/publish ASSET_ID
/asset ASSET_ID
/unpublish ASSET_ID
```

`/export` acepta un archivo en la raíz del workspace de una ejecución finalizada.
La publicación requiere aprobación, validación de tipo, tamaño e integridad.
AssetGateway entrega solo IDs registrados, comprueba propietario y expiración,
y no expone directorios privados. El canal implementa `send_document(path)`;
Telegram es la implementación incluida.

## Validación y límites operativos

`python -B -m unittest discover -s tests -v` es la suite de regresión prevista;
en este corte no se completó después de los últimos cambios. Las pruebas
acotadas de arquitectura (30) y contratos F3 (12) pasaron. La composición real
y la medición de embeddings locales siguen pendientes; no se deben inferir de
pruebas con contratos o vectores simulados.
Docker, Telegram y LLM se simulan en las pruebas normales; el worker sí ejecuta
un fixture controlado en un proceso separado.

La validación real de Docker, HTTPS, Telegram y proveedores se realiza en el host
de despliegue. La instancia activa se comprueba con `docker compose ps` y los
endpoints `/docs` y `/openapi.json`; los adaptadores de compatibilidad no deben
retirarse sin una migración coordinada de ese host.
