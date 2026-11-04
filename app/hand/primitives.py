"""Offline construction aids. Outputs are proposals, never approvals."""
import ast
import sys


def check_python(code):
    if not isinstance(code, str) or len(code) > 200000:
        raise ValueError('Invalid source')
    try:
        tree = ast.parse(code)
        compile(tree, '<candidate>', 'exec')
    except (SyntaxError, ValueError) as exc:
        return {'valid': False, 'error': str(exc), 'imports': []}
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split('.')[0])
    known = set(getattr(sys, 'stdlib_module_names', ())) | {'khai_gateway', 'khai_input', 'khai_documents', 'app'}
    return {'valid': True, 'imports': sorted(imports),
            'external_imports': sorted(imports - known),
            'note': 'Static check only; imports and runtime behavior are not verified.'}


def descriptor_seed(name):
    import json
    from pathlib import Path
    seeds = json.loads((Path(__file__).resolve().parents[2] / 'config/service_templates.json').read_text(encoding='utf-8'))
    if name not in seeds:
        raise ValueError('Unknown seed')
    return {'descriptor': dict(seeds[name]), 'status': 'unreviewed_template',
            'note': 'Verify provider documentation, credentials and quota; Pixy approval is required.'}
