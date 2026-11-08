"""Private read-only activity log page (Nov 5 2026).

Served on 127.0.0.1:3342 only; nginx forwards GET /assistant/activity/<token>. The token comes from a
Telegram message the broker sends to the owner (/activity page, 45 minutes). The page has no script and
no forms; only ?days=N and ?kind=K filters. Paths are never logged (they carry the token).
"""
import http.server
import re
import secrets
import socketserver
import threading
import urllib.parse

from mission_page import message_page

PATH = re.compile(r'^/assistant/activity/([A-Za-z0-9_-]{40,64})$')


def handler_for(broker):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'assistant'
        sys_version = ''

        def log_message(self, *args):
            pass

        def send(self, code, body, nonce=None):
            data = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Robots-Tag', 'noindex')
            csp = "default-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'"
            if nonce:
                csp += f"; style-src 'nonce-{nonce}'"
            self.send_header('Content-Security-Policy', csp)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition('?')
            m = PATH.match(path)
            if not m:
                return self.send(404, message_page('Not found.'))
            q = urllib.parse.parse_qs(query)
            try:
                days = int(q.get('days', ['30'])[0])
            except ValueError:
                days = 30
            kind = q.get('kind', [None])[0]
            nonce = secrets.token_urlsafe(16)
            try:
                out = broker.call('activity_view', {'token': m.group(1), 'days': days, 'kind': kind, 'nonce': nonce}, True)
            except ValueError as e:
                return self.send(410, message_page(str(e)))
            except Exception:
                return self.send(503, message_page('The activity log is not available right now. Try again in a minute.'))
            self.send(200, out['html'], nonce)
    return Handler


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def start(broker, port=3342):
    server = Server(('127.0.0.1', port), handler_for(broker))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
