"""Record, replay, measure (web 0.17.0, PROCURATOR browsing plan Phase 2).

Live side (inside the broker):
- traces: the driver writes a Playwright trace of every browser act into the inbox; the broker moves it to
  STATE/traces/<task or browser>/<step>.zip and keeps 7 days. Never sent to the model. /webtrace links it on the dashboard.
- captures: on a trouble signal (form_gaps, fill warnings, a choice that did not register, a step that changed nothing,
  a budget pause, needs_attention, a takeover) the driver saves an MHTML archive of the page; the broker stores it with
  the serialized snapshot and a draft expect.json under STATE/fixtures/<site>/<date>-<reason>-<id>/ (30 days).
  Personal data stays on the server: nothing here is copied into the repository automatically.

Offline side (no network, no browser service):
  python3 fixtures.py run [--recorded] [DIR ...]   check fixtures (default: integrations/web/fixtures)
  --recorded checks the saved snapshot.json (no Chrome needed); without it each page.mhtml / source.html is re-serialized
  in a local headless Chrome by web-plugin/runner.fixtures.cjs and the expectations are checked against that.
"""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEEDS = HERE / 'fixtures'
TRACE_DAYS = 7
CAPTURE_DAYS = 30
CAPTURE_REASONS = ('form_gaps', 'fill_warnings', 'choice_not_registered', 'no_change', 'budget', 'needs_attention', 'takeover')

def site_of(url):
    host = urllib.parse.urlsplit(url or '').hostname or 'unknown'
    return re.sub(r'[^a-z0-9.-]', '', host[4:] if host.startswith('www.') else host)[:60] or 'unknown'

def strip_private(snapshot):
    """Selectors are the broker's private handles; they are not part of a fixture."""
    out = dict(snapshot or {})
    out['controls'] = [{k: v for k, v in c.items() if k != 'selector'} for c in out.get('controls', [])]
    return out

def draft_expect(snapshot, url, reason):
    from task_policy import classify_target, form_filled
    controls = snapshot.get('controls', [])
    fields = [{'label': c['label'], 'tag': c['tag']} | ({'options_include': [o['label'] for o in c.get('options', [])][:5]} if c['tag'] == 'select' else {})
              for c in controls if c['tag'] in ('input', 'select', 'textarea') and c.get('label') and c.get('type') not in ('hidden',)][:30]
    targets = [{'label': c['label'], 'class': classify_target(c, {'url': url, 'filled': form_filled(c, controls)})['class']}
               for c in controls if c['tag'] == 'button' or c.get('type') == 'submit'][:12]
    return {'url': url, 'site': site_of(url), 'reason': reason, 'captured': dt.datetime.now().isoformat(timespec='seconds'),
            'controls': fields, 'targets': targets, 'verify': None,
            'note': 'Draft written at capture time. Edit the expectations (what MUST be found) before using it as a regression fixture.'}

