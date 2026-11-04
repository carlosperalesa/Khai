# Purga manual pendiente — 4 de noviembre de 2026

La versión reparada está desplegada con el estado anterior conservado. La revisión automática rechazó ejecutar la eliminación irreversible de las ocho bases y sus respaldos. El propietario eligió conservar el estado y recibir este procedimiento manual. **No se ejecutó `--apply`.**

Desde una sesión SSH en el host de KHAI, con las tareas detenidas y Nano seleccionado:

```sh
cd /opt/khai &&
sudo docker compose stop bot docker_proxy &&
sudo python3 -m scripts.reset_history --data-dir /opt/khai/data --validate &&
sudo python3 -m scripts.reset_history --data-dir /opt/khai/data --apply &&
sudo docker compose up -d --no-build bot docker_proxy
```

Este comando se entrega para ejecución manual; no se ha ejecutado durante el despliegue. `--apply` reconstruye las ocho bases, elimina el historial operativo y los respaldos inventariados de `data` y del directorio `backups` del proyecto. Conserva identidad, claves, certificados, preferencias, conocimiento depurado, skills y aprobaciones técnicas. No existe rollback del historial eliminado. Si una etapa falla, la cadena se detiene; inspecciona el error antes de continuar. Para recuperar el servicio sin aplicar la purga: `sudo docker compose up -d --no-build bot docker_proxy`.

La validación offline previa a producción reconstruyó correctamente ocho bases: tres skills, 23 archivos conservados y 187 archivos removibles en ese inventario. El corte se establece al aplicar y el webhook descarta mensajes y botones con fecha anterior. No se ha reinstalado el webhook ni descartado su cola remota en este despliegue.

Telegram ya aceptó un lote con 49 identificadores conocidos del chat privado del propietario; 436 quedaron fuera de la ventana temporal. No hubo lotes rechazados. La aceptación del lote no acredita cada eliminación individual y no se afirma que Telegram esté vacío. Los mensajes de las pruebas posteriores no forman parte de ese lote.

Las imágenes históricas y la caché Docker siguen conservadas, al igual que las bases y respaldos. El comando anterior no borra imágenes, cachés, logs del motor Docker ni todos los temporales fuera de `data`. Algunas imágenes históricas se construyeron con respaldos dentro de su contexto; la imagen nueva los excluye. La eliminación de esos restos requiere inventario y limpieza adicionales antes de acreditar una purga integral. No usar una poda global de Docker para esta operación.
