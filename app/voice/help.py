"""Telegram help and buttons whose callbacks carry the complete command."""

HELP_TEXT = """Comandos disponibles:
/help o /? - muestra esta ayuda.

Modo:
/mode low o /m l - Gemini.
/mode mid o /m m - Dolphin CPU.
/mode high o /m h - Dolphin GPU.
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
        {'label': 'Mode low', 'value': '/mode low'},
        {'label': 'Mode medium', 'value': '/mode mid'},
        {'label': 'Mode high', 'value': '/mode high'},
    ],
    [{'label': 'Ver modo actual', 'value': '/mode ?'}],
    [{'label': 'Mis Chats', 'value': '/c'}],
]
