"""Durable, replay-safe reception admission after native signature verification."""
import datetime
import hashlib
import sqlite3
import time


class Admission:
    def __init__(self, filename, daily=20, per_caller=3, now=time.time):
        self.filename = filename
        self.daily = daily
        self.per_caller = per_caller
        self.now = now

    def allow(self, call_sid, caller):
        day = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).date().isoformat()
        # Retain a digest for caller throttling, never message content or signatures.
        caller_hash = hashlib.sha256(caller.encode()).hexdigest()
        with sqlite3.connect(self.filename, timeout=5) as db:
            db.execute('CREATE TABLE IF NOT EXISTS admissions (sid TEXT PRIMARY KEY, day TEXT NOT NULL, caller TEXT NOT NULL, allowed INTEGER NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT allowed FROM admissions WHERE sid=?', (call_sid,)).fetchone()
            if previous is not None:
                return bool(previous[0])
            total = db.execute('SELECT COUNT(*) FROM admissions WHERE day=? AND allowed=1', (day,)).fetchone()[0]
            caller_total = db.execute('SELECT COUNT(*) FROM admissions WHERE day=? AND caller=? AND allowed=1', (day, caller_hash)).fetchone()[0]
            allow = total < self.daily and caller_total < self.per_caller
            db.execute('INSERT INTO admissions VALUES (?,?,?,?)', (call_sid, day, caller_hash, int(allow)))
            # Completed previous-day callbacks never qualify for an initial rewrite.
            # Keep a week of replay decisions while bounding private metadata retention.
            cutoff = (datetime.date.fromisoformat(day)-datetime.timedelta(days=7)).isoformat()
            db.execute('DELETE FROM admissions WHERE day<?', (cutoff,))
            return allow