class Fixtures:
    def __init__(self, broker, state, inbox):
        self.b, self.state, self.inbox = broker, Path(state), Path(inbox)
        self.traces, self.captures = self.state / 'traces', self.state / 'fixtures'
        with broker.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS fixture_captures(scope TEXT,reason TEXT,ts REAL,path TEXT)')
        self.last_rotate = 0

    def setting(self):
        with self.b.db() as db:
            r = db.execute("SELECT value FROM owner_settings WHERE key='tracing'").fetchone()
        return (r['value'] if r else 'on') != 'off'

    def scrub(self, path):
        """Owner-fact values (vault) are removed from traces and archives (Phase 4)."""
        vault = getattr(self.b, 'vault', None)
        if vault:
            try:
                vault.scrub_file(path)
            except Exception:
                Path(path).unlink(missing_ok=True)     # never keep an unscrubbed copy

    # ---------- traces ----------
    def trace_path(self, plan_id):
        if not self.setting() or not self.inbox.is_dir():
            return None
        return str(self.inbox / ('trace-' + re.sub(r'[^a-f0-9]', '', plan_id)[:16] + '-' + uuid.uuid4().hex[:8] + '.zip'))

    def keep_trace(self, path, scope, plan_id):
        if not path:
            return None
        src = Path(path)
        try:
            if not src.is_file() or src.stat().st_size == 0:
                src.unlink(missing_ok=True)
                return None
            if src.stat().st_size > 60_000_000:
                src.unlink(missing_ok=True)
                return None
            d = self.traces / re.sub(r'[^a-f0-9]', '', scope)[:16]
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest = d / (time.strftime('%Y%m%d-%H%M%S') + '-' + re.sub(r'[^a-f0-9]', '', plan_id)[:16] + '.zip')
            shutil.move(str(src), dest)
            os.chmod(dest, 0o600)
            self.scrub(dest)
            self.rotate()
            return dest
        except OSError:
            return None

    def trace_files(self, scope):
        d = self.traces / re.sub(r'[^a-f0-9]', '', scope or '')[:16]
        if not scope or not d.is_dir():
            return []
        return [{'name': f.name, 'bytes': f.stat().st_size, 'when': f.stat().st_mtime} for f in sorted(d.glob('*.zip'))]

    def trace_file(self, scope, name):
        if not re.fullmatch(r'[a-f0-9]{16}', scope or '') or not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{1,16}\.zip', name or ''):
            raise ValueError('Not found.')
        f = self.traces / scope / name
        if not f.is_file():
            raise ValueError('Not found.')
        return f

    def rotate(self, now=None):
        now = now or time.time()
        if now - self.last_rotate < 600:
            return
        self.last_rotate = now
        for root, days in ((self.traces, TRACE_DAYS), (self.captures, CAPTURE_DAYS)):
            if not root.is_dir():
                continue
            for f in root.rglob('*'):
                try:
                    if f.is_file() and now - f.stat().st_mtime > days * 86400:
                        f.unlink()
                except OSError:
                    pass
            for d in sorted((p for p in root.rglob('*') if p.is_dir()), key=lambda p: -len(p.parts)):
                try:
                    d.rmdir()
                except OSError:
                    pass

    # ---------- captures ----------
    def capture(self, s, reason, scope):
        """Archive the live page once per task and reason (10-minute spacing). Never on a page with a password field."""
        if reason not in CAPTURE_REASONS or not self.setting() or s.get('practice') or s.get('project_source') or s['status'] != 'open' or not self.inbox.is_dir():
            return None
        with self.b.db() as db:
            recent = db.execute('SELECT 1 FROM fixture_captures WHERE scope=? AND reason=? AND ts>?', (scope, reason, time.time() - 600)).fetchone()
            today = db.execute('SELECT COUNT(*) n FROM fixture_captures WHERE ts>?', (time.time() - 86400,)).fetchone()['n']
        if recent or today >= 40:
            return None
        target = self.inbox / ('capture-' + uuid.uuid4().hex[:16] + '.mhtml')
        try:
            out = self.b.provider.run(s, 'capture', capture_path=str(target))
        except Exception:
            out = {'ok': False}
        try:
            if not out.get('ok') or not target.is_file():
                return None
            snap = out.get('snapshot') or {}
            if any(c.get('type') == 'password' or c.get('value') == '[manual entry only]' for c in snap.get('controls', [])):
                return None
            url = out.get('url') or snap.get('url') or ''
            d = self.captures / site_of(url) / (time.strftime('%Y%m%d-%H%M%S') + '-' + reason + '-' + uuid.uuid4().hex[:6])
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.move(str(target), d / 'page.mhtml')
            self.scrub(d / 'page.mhtml')
            vault = getattr(self.b, 'vault', None)
            (d / 'snapshot.json').write_text(json.dumps(vault.redact(strip_private(snap)) if vault else strip_private(snap), indent=1))
            (d / 'expect.json').write_text(json.dumps(draft_expect(snap, url, reason), indent=1))
            for f in d.iterdir():
                os.chmod(f, 0o600)
            with self.b.db() as db:
                db.execute('INSERT INTO fixture_captures VALUES(?,?,?,?)', (scope, reason, time.time(), str(d)))
            self.rotate()
            return d
        finally:
            target.unlink(missing_ok=True)

    def triggers(self, result):
        """Capture reasons visible in one act result."""
        out = result.get('result') or {}
        reasons = []
        snap = out.get('snapshot') or {}
        if snap.get('form_gaps'):
            reasons.append('form_gaps')
        if any(not f.get('ok') for f in out.get('fills') or []):
            reasons.append('fill_warnings')
        if out.get('reason') == 'choice_not_registered':
            reasons.append('choice_not_registered')
        if result.get('status') == 'completed' and out.get('progress') and out['progress'].get('changed') is False:
            reasons.append('no_change')
        return reasons

