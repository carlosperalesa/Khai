# Bloque 3: recuperación y aprendizaje con transacciones compartidas

Fecha de cierre: 29 de octubre de 2026, America/Santiago.

**Estado: implementación de Bloque 3 completada y validada puntualmente (78/78 pruebas).**
Reutiliza la base SQLite existente, puertos y máquinas de estado bajo la opción A
(transacción compartida). No añade LangGraph ni dependencias externas. Sin commits,
push ni despliegue remoto en Azure.

## Decisión y directrices

El propietario eligió la **opción A: transacción compartida mediante los puertos
existentes**. Heart, Pixy, Hand y Coordinator comparten `control.sqlite3` en la
composición real. Se reutilizan esa base, `ControlUnitOfWork`, los puertos y las
máquinas de estados existentes. No añadir LangGraph, dependencias ni otro motor
de ejecución.

La alternativa B, órdenes con recepción y confirmación independientes por órgano,
se discutió como preparación para una futura separación en procesos. No fue la
opción elegida para este bloque.

Aplican [GEMINI.md](../.agents/GEMINI.md), especialmente el trabajo por bloques,
la pausa al consultar y el ahorro de recursos. El propietario reiteró que **no
se debe correr toda la suite durante los bloques**. Ejecutar solo pruebas
puntuales necesarias y reservar la suite completa para el cierre final acordado.

No hacer commit, push, purga, despliegue, reconstrucción ni reinicio remoto.
Preservar los cambios de los bloques anteriores. La sesión no modificó Azure.

## Plan acordado

### Recuperación de Heart

1. Vincular cada autorización al objetivo, revisión, intento anterior e
   instrucciones concretas. Incluir esos datos en el hash aprobado para impedir
   cambios posteriores. Un ID aislado de autorización no permite ejecutar.
2. Consumir la autorización mediante Hand, conservar la evidencia anterior y
   programar el nuevo intento en una sola transacción. Registrar un recibo para
   recuperar su resultado tras un reinicio sin repetir la transición.
3. Rechazar órdenes obsoletas, objetivos cancelados y ejecuciones activas o
   inciertas pendientes de reconciliación. Una recuperación no puede deshacer
   una cancelación ni ampliar silenciosamente permisos de creación o red.
4. Mantener el mismo `task_id`, el consumo histórico y el registro de intentos.
   Añadir únicamente el presupuesto inicial establecido por recuperación
   autorizada: 4 generaciones, 4 ejecuciones y 16 llamadas LLM. No duplicar los
   límites acumulados ni poner los consumos a cero.
5. Mantener el caso abierto durante generación, ejecución y verificación.
   Declarar `RECOVERED` solo con éxito explícito y evidencia de la nueva ejecución.
   `COMPLETED`, un campo ausente o un resultado anterior no bastan.
6. Reconciliar aprobaciones y recibos pendientes al reanudar Heart. Obtener
   mediante Senses evidencia posterior del objetivo y ejecución exactos.

### Aprendizaje y reglas

1. Registrar observaciones, actualizar estadísticas y crear propuestas con una
   misma conexión y transacción. Evitar conexiones de escritura anidadas.
2. Deduplicar por ejecución y rechazar atribuciones contradictorias. Contar
   objetivos distintos y separar evidencia por hipótesis y versión.
3. Vincular las candidatas de memoria con su regla canónica para evitar propuestas
   duplicadas o estados divergentes.
4. Hacer atómicas la aprobación, sustitución y reversión de reglas. Propagar los
   errores; no registrar éxito cuando una operación interna falló.
5. Conservar la aprobación humana obligatoria para activar reglas. Dreams sigue
   funcionando por eventos, sin temporizadores nuevos de aprendizaje.

### Fronteras e interfaces

- La entrada de recuperación exige la orden completa, con el plan exacto aprobado.
- Hand valida y consume la autorización; Coordinator define la transición de la
  tarea; Heart diagnostica, supervisa y verifica; Pixy conserva la decisión.
- Los métodos transaccionales usan la conexión compartida. Las llamadas LLM,
  comunicaciones externas y ejecuciones de contenedores quedan fuera de la
  transacción.
- El recibo de programación acredita que se registró el nuevo intento. No prueba
  que la reparación se haya ejecutado ni que haya funcionado.

## Trabajo completado en el Bloque 3

Se completó la integración transversal del flujo causal de recuperación y el ciclo de aprendizaje:

