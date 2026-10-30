# Arquitectura vigente

## Flujo

1. Mind convierte lenguaje natural en objetivo, capacidad y proveedor opcional.
2. El catÃ¡logo filtra skills activas. Sin proveedor explÃ­cito usa solo una preferencia aprobada; si no existe, pregunta.
3. Si falta la capacidad, Hand abre un `agent_run` durable: LLM, herramientas, resultados o errores, nueva decisiÃ³n y verificaciÃ³n.
4. Pixy revisa descriptores y artefactos y solicita decisiones que exceden la autoridad otorgada.
5. Voice ejecuta accesos externos; el worker ejecuta la skill sin red directa.
6. La verificaciÃ³n determina el resultado. Dreams registra el episodio y el catÃ¡logo promueve capacidades verificadas.

## Contratos

`Intent` contiene objetivo, capacidad y proveedor opcional. `CapabilitySpec` declara capacidad, proveedor, alias, operaciones, requisitos y estado. `DecisionReceipt` registra decisiÃ³n, alcance, vigencia y procedencia. `SkillSpec` enlaza esos datos con esquemas, runtime, dependencias, servicios y verificador.

Cada skill exige `SKILL.md`; Python, referencias y descriptores son opcionales segÃºn la capacidad. Tools son facultades de plataforma; skills enseÃ±an y empaquetan una capacidad; proveedores son implementaciones registradas. Ninguno se sustituye por una marca hardcodeada.

## Hand, reparaciÃ³n y aprendizaje

Runs, mensajes y tool calls se guardan en SQLite. El lÃ­mite es 16 turnos lÃ³gicos, 40 tool calls y 10 minutos. Los errores de validaciÃ³n regresan completos. Dos errores idÃ©nticos sin cambio efectivo transfieren el caso a Heart.

Heart recibe error, trayectoria y evidencia de Senses; produce diagnÃ³stico y objetivo correctivo. Hand implementa y Pixy revisa. Las taxonomÃ­as deterministas solo etiquetan telemetrÃ­a.

`episode_notes` y FTS5 conservan Ã©xitos y fallos tÃ©cnicos, incluidos los previos a ejecutar. No se aprende de cancelaciones, permisos negados ni secretos ausentes. Las notas orientan a Hand, pero nunca acreditan Ã©xito.

SQLite guarda control, conversaciones, runs, recibos, catÃ¡logo, preferencias y episodios. Voice posee credenciales y red; Pixy, aprobaciones. Los hashes fijan artefactos. Estados terminales y recibos, no el texto, son la fuente de verdad.
