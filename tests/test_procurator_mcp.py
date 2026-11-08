"""bin/procurator-mcp.py (0.25.0): MCP handshake, tool list, calls mapped to broker actions, errors; and the request queue."""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('procurator_mcp', ROOT / 'bin' / 'procurator-mcp.py')
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
sys.path.insert(0, str(ROOT / 'integrations' / 'web'))
from mcp_requests import McpRequests   # noqa: E402


class Server(unittest.TestCase):
    def test_handshake_list_and_calls(self):
        calls = []
        fake = lambda action, args: (calls.append((action, args)) or {'request_id': 'abcd1234', 'status': 'waiting_for_owner'})
        r = mcp.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}}, fake)
        self.assertEqual(r['result']['serverInfo']['name'], 'procurator')
        self.assertIsNone(mcp.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, fake))
        names = [t['name'] for t in mcp.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}, fake)['result']['tools']]
        self.assertEqual(names, ['request_task', 'task_status', 'recent_activity', 'health_summary'])
        for banned in ('send', 'approve', 'call', 'mail', 'browser'):
            self.assertFalse(any(banned in n for n in names))
        r = mcp.handle({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'request_task', 'arguments': {'task': 'Book a dentist cleaning next week', 'client': 'Claude Desktop'}}}, fake)
        self.assertFalse(r['result']['isError'])
        self.assertEqual(calls[-1], ('mcp_request', {'text': 'Book a dentist cleaning next week', 'client': 'Claude Desktop'}))
        mcp.handle({'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'health_summary', 'arguments': {}}}, fake)
        self.assertEqual(calls[-1][0], 'health_digest')
        self.assertEqual(mcp.handle({'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'mail_send'}}, fake)['error']['code'], -32602)
        self.assertEqual(mcp.handle({'jsonrpc': '2.0', 'id': 6, 'method': 'resources/list'}, fake)['error']['code'], -32601)
        def boom(a, b):
            raise ValueError('PROCURATOR did not answer.')
        self.assertTrue(mcp.handle({'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call', 'params': {'name': 'task_status', 'arguments': {'request_id': 'x'}}}, boom)['result']['isError'])

    def test_stdio_loop(self):
        out = io.StringIO()
        mcp.main(io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n\n'), out)
        lines = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual(lines[0]['result'], {})
        self.assertEqual(lines[1]['error']['code'], -32700)


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sent, self.t = [], [1_800_000_000.0]
        self.q = McpRequests(self.tmp.name + '/mcp.sqlite3', self.sent.append, now=lambda: self.t[0])

    def tearDown(self):
        self.tmp.cleanup()

    def test_request_waits_for_the_owner_then_starts_once(self):
        r = self.q.request({'text': 'Email Maria the agenda', 'client': 'Claude Desktop<script>'})
        self.assertEqual(r['status'], 'waiting_for_owner')
        self.assertIn('/mcprun ' + r['request_id'], self.sent[-1])
        self.assertIn('Claude Desktopscript', self.sent[-1])
        self.assertEqual(self.q.status({'id': r['request_id']})['status'], 'waiting_for_owner')
        a = self.q.accept(r['request_id'])
        self.assertEqual(a['text'], 'Email Maria the agenda')
        self.assertEqual(self.q.status({'id': r['request_id']})['status'], 'started_in_his_telegram_conversation')
        with self.assertRaisesRegex(ValueError, 'already started'):
            self.q.accept(r['request_id'])

    def test_expiry_limits_and_validation(self):
        r = self.q.request({'text': 'Call the bank'})
        self.t[0] += 86401
        self.assertEqual(self.q.status({'id': r['request_id']})['status'], 'expired')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.q.accept(r['request_id'])
        with self.assertRaises(ValueError):
            self.q.request({'text': 'x'})
        for i in range(20):
            self.q.request({'text': 'task %d' % i})
        with self.assertRaisesRegex(ValueError, 'Twenty'):
            self.q.request({'text': 'one more'})


if __name__ == '__main__':
    unittest.main()