| Componente / Archivo | Implementación completada |
| --- | --- |
| [app/heart/catalog.py](../app/heart/catalog.py) | Validación estricta de `task_binding` e instrucciones correctivas en el núcleo del plan; `remediate_task` clasificado como mutante (`requires_approval=True`). |
| [app/heart/recovery.py](../app/heart/recovery.py) | Diagnóstico con `task_binding` en hallazgos; `Recovery.design` genera planes acotados con identidad observada; `_order` emite la orden validada con cabecera y plan canónico; `Recovery.advance` consume `order` en `coord.retry_task` sin fallbacks opacos; `Recovery.verify` exige evidencia explícita del intento nuevo (`is_new_attempt` y `v.get('success') is True`). |
| [app/hand/hand.py](../app/hand/hand.py) | Validación estricta de orden y despacho de remediación de tareas mediante `authorization_port.dispatch_task_recovery`. |
| [app/kernel/control_plane.py](../app/kernel/control_plane.py) | `dispatch_task_recovery` ejecuta consumo de orden, transición de tarea y emisión de recibo atómicamente en `ControlUnitOfWork`. |
| [app/mind/coordinator.py](../app/mind/coordinator.py) | `retry_task` exige la orden completa y plan autorizado, despachando la transición atómica mediante Hand. |
| [app/mind/recovery_attempt.py](../app/mind/recovery_attempt.py) | `build_attempt` verifica binding exacto, archiva intento previo en `recovery_history`, asigna extensión de presupuesto acotada y prepara el nuevo intento con `error=None`. |
| [app/dreams/dreams.py](../app/dreams/dreams.py) | Deduplicación por ejecución en `task_observations`, rechazo explícito con `ValueError` ante resultados contradictorios, consolidación transaccional de habilidades y propuestas compartiendo conexión. |
| [app/pixy/rules.py](../app/pixy/rules.py) | Aprobación, reemplazo y reversión atómica de reglas en una sola transacción `BEGIN IMMEDIATE`. |

## Orden de continuación

1. Leer este documento, GEMINI.md y el aviso superior de README. Revisar el estado
   local y los cambios parciales conservando los bloques anteriores.
2. Completar el recorrido de tarea: diagnóstico ligado al intento, plan y hash,
   decisión de Pixy, orden de Hand, transición atómica y recibo reanudable.
3. Completar la supervisión y verificación de Heart, incluyendo la reconciliación
   de aprobaciones tras reinicios y el rechazo de evidencia anterior.
4. Ejecutar únicamente las pruebas puntuales de recuperación indicadas abajo.
5. Completar la transacción de observaciones/propuestas y el gobierno de reglas;
   ejecutar únicamente sus pruebas puntuales.
6. Actualizar esta documentación con los resultados reales y pendientes. No
   ejecutar la suite completa hasta el cierre final acordado ni desplegar.

## Pruebas y criterios de aceptación

Seleccionar casos concretos de `tests/test_recovery.py`,
`tests/test_f4_recovery.py`, `tests/test_control.py` y
`tests/test_f5_dreams.py`; añadir solo regresiones necesarias. No ejecutar
descubrimiento global de tests durante este bloque.

- Orden alterada, revisión/intento cambiado o tarea cancelada: no se programa
  otra ejecución ni se consume parcialmente la autorización.
- Caída después de la transacción: al reanudar se recupera el mismo recibo y no
  aparece otro intento ni otra ampliación de presupuesto.
- Fallo de la transición: autorización, orden y tarea revierten conjuntamente.
- Recuperaciones sucesivas: consumos intactos y ampliaciones fijas auditables.
- Reparación en cola o ejecutándose: caso abierto; resultado sin verificación,
  incierto o de una ejecución anterior: nunca `RECOVERED`.
- Nuevo resultado verificado: cierre del caso con evidencia vinculada a su
  objetivo, revisión, intento y ejecución.
- Observación repetida: no duplica estadísticas ni propuestas; observación
  contradictoria: rechazo explícito.
- Fallo al aprobar, sustituir o revertir una regla: ningún estado parcialmente
  actualizado ni confirmación falsa; aprobación humana sigue siendo necesaria.

**Validación realizada del bloque 3: 78/78 pruebas puntuales pasadas.**
- `tests/test_f4_recovery.py`: 7/7 pasadas.
- `tests/test_recovery.py`: 17/17 pasadas.
- `tests/test_f5_dreams.py`: 6/6 pasadas (incluyendo regresión de observaciones contradictorias y reversión atómica de reglas).
- `tests/test_control.py`: 48/48 pasadas.

La suite completa global permanece reservada para el hito de cierre final acordado.

## Entorno para retomar

Workspace: `C:\Users\cperales\Downloads\sr\Data\Khai`, PowerShell en Windows.
El intérprete usado en los cortes anteriores fue Python 3.12.10. El ejecutable
de `.venv/Scripts/python.exe` apuntaba a un Python ausente de otro equipo; las
dependencias existentes se cargaron desde `.venv/Lib/site-packages` con el
intérprete `python`. Comprobar disponibilidad al retomar sin reinstalar ni añadir
dependencias por defecto.

Ejemplo para ejecutar un único caso seleccionado, sustituyendo su nombre real:

```powershell
@'
import sys, unittest
sys.path.insert(0, '.venv/Lib/site-packages')
suite = unittest.defaultTestLoader.loadTestsFromName('tests.modulo.Clase.test_caso')
result = unittest.TextTestRunner(verbosity=1).run(suite)
sys.exit(not result.wasSuccessful())
'@ | python -B -
```

Mensaje sugerido para retomar: «Lee GEMINI.md y el plan del bloque 3. Continúa la
opción A desde la integración pendiente de Heart, conservando los cambios
anteriores. Solo pruebas puntuales; sin suite completa, commits ni despliegue».
