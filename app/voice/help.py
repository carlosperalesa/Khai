"""Telegram help and buttons whose callbacks carry the complete command."""

HELP_TEXT = """Comandos disponibles:
/help o /? - muestra esta ayuda.

Modo:
/mode nano o /m n - Gemini (Cloud API sin VM).
/mode low o /m l - Dolphin CPU (e2-standard-8, 8B).
/mode mid o /m m - Dolphin GPU (g2-standard-4, L4 8B).
/mode high o /m h - Qwen 32B GPU (g2-standard-8, L4 32B).
/mode ? o /m ? - muestra el modo actual.

Sistema:
/pixy o /p - habla con Pixy.
/health - muestra el estado del sistema.
/cancel o /stop - detiene el objetivo en curso.
/restart - reinicia Khai.

Conversaciones:
/chats o /c [número] - muestra o activa un chat.
/new [nombre] - crea un chat nuevo.
/rename [número] <nombre> - renombra un chat.
/clear - limpia el chat actual e inicia uno nuevo.
/delete [número] - elimina el chat actual o el indicado."""

HELP_BUTTONS = [
    [
        {'label': 'Mode nano', 'value': '/mode nano'},
        {'label': 'Mode low', 'value': '/mode low'},
        {'label': 'Mode medium', 'value': '/mode mid'},
        {'label': 'Mode high', 'value': '/mode high'},
    ],
    [{'label': 'Ver modo actual', 'value': '/mode ?'}],
    [{'label': 'Mis Chats', 'value': '/c'}],
]
