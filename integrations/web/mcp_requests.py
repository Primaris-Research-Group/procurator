"""Requests from MCP clients (0.25.0): Claude Desktop, Claude Code or ChatGPT hand PROCURATOR a task through bin/procurator-mcp.py.

A request only queues text. The owner sees it in Telegram and starts it with /mcprun ID (it then runs in their own conversation, where
every send, call, booking and submission still needs their approval), or ignores it (it expires after 24 hours). External text never
grants a permission: AGENTS.md, "Only the authenticated owner can submit assignments".
"""
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

LIMIT_PER_DAY = 20
TTL = 86400


class McpRequests:
    def __init__(self, path, telegram, now=time.time):
        self.path, self.telegram, self.now = path, telegram, now
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS mcp_requests(id TEXT PRIMARY KEY,client TEXT,text TEXT,created REAL,status TEXT,answered REAL)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def request(self, a, scrub=lambda s: s):
        text = ' '.join(str(a.get('text') or '').split())
        client = re.sub(r'[^A-Za-z0-9 ._-]', '', str(a.get('client') or 'MCP client'))[:40] or 'MCP client'
        if not 3 <= len(text) <= 2000:
            raise ValueError('task is 3-2000 characters.')
        text = scrub(text)
        t = self.now()
        with self.db() as db:
            if db.execute('SELECT COUNT(*) FROM mcp_requests WHERE created>?', (t - 86400,)).fetchone()[0] >= LIMIT_PER_DAY:
                raise ValueError('Twenty requests already arrived today; the owner can raise this by asking PROCURATOR directly in Telegram.')
            rid = secrets.token_hex(4)
            db.execute("INSERT INTO mcp_requests VALUES(?,?,?,?,'waiting',NULL)", (rid, client, text, t))
        self.telegram('%s asks PROCURATOR to:\n\n%s\n\nStart it: /mcprun %s\nIgnore this message to decline (it expires in 24 hours). Every send, call or booking it '
                      'leads to still asks you first.' % (client, text, rid))
        return {'request_id': rid, 'status': 'waiting_for_owner',
                'note': 'Nothing runs until the owner starts it in Telegram (/mcprun). Check with task_status.'}

    def status(self, a):
        rid = str(a.get('id') or '')
        with self.db() as db:
            r = db.execute('SELECT * FROM mcp_requests WHERE id=?', (rid,)).fetchone()
        if not r:
            raise ValueError('No such request.')
        st = r['status']
        if st == 'waiting' and r['created'] < self.now() - TTL:
            st = 'expired'
        return {'request_id': r['id'], 'status': {'waiting': 'waiting_for_owner', 'started': 'started_in_his_telegram_conversation'}.get(st, st),
                'note': 'Results are reported to the owner in Telegram, not here.' if st == 'started' else None}

    def accept(self, rid):
        """Owner only (/mcprun ID): marks it started and returns the text for the wake."""
        with self.db() as db:
            r = db.execute('SELECT * FROM mcp_requests WHERE id=?', (str(rid or ''),)).fetchone()
            if not r:
                raise ValueError('No such request.')
            if r['status'] != 'waiting':
                raise ValueError('That request was already %s.' % r['status'])
            if r['created'] < self.now() - TTL:
                db.execute("UPDATE mcp_requests SET status='expired' WHERE id=?", (r['id'],))
                raise ValueError('That request expired (24 hours). Ask again from the MCP client.')
            db.execute("UPDATE mcp_requests SET status='started',answered=? WHERE id=?", (self.now(), r['id']))
        return {'request_id': r['id'], 'client': r['client'], 'text': r['text']}
