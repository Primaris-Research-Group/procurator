#!/usr/bin/env python3
"""PROCURATOR as an MCP server (0.25.0): Claude Desktop, Claude Code, ChatGPT or any MCP client can hand PROCURATOR a task.

It speaks MCP over stdio (newline-delimited JSON-RPC 2.0) and runs ON THE SERVER, reached through SSH, so the SSH key is the
credential and nothing listens on a network port. Claude Desktop (claude_desktop_config.json):

  "procurator": {"command": "ssh", "args": ["-T", "root@YOUR-DROPLET", "python3", "/opt/procurator/bin/procurator-mcp.py"]}

Tools:
  request_task   hand PROCURATOR a task in plain words. Nothing runs yet: The owner gets the text in Telegram and starts it
                 with /mcprun ID (or ignores it). An MCP client can delegate work; it can never approve it.
  task_status    where a request stands (waiting for the owner, started, declined or expired)
  recent_activity  what PROCURATOR did recently (titles and status from the activity log; no message bodies, no health details)
  health_summary counts only: what in the health module needs them (never details)

No approval, health, mail, call or browser tool is exposed here, on purpose.
"""
import json
import socket
import sys

SOCK = '/run/procurator-web/tools.sock'
PROTOCOL = '2025-06-18'
TOOLS = [
    {'name': 'request_task', 'description': 'Hand PROCURATOR (The owner\'s administrative agent) a task in plain words: email, calendar, a phone call, a web errand, a '
     'booking, research. It does not run until the owner starts it from Telegram with /mcprun; every send, call, booking or submission still needs '
     'their own approval there. Returns a request id for task_status.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['task'],
                     'properties': {'task': {'type': 'string', 'maxLength': 2000, 'description': 'What to do, with the facts PROCURATOR needs.'},
                                    'client': {'type': 'string', 'maxLength': 40, 'description': 'Who is asking, e.g. "Claude Desktop".'}}}},
    {'name': 'task_status', 'description': 'Where a request_task request stands: waiting for the owner, started in their Telegram conversation, declined or expired.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['request_id'],
                     'properties': {'request_id': {'type': 'string', 'maxLength': 16}}}},
    {'name': 'recent_activity', 'description': 'What PROCURATOR did recently: titles and status from its activity log (no message bodies, no health details).',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {'days': {'type': 'integer', 'minimum': 1, 'maximum': 14}}}},
    {'name': 'health_summary', 'description': 'Counts only: upcoming health appointments, items waiting on the owner and refills in progress. Never details.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'properties': {}}},
]
ACTIONS = {'request_task': 'mcp_request', 'task_status': 'mcp_status', 'recent_activity': 'mcp_recent', 'health_summary': 'health_digest'}


def broker(action, args, path=SOCK, timeout=30):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    s.sendall((json.dumps({'action': action, 'args': args}) + '\n').encode())
    buf = b''
    while not buf.endswith(b'\n'):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    r = json.loads(buf or b'{}')
    if not r.get('ok'):
        raise ValueError(str(r.get('error') or 'PROCURATOR did not answer.'))
    return r['result']


def handle(msg, call=broker):
    """One JSON-RPC message in, one response (or None for a notification) out."""
    mid, method, params = msg.get('id'), msg.get('method'), msg.get('params') or {}
    if mid is None:
        return None   # notifications (initialized, cancelled) need no answer
    ok = lambda result: {'jsonrpc': '2.0', 'id': mid, 'result': result}
    if method == 'initialize':
        return ok({'protocolVersion': params.get('protocolVersion') or PROTOCOL, 'capabilities': {'tools': {'listChanged': False}},
                   'serverInfo': {'name': 'procurator', 'version': '0.25.0'},
                   'instructions': 'PROCURATOR runs tasks only after its owner starts them in Telegram. Use request_task, then task_status.'})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': TOOLS})
    if method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        if name not in ACTIONS:
            return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32602, 'message': 'Unknown tool: %s' % str(name)[:40]}}
        try:
            if name == 'request_task':
                args = {'text': str(args.get('task') or ''), 'client': str(args.get('client') or 'MCP client')}
            elif name == 'task_status':
                args = {'id': str(args.get('request_id') or '')}
            out = call(ACTIONS[name], args)
            return ok({'content': [{'type': 'text', 'text': json.dumps(out)}], 'isError': False})
        except Exception as e:
            return ok({'content': [{'type': 'text', 'text': str(e)[:500]}], 'isError': True})
    return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'Method not found'}}


def main(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            stdout.write(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}) + '\n')
            stdout.flush()
            continue
        out = handle(msg) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out) + '\n')
            stdout.flush()


if __name__ == '__main__':
    main()
