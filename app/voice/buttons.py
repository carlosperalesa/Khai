"""Internal button contract; Telegram wire names belong only to its adapter."""


def validate_buttons(buttons):
    if buttons is None:
        return None
    if not isinstance(buttons, list) or not buttons:
        raise ValueError('Buttons must be a nonempty list')
    rows = buttons if isinstance(buttons[0], list) else [[b] for b in buttons]
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError('Invalid button row')
        for button in row:
            if (not isinstance(button, dict) or set(button) != {'label', 'value'}
                    or not isinstance(button['label'], str) or not button['label'].strip()
                    or not isinstance(button['value'], str)
                    or not 1 <= len(button['value'].encode('utf-8')) <= 64):
                raise ValueError('Invalid internal button: expected label/value')
    return rows
