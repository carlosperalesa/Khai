# Operaciones

## Limpieza y revisiÃ³n

Ejecutar `make clean`, nunca `clean-all`, para conservar `.venv`. Antes de publicar revisar `git status`, `git diff --check`, secretos accidentales y enlaces documentales. Las suites locales no sustituyen la validaciÃ³n conversacional de producciÃ³n.

## Despliegue

1. Registrar el commit sano desplegado para rollback.
2. Detener Core y proxy.
3. Purgar exclusivamente SQLite de runtime, logs, borradores, workspaces y skills generadas.
4. Conservar secretos, certificados, identidad y configuraciÃ³n.
5. Actualizar desde `origin/main` y reconstruir Core y proxy juntos.
6. Comprobar arranque, contenedores y endpoint de salud.
7. Ejecutar `VALIDATION.md` por el canal real.

La purga debe resolver y mostrar rutas absolutas antes de borrar. No se usan globs contra raÃ­ces amplias.

## Rollback y secretos

Ante pÃ©rdida de control, corrupciÃ³n, imposibilidad de arrancar o riesgo para secretos, detener servicios, desplegar el commit sano, purgar nuevamente solo datos generados y arrancar con bases vacÃ­as. Un fallo funcional se documenta y recorre con `/p corrige el ultimo error`; no se oculta con rollback.

Los secretos entran por el canal privado de Voice, nunca por chat, Git, logs ni descriptores. La solicitud debe indicar nombre, motivo y forma de carga. Nunca se sustituye un proveedor porque falte una clave.
