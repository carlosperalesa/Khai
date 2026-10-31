"""Configured platform search over Voice's bounded public HTTPS transport."""
import json
from pathlib import Path
import yaml

from app.voice.gateway.external_gateway import ExternalGateway


class ResearchSearch:
    def __init__(self, config_dir, secrets):
        path = Path(config_dir) / 'research.yaml'
        self.config = yaml.safe_load(path.read_text(encoding='utf-8')) if path.is_file() else {}
        if not isinstance(self.config, dict) or not all(
                self.config.get(key) for key in ('provider', 'service', 'operation', 'result_path')):
            raise ValueError('Platform research service is not configured')
        self.gateway = ExternalGateway(path, secrets)

    def search(self, query, limit=8):
        if (not isinstance(query, str) or not query.strip() or len(query) > 600
                or len(query.split()) > 75):
            raise ValueError('Invalid research query')
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('Research limit must be an integer from 1 to 20')
        operation = self.gateway.services.get(self.config['service'], {}).get('operations', {}).get(self.config['operation'], {})
        credential = operation.get('credential')
        if credential and not self.gateway.secrets.get(credential):
            raise PermissionError('Search credential unavailable in Voice')
        result = self.gateway.request(self.config['service'], self.config['operation'],
                                      {'q': query.strip(), 'count': limit})
        data = json.loads(result['body'])
        if not isinstance(data, dict) or data.get('error'):
            raise ValueError('Research provider returned an invalid response')
        rows = data
        for key in self.config['result_path']:
            if not isinstance(rows, dict):
                raise ValueError('Research provider returned invalid results')
            rows = rows.get(key, [] if key == self.config['result_path'][-1] else {})
        if not isinstance(rows, list):
            raise ValueError('Research provider returned invalid results')
        results = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            title, url = row.get('title'), row.get('url')
            if isinstance(title, str) and title.strip() and isinstance(url, str) and url.startswith(('https://', 'http://')):
                description = row.get('description')
                results.append({'title': title, 'url': url,
                                'description': description if isinstance(description, str) else ''})
        if not results:
            raise ValueError('Research provider returned no valid sources')
        return {'provider': self.config['provider'], 'query': query.strip(),
                'results': results, 'untrusted': True}
