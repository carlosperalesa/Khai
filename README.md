# Khai — ejecución controlada y aprendizaje con evidencia

Khai es un asistente personal para Telegram que interpreta objetivos, genera o reutiliza código Python y lo ejecuta mediante Voice con DockerRuntime o LocalRuntime aislado. Mantiene estado en SQLite para recuperar el trabajo sin repetir automáticamente operaciones de resultado incierto.

El proyecto conserva nombres históricos de OpenClaw en contenedores y tablas. Su motor de ejecución es propio; no utiliza el Gateway de OpenClaw ni el runtime de Hermes Agent. Ambos proyectos sirvieron como referencias de diseño.

## Estado de esta revisión

La arquitectura actual está implementada en `app/` y desplegada en la VM de
Azure siguiendo [la guía de operación](docs/OPERATIONS.md). Los adaptadores de
compatibilidad se conservan porque todavía forman parte del contrato de
despliegue y no deben usarse para código nuevo.

## Comportamiento actual

- Un `update_id` de Telegram crea como máximo un objetivo durable. Se comprueban el secreto del webhook y la identidad del usuario, también para callbacks.
- Cada objetivo conserva su presupuesto: 4 generaciones, 4 ejecuciones, 16 intentos de proveedor y 30 minutos de plazo por defecto. Los reintentos y las hipótesis alternativas comparten esos límites.
- SkillForge genera código; Pixy lo revisa; Coordinator decide las transiciones; Docker Proxy administra contenedores identificados por `execution_id`.
- La revisión y la aprobación se vinculan al SHA256 del archivo. Cambiar el código invalida la autorización; Pixy no reescribe un artefacto después de revisarlo.
- Cada habilidad dispone de un workspace propio. No recibe la base de datos, los módulos, los secretos ni el socket Docker del Core.
- Salir con código `0` significa que el proceso terminó. El objetivo permanece `UNVERIFIED` hasta comprobar un archivo con un SHA256 esperado por el usuario, o recibir una verificación explícita del usuario.
- Lanzar un servicio AGENT produce `started`, no éxito verificado. Su proceso se supervisa hasta el plazo del objetivo.
- Solo observaciones verificadas alimentan el aprendizaje. Los recibos se deduplican por ejecución; las reglas nuevas pasan por estado `proposed` y aprobación explícita.
- Khai mantiene una personalidad cercana y aprende preferencias explícitas sin convertir conversaciones aisladas o resultados no verificados en memoria permanente.
- Pixy tiene una voz juguetona, pero sus facultades siguen limitadas a revisar, informar, solicitar aprobación y cancelar según las reglas del sistema.

## Componentes

| Componente | Responsabilidad |
| --- | --- |
| Orquestador | Autenticar Telegram y persistir entradas; ofrecer comandos explícitos. |
| Coordinator / ControlStore | Presupuesto, estados, aprobaciones, recuperación y cancelación. |
| Mind | Clasificar intenciones y proponer causas; no despachar contenedores. |
| SkillForge | Generar scripts y guardar artefactos identificados por hash. |
| Pixy | Filtro AST y revisión semántica; el sandbox aplica los límites efectivos. |
| Docker Proxy / Engine | Preparar imágenes, ejecutar, supervisar y reconciliar contenedores. |
| Dreams / Memory | Evidencia, métricas por contexto, propuestas de reglas y reversión. |
| Heart / Senses | Diagnóstico basal y consultas de observabilidad acotadas. |

## Modos de ejecución

| Modo | Uso | Autorización |
| --- | --- | --- |
| SAFE | Script corto en workspace privado. | Revisión de Pixy; aprobación humana si solicita red, instala dependencias o Pixy la requiere. |
| AGENT | Proceso de larga duración, sin puertos publicados automáticamente. | Siempre requiere aprobación humana del artefacto. |
| GOD | Compatibilidad con `DynamicSkill(BaseSkill)` y checkpoints locales. | Siempre requiere aprobación humana; no otorga acceso al host. |

El nombre GOD es histórico: no permite administrar el VPS, modificar el Core ni controlar Docker. La administración del host se realiza por un operador fuera del agente.

## Uso desde Telegram

```text
/help
/?
/pixy diagnóstico
/p explica el último error
/make objetivo
/tasks
/artifact TASK_ID
/approve TASK_ID TOKEN
/deny TASK_ID TOKEN
/cancel TASK_ID
/expect TASK_ID archivo_relativo SHA256_ESPERADO
/verify TASK_ID EXECUTION_ID success
/verify TASK_ID EXECUTION_ID failure
/rules
/rule approve RULE_ID [RULE_ID_SUSTITUIDA]
/rule rollback RULE_ID
```

`/expect` se registra antes de ejecutar. `/verify` se usa sobre el resultado `UNVERIFIED` de una ejecución concreta. Una cancelación en curso se confirma consultando `/tasks`.

`/help` y `/?` muestran el listado completo de comandos. `/pixy` y `/p`
dirigen el mensaje directamente a Pixy. Pixy consulta primero a Heart y, cuando
necesita evidencia, a Senses; después documenta el diagnóstico en Heart y
explica al usuario la evidencia, las hipótesis y la solución propuesta. Las
preguntas de seguridad de Pixy se responden con `y` o `n`.

## Instalación y operación

El despliegue está pensado para Linux con Docker Engine y Compose. Configura los valores de [runtime.env.example](config/runtime.env.example), mantén las credenciales fuera de Git y sigue [la guía de operación](docs/OPERATIONS.md). Core y proxy deben actualizarse juntos porque el protocolo de ejecución cambió.

## Pruebas

```bash
python -m pip install -r requirements-dev.txt
python -B -m unittest discover -s tests -v
```

Las pruebas no requieren credenciales ni un daemon Docker. La validación local
completó la suite del proyecto; la validación real de Docker, HTTPS, Telegram y
proveedores se realiza en el host de despliegue y se describe en
[OPERATIONS.md](docs/OPERATIONS.md).

## Documentación

- [Migración actual y arquitectura corporal](docs/ARCHITECTURE_MIGRATION.md).
- [Arquitectura, contratos y estados](docs/ARCHITECTURE.md).
- [Configuración, migración, operación y recuperación](docs/OPERATIONS.md).
- [Cambios, validación y límites pendientes](docs/IMPLEMENTATION.md).
