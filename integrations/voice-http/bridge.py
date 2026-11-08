#!/usr/bin/env python3
"""Classic inbound-answer compatibility bridge. Never creates carrier calls.

The upstream OpenClaw endpoint authenticates the original request before any XML
is changed. Only its initial pause-only reply for the configured test caller is
replaced. All operational callbacks still go through OpenClaw unchanged.
"""
import http.server
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

PATH = '/assistant/voice/webhook'
import os
ORIGIN = os.environ.get('PROCURATOR_PUBLIC_BASE', 'https://assistant.example.com')
UPSTREAM = 'http://127.0.0.1:3334'
MAX_BODY = 16384


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def upstream(target, body, signature):
    request = urllib.request.Request(UPSTREAM+target, data=body, method='POST', headers={
        'Host': ORIGIN.split('//', 1)[-1], 'Content-Type': 'application/x-www-form-urlencoded',
        'X-Twilio-Signature': signature})
    try:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects()).open(request, timeout=12)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        result = response.read(131073)
        if len(result) > 131072:
            raise ValueError('Upstream reply too large.')
        return response.code, response.headers.get('Content-Type', 'application/octet-stream'), result


def process(target, body, signature, settings, send=upstream, admission=None):
    url = urllib.parse.urlsplit(target)
    if url.scheme or url.netloc or url.fragment or url.path != PATH or len(target) > 2048:
        return 404, 'text/plain', b'Not found'
    if len(body) > MAX_BODY:
        return 413, 'text/plain', b'Request too large'
    try:
        fields = urllib.parse.parse_qs(body.decode('utf-8'), keep_blank_values=True, max_num_fields=128)
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True, max_num_fields=64)
    except (UnicodeError, ValueError):
        return 400, 'text/plain', b'Invalid form'
    for key in ['From', 'To', 'CallSid', 'Direction', 'CallStatus']:
        if len(fields.get(key, [])) > 1:
            return 400, 'text/plain', b'Ambiguous form'
    # No answer action is authorized until the unmodified signed callback passes
    # the pinned gateway's verification. The proxy has no Twilio credentials.
    status, content_type, result = send(target, body, signature)
    value = lambda key: fields.get(key, [''])[0]
    if (status != 200 or value('Direction') != 'inbound'
            or value('CallStatus') not in ('ringing', 'initiated', '', 'in-progress')
            or (value('From') != settings['testNumber'] and not settings.get('receptionEnabled', False))
            or value('To') != settings['fromNumber']
            or not re.fullmatch(r'CA[0-9a-fA-F]{32}', value('CallSid'))
            or any(key in fields for key in ['SpeechResult', 'Digits'])
            or any(key in query for key in ['classic_answered', 'type', 'callId', 'turnToken'])):
        return status, content_type, result
    try:
        xml = ET.fromstring(result)
    except ET.ParseError:
        return status, content_type, result
    if xml.tag != 'Response':
        return status, content_type, result
    # Apply the same durable limit to classic answers and realtime Connect/Stream
    # answers. The native gateway has already authenticated the callback. Do not
    # count rejections as accepted calls, or mutate an authenticated stream URL.
    can_answer = any(child.tag not in ('Reject', 'Hangup') for child in xml)
    if settings.get('receptionEnabled', False) and can_answer:
        if admission is None or not admission.allow(value('CallSid'), value('From')):
            return 200, 'application/xml', b'<Response><Reject reason="busy"/></Response>'
    if (value('CallStatus') == 'in-progress' or len(xml) != 1 or xml[0].tag != 'Pause'
            or xml[0].attrib != {'length': '30'} or len(xml[0])):
        return status, content_type, result
    # A leading Pause delays pickup. Say answers; Redirect supplies the resulting
    # in-progress event to the normal handler, which starts its inbound greeting.
    response = ET.Element('Response')
    ET.SubElement(response, 'Say', {'voice': 'alice'}).text = "Connecting you to the owner's AI assistant."
    redirect = ORIGIN+target+('&' if url.query else '?')+'classic_answered=1'
    ET.SubElement(response, 'Redirect', {'method': 'POST'}).text = redirect
    return 200, 'application/xml', ET.tostring(response, encoding='utf-8', xml_declaration=True)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Do not log phone numbers, transcripts, signatures or callback URLs.

    def respond(self, status, content_type, body):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond(405, 'text/plain', b'POST required')

    def do_POST(self):
        try:
            self.connection.settimeout(15)
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get('Transfer-Encoding'):
                self.respond(400, 'text/plain', b'Content length required')
                return
            size = int(lengths[0])
            if size > MAX_BODY:
                self.respond(413, 'text/plain', b'Request too large')
                return
            if self.headers.get('Content-Type', '').split(';')[0].lower().strip() != 'application/x-www-form-urlencoded':
                self.respond(415, 'text/plain', b'Form required')
                return
            body = self.rfile.read(size)
            if len(body) != size:
                self.respond(400, 'text/plain', b'Incomplete form')
                return
            self.respond(*process(self.path, body, self.headers.get('X-Twilio-Signature', ''), self.server.settings,
                                  admission=self.server.admission))
        except Exception:
            print('Voice bridge request did not complete.', file=sys.stderr)
            try:
                self.respond(502, 'text/plain', b'Voice service unavailable')
            except Exception:
                pass


if __name__ == '__main__':
    settings = json.loads(Path('/var/lib/procurator-voice-http/settings.json').read_text())
    if not re.fullmatch(r'\+1[2-9]\d{2}[2-9]\d{6}', settings['testNumber']) or settings['fromNumber'] != '+15550100100':
        raise SystemExit('Voice bridge settings are not ready.')
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 3335), Handler)
    server.settings = settings
    from admission import Admission
    server.admission = Admission('/var/lib/procurator-voice-http/admissions/admissions.sqlite3')
    print('Voice answer bridge listening on loopback.', flush=True)
    server.serve_forever()
