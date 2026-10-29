"""Telegram help and buttons whose callbacks carry the complete command."""

HELP_TEXT = """Comandos disponibles:
/help o /? - muestra esta ayuda.
/mode ? o /m ? - consulta el modo y su estado.
/mode low o /m l - usa Gemini.
/mode mid o /m m - usa Dolphin CPU Spot.
/mode high o /m h - usa Dolphin GPU L4 Spot.
Pulsa los botones para cambiar de modo.
/pixy o /p - habla directamente con Pixy (diagnóstico, reglas y servicios).
/health - muestra el estado de salud e integridad del sistema.
/cancel o /stop - detiene el objetivo en curso (o el ID indicado).
/restart - reinicia el contenedor Docker de Khai.
Comandos in-chat:
/chats o /c [número|nombre]: Muestra la lista numerada de conversaciones recientes indicando cuál está activa
/new [nombre]: Desmarca la anterior como activa y crea un hilo limpio con ese nombre.
/clear: Archiva el contexto de la conversación actual e inicia un hilo limpio inmediatamente sin cambiar de tema.
/archive: Archiva la conversación activa actual y conmuta a la más reciente anterior (o crea una nueva si no quedan)."""

HELP_BUTTONS = [{'label': '/mode ' + mode, 'value': '/mode ' + mode}
                for mode in ('low', 'mid', 'high', '?')]
