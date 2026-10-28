"""Render typed action results without inventing facts or external effects."""
import json
import hashlib


def _label(key):
    return str(key).replace('_', ' ').strip().capitalize()


def _value_lines(value, depth=0):
    if depth > 3:
        return [json.dumps(value, ensure_ascii=False)[:500]]
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(_label(key) + ':')
                lines.extend(_value_lines(item, depth + 1))
            else:
                lines.append(f'{_label(key)}: {item}')
        return lines
    if isinstance(value, list):
        lines = []
        for index, item in enumerate(value, 1):
            rendered = _value_lines(item, depth + 1)
            if rendered:
                lines.append(f'{index}. {rendered[0]}')
                lines.extend('   ' + line for line in rendered[1:])
        return lines
    return [str(value)]


def present_recipe(task):
    """Only a completed write receipt may be described as accepted by its provider."""
    lines = []
    inputs = task.get('recipe_input') or {}
    recipient = next((inputs[key] for key in ('email', 'correo', 'to', 'recipient', 'destinatario')
                      if isinstance(inputs.get(key), str) and '@' in inputs[key]), None)
    recipient_hash = (hashlib.sha256(json.dumps(recipient, ensure_ascii=False).encode()).hexdigest()
                      if recipient else None)
    for receipt in task.get('recipe_receipts', []):
        lines.append(_label(receipt['step_id']) + ':')
        lines.extend(_value_lines(receipt['output']))
        for effect in receipt.get('external_effects', []):
            if effect.get('effect') != 'write' or effect.get('status') != 'completed':
                continue
            summary = effect.get('summary') or {}
            name = summary.get('name') or effect.get('operation') or 'escritura'
            sent_to_confirmed = (recipient_hash and recipient_hash in {
                value for key, value in effect.get('parameter_hashes', {}).items()
                if key in {'email', 'correo', 'to', 'recipient', 'destinatario'}})
            if sent_to_confirmed and any(word in name.casefold() for word in ('correo', 'mail', 'email')):
                lines.append(f'El proveedor confirmó el envío a {recipient}.')
            else:
                lines.append(f'El proveedor confirmó la operación: {name}.')
    lines.append('¿Es correcto el resultado completo? Responde y, n o explica qué debo corregir.')
    return '\n'.join(lines)[:8000]
