# Implementación — 24 de octubre de 2026

## Cambios principales

- Coordinador persistente con identidad por objetivo e intento, deduplicación, bloqueos de propietario y presupuestos acumulados.
- Protocolo autenticado de ejecución con recibos persistidos, cancelación previa al envío y recuperación mediante nombres de contenedor estables.
- Separación de generación, revisión, autorización y ejecución; artefactos SHA256 que no se reescriben tras la revisión.
- Workspaces aislados, sin base de datos ni credenciales del Core; red desactivada salvo autorización y límites de recursos del worker.
- Aprobaciones explícitas con vencimiento y contenido fijado; transporte estructurado de solicitudes de permiso dentro de GOD.
- Separación entre proceso terminado, servicio iniciado y resultado verificado.
- Verificación por SHA256 esperado o decisión del usuario; métricas deduplicadas por ejecución, reglas candidatas y sustitución/reversión explícita.
- Preservación de tareas históricas y del antiguo esquema de reglas; cierre explícito de conexiones SQLite.
- URL base del proxy normalizada; autenticación e identidad verificadas en Telegram, incluidos callbacks.
- Eliminación de comandos de infraestructura generados por el modelo y de la instalación sin uso del paquete OpenClaw en la imagen.

## Compatibilidad

Core y proxy se actualizan juntos. El antiguo `/run-skill` deja de aceptar ejecuciones. Las habilidades históricas permanecen en disco, pero no se consideran automáticamente verificadas ni se reanudan tras la migración.

SAFE mantiene el formato de script con `# MODE:` y dependencias opcionales `# PIP:`. GOD mantiene la compatibilidad acotada de `BaseSkill.run_step` y `require_approval`; no permite acceso al host. AGENT no publica puertos ni vive de forma indefinida: se limita al plazo del objetivo.

Los antiguos marcadores de Pixy para inyectar secretos, agregar reglas sin evidencia o controlar Docker no producen mutaciones. La administración del sistema se realiza fuera del agente. `/new`, `/compact` y `/restart` muestran ayuda en vez de eliminar evidencia o reiniciar servicios.

Los archivos de identidad existentes no se modificaron. La revisión está
desplegada en la VM de Azure; la configuración operativa y las comprobaciones
de salud se mantienen en [OPERATIONS.md](OPERATIONS.md).

## Validación

La suite se ejecuta con `python -B -m unittest discover -s tests -v` y completó
70 pruebas correctamente. Cubre entradas duplicadas y simultáneas, presupuesto
persistente, propietario exclusivo, cancelación, aprobaciones ligadas a
contenido, denegación, recibos de aprobación, resultado incierto, falso éxito,
daemon iniciado, agotamiento de hipótesis, reintentos tras timeout de transporte,
métricas y reglas, migración de conocimiento, protocolo, autenticación y
aislamiento de montajes.

Incluye un flujo completo offline con Docker/Telegram/LLM simulados y una prueba real del runner en un subproceso local inofensivo: guarda un checkpoint, pausa solicitando aprobación y completa después de recibir el permiso. Esa prueba no utiliza Docker ni contacta servicios.

Las pruebas locales no utilizan las credenciales del proyecto. La validación del
despliegue se realiza por separado en el host y no debe confundirse con la suite
local.

## Límites y trabajo posterior

- Falta validar imágenes y límites efectivos con Docker real en Linux, incluidas las condiciones de memoria de la VM y el cierre del daemon.
- La verificación automática v1 se limita a SHA256 de archivos; una prueba funcional HTTP, semántica o de salud requiere otro verificador y su propia política.
- Los checkpoints no garantizan una única ejecución de efectos externos. Los resultados inciertos o fallos con posibles efectos parciales requieren inspección del operador.
- La salida de red aprobada utiliza bridge; las restricciones por dominio y contra servicios internos deben aplicarse en la infraestructura antes de habilitar accesos sensibles.
- Falta automatizar cuotas de disco, retención de imágenes, workspaces y evidencia. Los límites del worker no representan una cuota de sus bind mounts ni una cuota completa del proceso de construcción de imágenes.
- La proporción de éxitos es observacional. No se implementó un experimento A/B que demuestre mejora causal frente a una estrategia de referencia.
- La sustitución de reglas es explícita; no existe un detector semántico automático de contradicciones.
- No se migró el runtime a OpenClaw ni a Hermes. Se adoptaron patrones de diseño en la implementación propia.

La ampliación de autonomía debe depender de esos resultados medidos; esta revisión no convierte una reflexión del modelo en evidencia de que una tarea funcionó.
