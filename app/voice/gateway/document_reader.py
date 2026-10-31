"""Bounded, public HTTPS reader for URLs explicitly supplied by the owner."""
import http.client
import ipaddress
import socket
import ssl
from html.parser import HTMLParser
from urllib.parse import urlsplit


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'nav', 'footer', 'noscript'}:
            self.hidden += 1
        elif tag in {'p', 'pre', 'h1', 'h2', 'h3', 'li', 'br'} and not self.hidden:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'nav', 'footer', 'noscript'} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def read_public_document(url, allowed_urls):
    if not isinstance(url, str) or url not in allowed_urls or len(url) > 2048:
        raise PermissionError('URL must be explicitly supplied by the owner')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.fragment or parsed.port not in (None, 443)):
        raise PermissionError('Only public HTTPS URLs are allowed')
    ips = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise PermissionError('Private destinations are forbidden')
    conn = http.client.HTTPSConnection(parsed.hostname, timeout=15)
    try:
        sock = socket.create_connection((sorted(ips)[0], 443), timeout=15)
        conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        conn.request('GET', path, headers={'Accept': 'text/html,text/plain', 'User-Agent': 'Khai/1.0'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError('Document HTTP status ' + str(response.status))
        mime = (response.getheader('Content-Type') or '').split(';')[0].lower()
        if mime not in {'text/plain', 'text/html'}:
            raise ValueError('Unsupported document content type')
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Document exceeds size limit')
        text = raw.decode('utf-8', errors='replace')
        if mime == 'text/html':
            parser = _Text()
            parser.feed(text)
            text = ''.join(parser.parts)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned = '\n'.join(lines)
        return {'url': url, 'text': cleaned[:16000], 'truncated': len(cleaned) > 16000,
                'untrusted': True}
    finally:
        conn.close()
