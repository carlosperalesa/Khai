"""Telegram help and buttons whose callbacks carry the complete command."""

HELP_TEXT = """Comandos disponibles:
/help o /? - muestra esta ayuda.
/mode low - usa Gemini y el respaldo Cloud.
/mode mid - usa Dolphin CPU Spot.
/mode high - usa Dolphin GPU L4 Spot.
/mode ? - consulta el modo y su estado.
También puedes usar /m en lugar de /mode. Pulsa los botones para cambiar de modo.
/pixy <mensaje> o /p <mensaje> - habla directamente con Pixy (diagnóstico, reglas y servicios).
/health - muestra el estado de salud e integridad del sistema.
/cancel [ID] o /stop [ID] - detiene el objetivo en curso (o el ID indicado).
/restart - reinicia el contenedor Docker de Khai.
/us <usuario> /pas <contraseña> /api <clave> - ingresa credenciales en chat privado (puedes enviarlas juntas en un mismo mensaje o por separado).
Las aprobaciones y confirmaciones se responden con y/n."""

HELP_BUTTONS = [{'label': '/mode ' + mode, 'value': '/mode ' + mode}
                for mode in ('low', 'mid', 'high', '?')]
