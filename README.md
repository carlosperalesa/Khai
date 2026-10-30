# Khai

Khai es un agente personal capaz de investigar, construir, revisar, ejecutar y aprender capacidades reutilizables. EstÃ¡ en desarrollo activo: la salud confirma infraestructura, pero la aceptaciÃ³n funcional depende de recorridos conversacionales reales documentados en `docs/VALIDATION.md`.

## Principios

- Adaptabilidad: una solicitud es capacidad, proveedor opcional y objetivo. Los proveedores son datos del catÃ¡logo, no ramas del Core.
- Transparencia: Khai pregunta ante decisiones nuevas y nunca sustituye silenciosamente un proveedor.
- AutonomÃ­a acotada: decide detalles tÃ©cnicos reversibles dentro del objetivo autorizado. Secretos, permisos, costos, efectos externos y preferencias pasan por Pixy.
- Evidencia: no declara Ã©xito sin ejecuciÃ³n y verificaciÃ³n reales.
- Aprendizaje: cada Ã©xito o fallo tÃ©cnico Ãºtil deja un episodio recuperable; cambiar polÃ­ticas requiere evidencia repetida.

## Componentes

Mind interpreta y coordina; Hand investiga, crea y repara mediante un ciclo de herramientas persistente; Pixy revisa y obtiene decisiones; Heart diagnostica fallos repetidos o sistÃ©micos; Voice concentra red, credenciales y recibos; Dreams registra resultados y propone aprendizaje.

Cada skill contiene `SKILL.md`, `app/`, `data/` y `other/`. Sus metadatos declaran `capability`, `provider` y `aliases`. Instalar una skill no cambia preferencias. Los workers no tienen red directa.

La arquitectura vigente estÃ¡ en `docs/ARCHITECTURE.md`; despliegue, purga y rollback en `docs/OPERATIONS.md`; evidencia en `docs/VALIDATION.md`; decisiones aceptadas en `docs/adr/`. Para limpieza segura use `make clean`; `clean-all` elimina `.venv` y no forma parte del flujo normal.
