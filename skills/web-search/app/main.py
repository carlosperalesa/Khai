# MODE: SAFE
"""Candidate web-search skill: Brave results through an approved Voice service."""
import json
from khai_gateway import request
from khai_input import params

query = params.get('query')
count = params.get('count', 8)
if (not isinstance(query, str) or not query.strip() or len(query) > 600
        or len(query.split()) > 75):
    raise ValueError('Se requiere una consulta de hasta 600 caracteres y 75 palabras')
if type(count) is not int or not 1 <= count <= 20:
    raise ValueError('count debe ser un entero entre 1 y 20')
response = request('brave_search', 'search', {'q': query.strip(), 'count': count})
if not isinstance(response, dict) or response.get('error'):
    raise ValueError('Respuesta inválida del buscador')
web = response.get('web', {})
if not isinstance(web, dict) or not isinstance(web.get('results', []), list):
    raise ValueError('Resultados inválidos del buscador')
items = []
for item in web.get('results', [])[:count]:
    if not isinstance(item, dict):
        continue
    title, link = item.get('title'), item.get('url')
    if isinstance(title, str) and isinstance(link, str) and link.startswith(('https://', 'http://')):
        snippet = item.get('description')
        items.append({'title': title, 'url': link,
                      'description': snippet if isinstance(snippet, str) else ''})
print(json.dumps({'provider': 'brave', 'results': items}, ensure_ascii=False))
