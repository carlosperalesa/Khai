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
    seeds = {
        'open_meteo': ('forecast', 'https://api.open-meteo.com/v1/forecast',
                       ['latitude', 'longitude', 'daily', 'timezone'], None),
        'github': ('search_repositories', 'https://api.github.com/search/repositories',
                   ['q', 'per_page'], None),
        'brave': ('search', 'https://api.search.brave.com/res/v1/web/search',
                  ['q', 'count'], 'BRAVE_SEARCH_API_KEY'),
    }
    if name not in seeds:
        raise ValueError('Unknown seed')
    operation, url, parameters, credential = seeds[name]
    descriptor = {'service_id': 'brave_search' if name == 'brave' else name, 'operation': operation, 'version': 'v1',
                  'url': url, 'method': 'GET', 'parameters': parameters,
                  'content_types': ['application/json'], 'max_bytes': 65536,
                  'timeout': 15, 'per_minute': 10, 'effect': 'read',
                  'credential_ref': credential, 'credential_header': 'X-Subscription-Token' if credential else 'Authorization',
                  'credential_prefix': '', 'effect_contract': None}
    return {'descriptor': descriptor, 'status': 'unreviewed_template',
            'note': 'Verify current provider documentation, credentials and quota; Pixy approval is required.'}