# ---------- offline checks ----------
def check(expect, snapshot):
    """Failures (strings) of one fixture's expectations against a serialized snapshot. Empty list = pass."""
    from task_policy import classify_target, form_filled, space
    from journal import ALREADY, CONFIRMED
    problems = []
    controls = snapshot.get('controls', [])
    find = lambda label, tag=None: [c for c in controls if space(c.get('label', '')) == space(label) and (not tag or c.get('tag') == tag)]
    for want in expect.get('controls', []):
        hits = find(want['label'], want.get('tag'))
        if not hits:
            problems.append('control missing: %s (%s)' % (want['label'], want.get('tag') or 'any'))
            continue
        labels = {space(o.get('label', '')) for c in hits for o in c.get('options', [])}
        for opt in want.get('options_include', []):
            if space(opt) not in labels:
                problems.append('option missing in %s: %s' % (want['label'], opt))
        if want.get('hidden_native') and not any(c.get('hidden_native') for c in hits):
            problems.append('%s is not exposed as the hidden native select' % want['label'])
    for want in expect.get('targets', []):
        hits = find(want['label'])
        if not hits:
            problems.append('target missing: ' + want['label'])
            continue
        got = classify_target(hits[0], {'url': expect.get('url'), 'filled': form_filled(hits[0], controls), 'total': expect.get('total'), 'signed_in': expect.get('signed_in', False)})['class']
        if got != want['class']:
            problems.append('target %s classed %s, expected %s' % (want['label'], got, want['class']))
    if expect.get('verify'):
        text = snapshot.get('text') or ''
        got = 'submitted' if (ALREADY.search(text) or CONFIRMED.search(text)) else 'not_submitted'
        if got != expect['verify']:
            problems.append('verify-before-retry reads %s, expected %s' % (got, expect['verify']))
    return problems

def fixture_dirs(roots):
    out = []
    for root in roots:
        root = Path(root)
        if (root / 'expect.json').is_file():
            out.append(root)
        elif root.is_dir():
            out.extend(sorted(p.parent for p in root.rglob('expect.json')))
    return out

def serialize(files, node='node'):
    """Re-serialize archives in a local headless Chrome (runner.fixtures.cjs). {file: snapshot or error string}."""
    runner = HERE.parent / 'web-plugin' / 'runner.fixtures.cjs'
    r = subprocess.run([node, str(runner)] + [str(f) for f in files], capture_output=True, text=True, timeout=60 + 20 * len(files))
    out = {}
    for line in r.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if item.get('file'):
            out[item['file']] = item.get('snapshot') if item.get('ok') else 'error: ' + str(item.get('error'))
    for f in files:
        out.setdefault(str(f), 'error: ' + (r.stderr.strip().splitlines() or ['no output from runner.fixtures.cjs'])[-1][:300])
    return out

def run(roots=None, recorded=False, node='node'):
    dirs = fixture_dirs(roots or [SEEDS])
    results = []
    live = {}
    if not recorded:
        files = [next((d / n for n in ('page.mhtml', 'source.html') if (d / n).is_file()), None) for d in dirs]
        live = serialize([f for f in files if f], node) if any(files) else {}
    for d in dirs:
        expect = json.loads((d / 'expect.json').read_text())
        snap = None
        if recorded:
            p = d / 'snapshot.json'
            snap = json.loads(p.read_text()) if p.is_file() else 'error: no snapshot.json'
        else:
            f = next((d / n for n in ('page.mhtml', 'source.html') if (d / n).is_file()), None)
            snap = live.get(str(f)) if f else 'error: no page.mhtml or source.html'
        problems = [snap] if isinstance(snap, str) or snap is None else check(expect, snap)
        results.append({'fixture': str(d), 'name': expect.get('name') or d.name, 'ok': not problems, 'problems': problems})
    return results

def main(argv):
    if not argv or argv[0] not in ('run', 'list'):
        print(__doc__)
        return 2
    args = argv[1:]
    recorded = '--recorded' in args
    roots = [a for a in args if not a.startswith('--')]
    if argv[0] == 'list':
        for d in fixture_dirs(roots or [SEEDS]):
            print(d)
        return 0
    started = time.time()
    results = run(roots, recorded)
    for r in results:
        print(('PASS ' if r['ok'] else 'FAIL ') + r['name'] + ('' if r['ok'] else '\n   ' + '\n   '.join(r['problems'])))
    print('%d/%d fixtures passed in %.1f s (%s)' % (sum(r['ok'] for r in results), len(results), time.time() - started, 'recorded snapshots' if recorded else 'local headless Chrome'))
    return 0 if all(r['ok'] for r in results) else 1

if __name__ == '__main__':
    sys.path.insert(0, str(HERE))
    sys.exit(main(sys.argv[1:]))
