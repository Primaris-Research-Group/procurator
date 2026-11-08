#!/usr/bin/env python3
"""Local, credential-free runtime metrics. Never restarts services or sends messages."""
import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

BASE = Path('/opt/procurator')
OUTPUT = Path('/var/lib/procurator-health/status.json')
CONTAINER = 'procurator-openclaw-gateway-1'


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError('Health check unavailable')
    return result.stdout.strip()


def collect():
    warnings = []
    result = {'checked_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'automatic_restarts': False}
    result['cpu_count'] = os.cpu_count() or 1
    result['load_average'] = list(os.getloadavg())
    if result['load_average'][1] > 2*result['cpu_count']:
        warnings.append('Sustained CPU or runnable-work pressure')
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.strip().split()[0])*1024
    result['memory'] = {k: memory[k] for k in ['MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree']}
    if memory['MemAvailable'] < 256*1024**2:
        warnings.append('Low available host memory')
    disk = shutil.disk_usage(BASE)
    result['disk_free_bytes'] = disk.free
    if disk.free < 2*1024**3:
        warnings.append('Low host disk space')
    try:
        raw = command(['docker', 'inspect', '--format', '{{json .State}}', CONTAINER])
        state = json.loads(raw)
        # Do not retain process commands, errors, logs, env, or healthcheck output.
        result['gateway'] = {'running': state.get('Running') is True, 'oom_killed': state.get('OOMKilled') is True,
                             'health': state.get('Health', {}).get('Status', 'unknown')}
        if not result['gateway']['running'] or result['gateway']['health'] != 'healthy' or result['gateway']['oom_killed']:
            warnings.append('Gateway needs inspection')
    except Exception:
        result['gateway'] = {'health': 'unknown'}
        warnings.append('Gateway health unavailable')
    services = {}
    for service in ['procurator-web', 'procurator-mail', 'procurator-voice-http']:
        try:
            services[service] = command(['systemctl', 'is-active', service])
        except Exception:
            services[service] = 'unavailable'
            warnings.append(service+' needs inspection')
    result['services'] = services
    try:
        with sqlite3.connect('file:/var/lib/procurator-web/browser.sqlite3?mode=ro', uri=True, timeout=2) as db:
            result['browser_actions_by_state'] = dict(db.execute('SELECT status,COUNT(*) FROM plans GROUP BY status'))
            result['browser_sessions_by_state'] = dict(db.execute('SELECT status,COUNT(*) FROM sessions GROUP BY status'))
            result['browser_tasks_by_state'] = dict(db.execute('SELECT status,COUNT(*) FROM web_tasks GROUP BY status'))
    except Exception:
        warnings.append('Browser state counts unavailable')
    result['warnings'] = warnings
    result['status'] = 'attention' if warnings else 'healthy'
    return result


def main():
    os.umask(0o077)
    data = collect()
    OUTPUT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = OUTPUT.with_suffix('.tmp')
    with temp.open('w') as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, OUTPUT)
    print(json.dumps({'status': data['status'], 'warnings': data['warnings']}))


if __name__ == '__main__':
    main()
