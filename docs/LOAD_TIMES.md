# Revisión de tiempos de activación — 31 de octubre de 2026

La migración dejó operativo un disco `pd-balanced` de 100 GiB en `us-west1-b`.
Low respondió; high y mid agotaron la precarga. No hay causa raíz demostrada
para los perfiles GPU y no se autorizaron cambios de plazo ni de APIs de métricas.

## Medición observada

Fuente: `model_transition_events` en `data/voice/dolphin_usage.sqlite3`.
Solicitud por webhook: `telegram:1793481829521`.

| Perfil | Antes de precarga | Precarga | Activación total | Resultado |
|---|---:|---:|---:|---|
| high | 54,70 s | 90,13 s | 144,83 s | ReadTimeout |
| mid | 105,15 s | 90,12 s | 195,26 s | ReadTimeout |
| low | 92,95 s | 30,81 s | 123,76 s | Conectado |

La cascada completa tardó **464,01 s (7 min 44 s)**. Incluye operaciones GCE,
recreación de VM y espera de API; no equivale a sumar solo tiempos de carga.
`/api/ps` confirmó Dolphin residente en CPU, contexto 4096, `size_vram=0`.

En mid se observó Ollama a las 21:27:34 UTC, detección de L4 a las 21:27:59,
un watchdog de descubrimiento GPU a las 21:28:03, inicio de tensores a las
21:28:06 y `offloaded 33/33 layers to GPU` a las 21:28:33. El timeout ocurrió
a las 21:29:30. **El mensaje offloaded no acredita transferencia terminada.**

## Qué permiten concluir los registros

La versión observada es Ollama **0.40.12**, que fija llama.cpp **b11901**.
En ese código, el mensaje `offloaded` se imprime antes de `load_all_data`.
El tiempo posterior aún puede incluir lectura y transferencia de pesos;
la interpretación inicial de “pesos ya cargados” se corrigió.
[Versión fijada](https://raw.githubusercontent.com/ollama/ollama/v0.40.12/LLAMA_CPP_VERSION),
[código de carga](https://raw.githubusercontent.com/ggml-org/llama.cpp/b11901/src/llama-model.cpp).

El tamaño informado de Qwen es 19.851.349.765 bytes; Dolphin, 4.920.757.726.
Google publica un techo por tamaño de **168 MiB/s** para balanced de 100 GiB,
sujeto también a la VM. Leer íntegramente Qwen desde disco a ese máximo
supondría aproximadamente **112,7 s**, y Dolphin **27,9 s**. Es un cálculo
orientativo sin caché; **no es una medición del rendimiento del servidor**.
[Límites oficiales](https://docs.cloud.google.com/compute/docs/disks/performance).

No atribuir el retraso a un precalentamiento obligatorio del disco restaurado:
Google indica que un disco restaurado desde snapshot puede alcanzar su
rendimiento inmediatamente. [Restauración](https://docs.cloud.google.com/compute/docs/disks/restore-snapshot).

## Hipótesis y siguiente prueba propuesta

1. High puede necesitar más de 90 s para lectura, transferencia e inicialización
   en frío. El cálculo del techo de disco permite esa hipótesis, sin probarla.
2. En mid, el descubrimiento CUDA, la transferencia de pesos o la preparación
   del runner requieren correlación con el journal completo, buffers, contexto
   y errores NVIDIA. El watchdog es una señal, no un diagnóstico concluyente.
3. `_warmup_model` descarta las duraciones de la respuesta de Ollama, incluso
   cuando low sí responde. Registrar `load_duration`, `prompt_eval_duration`
   y `eval_duration` ayudaría a separar las etapas en futuras ejecuciones.
   [Respuesta de Ollama](https://docs.ollama.com/api/generate),
   [diagnóstico GPU](https://docs.ollama.com/troubleshooting).

La próxima prueba propuesta es un intento instrumentado de **mid**, con nano
disponible, captura completa de journal y del comando del runner, conservando
el límite inicial de 90 s y evitando destruir la evidencia con otra cascada.
Requiere decidir cómo ejecutar esa prueba con la transición actual, que todavía
es síncrona y no cancelable. No se ha autorizado ni ejecutado esta nueva prueba.

Cloud Monitoring API permanece deshabilitada por decisión del propietario.
No se instalaron agentes ni se ejecutaron benchmarks durante esta revisión.

## Aceptación conversacional posterior

La orden de establecer Brave como preferencia (`telegram:1793482754879`) llegó
al webhook, pero low tardó casi tres minutos y devolvió un JSON que Mind rechazó
con `ValidationError`. No se guardó la preferencia. Esa prueba confirma una
limitación adicional: una inferencia mínima exitosa no prueba que el modelo
respete el contrato del router con su contexto y esquema completos.

El contenido rechazado no quedó registrado, por lo que no se puede atribuir
el error a un campo concreto ni afirmar que la causa sea truncamiento de contexto.
Antes de corregir la integración convendría conservar los errores de validación
sin incluir secretos y contrastar la generación estructurada con el esquema real.
