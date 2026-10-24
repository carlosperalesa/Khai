# Migración de arquitectura de Khai

La implementación de `plan.md` vive en `app/`. Los imports de `modules/` y
`orchestrator.py` son adaptadores de despliegue; no deben usarse para código nuevo.
Se conservan hasta validar esta revisión en producción. `DEPLOYMENT_LOG.md`
describe despliegues anteriores, no acredita el despliegue de esta migración.

## Propiedad y ejecución

- Mind interpreta y coordina; Brain recibe una interfaz de Voice para el LLM.
- Identity carga `data/mind/identity.yaml`; la migración conserva literalmente
  el contenido anterior de `data/memory/agent.md`.
- Pixy realiza preflight, revisión y auditoría de artefactos. Las reglas nuevas
  son orientación aprobada por el usuario; no modifican `default_rules.yaml`.
- Hand conserva borradores `.nombre`, versiones activas, ZIP numerados,
  hashes y operaciones en `data/hand/hand.sqlite3`. Una restauración se audita
  otra vez y crea una identidad nueva; rollback de v2 a v1 produce v2.1.
- Voice posee credenciales, transportes, Runtime y AssetGateway. El motor Docker
  ahora está en `app/voice/runtime/docker_engine.py`; el proxy solo lo expone.
- Dreams registra evidencia por ejecución, skill, versión y hash. Tres resultados
  verificados con tasa suficiente pueden producir una propuesta, nunca activarla.
- Heart separa Beat, VitalSigns y Diagnosis; Senses usa adaptadores de lectura
  para sus quince observadores. No recibe shell ni socket Docker.

Los eventos centrales son resúmenes con IDs; las decisiones y presupuestos se
conservan en Pixy. Un outbox durable conecta las transiciones con el log central
sin fingir una transacción entre bases distintas.

## Migrar y arrancar

1. Detener bot y proxy. No ejecutar dos versiones contra los mismos datos.
2. Instalar las dependencias de `requirements-dev.txt` para ejecutar las pruebas.
3. Ejecutar `python -m scripts.migrate_architecture` desde la raíz.
4. Comprobar `data/architecture-v1.json` y las bases de dominio. La copia previa
   está en `data/backups/before-domain-split.sqlite3`; la base antigua se conserva.
5. Confirmar `data/voice/secrets.env`; se copia una vez desde `secrets.env`, sin
   sobrescribir un archivo existente. No imprimir su contenido.
6. Mantener `.env` para `PROXY_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` y `KHAI_HOST_ROOT`.
   Los secretos de proveedores se leen desde Voice, no se inyectan a workers.
7. Ejecutar las pruebas y reconstruir ambos contenedores con el Compose actualizado.

El importador copia tablas por dominio y registra cada importación. Los objetivos
antiguos pendientes pasan a `UNKNOWN`: requieren inspección antes de una nueva
ejecución. Los datos históricos permanecen en sus tablas de dominio y en el backup;
las reglas históricas no se convierten en nuevas aprobaciones por inferencia.

Para volver a la revisión anterior, detener ambos servicios, recuperar el código
anterior y restaurar **una copia** del backup SQLite mediante la API `backup` de
SQLite. Restaurar juntos código, configuración y datos del respaldo previo. No
sobrescribir datos nuevos sin conservarlos: los resultados posteriores a la
migración no están en el backup anterior.

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

Una skill usa `from khai_gateway import request` y
`request(service, operation, params)`. El worker intercambia JSON acotado por su
workspace; no recibe claves ni conectividad. Las descargas binarias se realizan
mediante AssetGateway y se guardan en cuarentena.

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

`python -B -m unittest discover -s tests -v` cubre regresiones, composición real,
migración y recuperación SQLite, Hand, ZIP corruptos, rollback, permisos, galería,
métricas separadas, contratos de Runtime y rechazo de acceso directo a red.
Docker, Telegram y LLM se simulan en las pruebas normales; el worker sí ejecuta
un fixture controlado en un proceso separado.

La validación real de Docker, Bubblewrap, Telegram y proveedores requiere el host
de despliegue. Hasta completarla no retirar los adaptadores de compatibilidad.
No considerar el paso de pruebas locales como autorización ni prueba de despliegue.
