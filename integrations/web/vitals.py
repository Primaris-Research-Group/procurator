"""Droplet vitals sampler for the dashboard (Nov 6 2026): CPU, memory, disk, load and the gateway container's memory.

One row a minute into vitals.sqlite3, 24 hours kept. Read-only on the system; it never changes anything
and reports nothing that identifies a person. The builders report their own vitals in their `hello`
(worker 1.3.0); those are read from the build store, not sampled here.
"""
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager

KEEP_SECONDS = 24 * 3600
GATEWAY = 'procurator-openclaw-gateway-1'


def cpu_times():
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()
        values = [int(x) for x in parts[1:8]]
        idle = values[3] + values[4]
        return idle, sum(values)
    except Exception:
        return None


def cpu_percent(before, after):
    if not before or not after or after[1] <= before[1]:
        return None
    return round(100.0 * (1 - (after[0] - before[0]) / (after[1] - before[1])), 1)


def memory():
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, _, rest = line.partition(':')
                info[k] = int(rest.split()[0]) * 1024
        total, avail = info['MemTotal'], info.get('MemAvailable', info.get('MemFree', 0))
        return {'mem_total': total, 'mem_used': total - avail}
    except Exception:
        return {'mem_total': None, 'mem_used': None}


def disk(path='/'):
    try:
        u = shutil.disk_usage(path)
        return {'disk_total': u.total, 'disk_free': u.free}
    except Exception:
        return {'disk_total': None, 'disk_free': None}


def load1():
    try:
        return round(os.getloadavg()[0], 2)
    except Exception:
        return None


def uptime():
    try:
        with open('/proc/uptime') as f:
            return int(float(f.read().split()[0]))
    except Exception:
        return None


def gateway_memory(container=GATEWAY, timeout=8):
    """Bytes used by the gateway container (docker stats), or None when docker is not reachable."""
    try:
        out = subprocess.run(['docker', 'stats', '--no-stream', '--format', '{{.MemUsage}}', container],
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        used = out.stdout.strip().split('/')[0].strip()
        return parse_size(used)
    except Exception:
        return None


def parse_size(text):
    units = {'B': 1, 'KIB': 1024, 'MIB': 1024 ** 2, 'GIB': 1024 ** 3, 'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3}
    t = text.strip().upper().replace(' ', '')
    for u in sorted(units, key=len, reverse=True):
        if t.endswith(u):
            try:
                return int(float(t[:-len(u)]) * units[u])
            except ValueError:
                return None
    try:
        return int(float(t))
    except ValueError:
        return None


def sample(interval=1.0):
    """One reading. The CPU figure is measured over `interval` seconds."""
    before = cpu_times()
    time.sleep(interval)
    after = cpu_times()
    row = {'cpu': cpu_percent(before, after), 'load1': load1(), 'uptime': uptime(), 'gw_mem': gateway_memory()}
    row.update(memory())
    row.update(disk())
    return row


class Vitals:
    def __init__(self, path, now=time.time):
        self.path, self.now, self.lock = path, now, threading.RLock()
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS droplet(t REAL PRIMARY KEY,cpu REAL,load1 REAL,mem_used INTEGER,mem_total INTEGER,
              disk_free INTEGER,disk_total INTEGER,gw_mem INTEGER,uptime INTEGER);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, row):
        t = self.now()
        with self.lock, self.db() as db:
            db.execute('INSERT OR REPLACE INTO droplet VALUES(?,?,?,?,?,?,?,?,?)',
                       (t, row.get('cpu'), row.get('load1'), row.get('mem_used'), row.get('mem_total'), row.get('disk_free'), row.get('disk_total'), row.get('gw_mem'), row.get('uptime')))
            db.execute('DELETE FROM droplet WHERE t<?', (t - KEEP_SECONDS,))
        return t

    def latest(self):
        with self.db() as db:
            r = db.execute('SELECT * FROM droplet ORDER BY t DESC LIMIT 1').fetchone()
        return dict(r) if r else None

    def series(self, hours=24, points=144):
        """Averaged buckets for charts: [{t, cpu, mem_used, gw_mem, disk_free}] oldest first."""
        since = self.now() - hours * 3600
        with self.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM droplet WHERE t>=? ORDER BY t', (since,))]
        if not rows:
            return []
        step = max(1, len(rows) // points)
        out = []
        for i in range(0, len(rows), step):
            chunk = rows[i:i + step]
            def avg(k):
                vals = [c[k] for c in chunk if c.get(k) is not None]
                return round(sum(vals) / len(vals), 1) if vals else None
            out.append({'t': chunk[-1]['t'], 'cpu': avg('cpu'), 'mem_used': avg('mem_used'), 'gw_mem': avg('gw_mem'), 'disk_free': avg('disk_free'), 'load1': avg('load1')})
        return out

    def start(self, every=60):
        """Background sampler thread (daemon)."""
        def loop():
            while True:
                try:
                    self.record(sample())
                except Exception:
                    pass
                time.sleep(max(5, every - 1))
        t = threading.Thread(target=loop, daemon=True, name='vitals')
        t.start()
        return t
