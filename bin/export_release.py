#!/usr/bin/env python3
"""Export a clean, de-personalized copy of a PROCURATOR working tree for the public repository.

The working copy that deploys one owner's box carries that owner's literals (chat id, addresses, host, phone, names) and files
that are nobody else's business (diagnostics, personal task packets, vendored secrets). The public tree is therefore an
EXPORT, never a push: this script copies only allow-listed paths, renames files, applies reviewed mechanical rewrites and
exact patches, lays an overlay (README, LICENSE, CI, config examples) on top, regenerates derived files, and refuses to
finish while any denylisted string survives.

    python3 bin/export_release.py --src . --out /path/to/release --rules release/rules.json [--report out.md] [--check-only]

rules.json (kept with the working copy; NOT exported, because its rewrite table names the literals it removes):
    {
      "allow":    ["integrations/web/**", ...],          # globs relative to --src; everything else is dropped
      "exclude":  ["**/__pycache__/**", ...],            # globs removed even when allowed
      "rename":   [["procurator-", "procurator-"], ...],    # literal path-segment replacements applied to every exported path
      "patches":  [{"file": "...", "find": "...", "replace": "...", "count": 1}, {"file": "...", "prepend": "..."}],
      "rewrites": [{"label": "...", "pattern": "...", "repl": "...", "files": "glob or null", "regex": true}],
      "overlay":  "release/overlay",                      # directory copied over the output last
      "denylist": "release/denylist.local.txt",           # one literal or /regex/ per line; the export fails on any hit
      "denylist_allow": ["tests/fixtures/allowed/**"],    # paths where hits are tolerated
      "post": [["python3", "integrations/web/connectors.py", "policy", "integrations/web-plugin/connector-policy.json"], ...]
    }

Patches run before rewrites (they match the original text exactly once, or the export fails); rewrites run in order over
every text file; the overlay is copied last; post commands run inside the output tree; the denylist scan is the final gate.
Text files are detected by content (no NUL bytes in the first 8 KB). Binary files are copied untouched.
"""
import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

TEXT_PROBE = 8192


def is_text(path):
    try:
        with open(path, 'rb') as f:
            return b'\0' not in f.read(TEXT_PROBE)
    except OSError:
        return False


def glob_match(rel, patterns):
    """fnmatch with '**' meaning any number of path segments (fnmatch alone treats '**' like '*')."""
    rel = rel.replace(os.sep, '/')
    for pat in patterns:
        pat = pat.replace(os.sep, '/')
        rx = _glob_rx(pat)
        if rx.match(rel):
            return True
    return False


_rx_cache = {}


def _glob_rx(pat):
    if pat in _rx_cache:
        return _rx_cache[pat]
    out = ''
    i = 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith('**/', i):
            out += '(?:.*/)?'
            i += 3
            continue
        if pat.startswith('**', i):
            out += '.*'
            i += 2
            continue
        if c == '*':
            out += '[^/]*'
        elif c == '?':
            out += '[^/]'
        else:
            out += re.escape(c)
        i += 1
    rx = re.compile('^' + out + '$')
    _rx_cache[pat] = rx
    return rx


def load_denylist(path):
    items = []
    for raw in Path(path).read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('/') and line.endswith('/') and len(line) > 2:
            items.append((line, re.compile(line[1:-1])))
        else:
            items.append((line, re.compile(re.escape(line), re.IGNORECASE if line.islower() else 0)))
    return items


class Export:
    def __init__(self, src, out, rules, report=None, check_only=False):
        self.src, self.out = Path(src).resolve(), Path(out).resolve()
        self.rules = rules
        self.report_path = report
        self.check_only = check_only
        self.log = {'copied': [], 'dropped': [], 'renamed': [], 'patched': [], 'rewrites': {}, 'overlay': [], 'post': [], 'denylist': []}

    # ---------- 1. select ----------
    def select(self):
        allow, exclude = self.rules.get('allow', []), self.rules.get('exclude', [])
        out_rel = None
        try:
            out_rel = str(self.out.relative_to(self.src)).replace(os.sep, '/')
        except ValueError:
            pass
        for root, dirs, files in os.walk(self.src):
            rel_root = os.path.relpath(root, self.src).replace(os.sep, '/')
            rel_root = '' if rel_root == '.' else rel_root
            # never descend into the output tree or .git
            dirs[:] = [d for d in dirs if d != '.git' and not (out_rel and (rel_root + '/' + d if rel_root else d) == out_rel)]
            for name in files:
                rel = (rel_root + '/' + name) if rel_root else name
                if glob_match(rel, allow) and not glob_match(rel, exclude):
                    yield rel
                else:
                    self.log['dropped'].append(rel)

    # ---------- 2. copy + rename ----------
    def dest_for(self, rel):
        parts = rel.split('/')
        new = []
        for p in parts:
            q = p
            for a, b in self.rules.get('rename', []):
                q = q.replace(a, b)
            new.append(q)
        return '/'.join(new)

    def copy(self):
        if self.out.exists():
            for child in self.out.iterdir():
                if child.name == '.git':
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        self.out.mkdir(parents=True, exist_ok=True)
        for rel in self.select():
            dst_rel = self.dest_for(rel)
            dst = self.out / dst_rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.src / rel, dst)
            self.log['copied'].append(dst_rel)
            if dst_rel != rel:
                self.log['renamed'].append((rel, dst_rel))

    # ---------- 3. patches ----------
    def patch(self):
        for p in self.rules.get('patches', []):
            path = self.out / self.dest_for(p['file'])
            if not path.exists():
                if p.get('optional'):
                    continue
                raise SystemExit('patch target missing: %s' % p['file'])
            text = path.read_text(encoding='utf-8')
            if 'prepend' in p:
                text = p['prepend'] + text
            else:
                n = text.count(p['find'])
                want = p.get('count', 1)
                if n != want:
                    raise SystemExit('patch %s: expected %d match(es) of %r, found %d' % (p['file'], want, p['find'][:60], n))
                text = text.replace(p['find'], p['replace'])
            path.write_text(text, encoding='utf-8')
            self.log['patched'].append(p['file'])

    # ---------- 4. rewrites ----------
    def rewrite(self):
        rules = []
        for r in self.rules.get('rewrites', []):
            flags = re.M | (re.I if r.get('ignorecase') else 0)
            rx = re.compile(r['pattern'] if r.get('regex', True) else re.escape(r['pattern']), flags)
            rules.append((r, rx))
        for path in sorted(self.out.rglob('*')):
            if not path.is_file() or '/.git/' in path.as_posix() or not is_text(path):
                continue
            rel = path.relative_to(self.out).as_posix()
            try:
                text = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            orig = text
            for r, rx in rules:
                if r.get('files') and not glob_match(rel, [r['files']] if isinstance(r['files'], str) else r['files']):
                    continue
                if r.get('exclude_files') and glob_match(rel, [r['exclude_files']] if isinstance(r['exclude_files'], str) else r['exclude_files']):
                    continue
                text, n = rx.subn(r['repl'], text)
                if n:
                    self.log['rewrites'][r.get('label', r['pattern'])] = self.log['rewrites'].get(r.get('label', r['pattern']), 0) + n
            if text != orig:
                path.write_text(text, encoding='utf-8')

    # ---------- 5. overlay ----------
    def overlay(self):
        ov = self.rules.get('overlay')
        if not ov:
            return
        ov = (self.src / ov) if not os.path.isabs(ov) else Path(ov)
        for root, _dirs, files in os.walk(ov):
            for name in files:
                srcp = Path(root) / name
                rel = srcp.relative_to(ov).as_posix()
                dst = self.out / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(srcp, dst)
                self.log['overlay'].append(rel)

    # ---------- 6. post commands ----------
    def post(self):
        for cmd in self.rules.get('post', []):
            r = subprocess.run(cmd, cwd=self.out, capture_output=True, text=True)
            self.log['post'].append((cmd, r.returncode, (r.stdout + r.stderr)[-400:]))
            if r.returncode != 0:
                raise SystemExit('post command failed (%s): %s' % (' '.join(cmd), (r.stdout + r.stderr)[-800:]))

    # ---------- 7. denylist gate ----------
    def scan(self):
        deny = load_denylist(self.src / self.rules['denylist']) if self.rules.get('denylist') else []
        allow_paths = self.rules.get('denylist_allow', [])
        hits = []
        for path in sorted(self.out.rglob('*')):
            if not path.is_file() or '/.git/' in path.as_posix() or not is_text(path):
                continue
            rel = path.relative_to(self.out).as_posix()
            if glob_match(rel, allow_paths):
                continue
            try:
                text = path.read_text(encoding='utf-8')
            except UnicodeDecodeError:
                continue
            for label, rx in deny:
                for i, line in enumerate(text.splitlines(), 1):
                    if rx.search(line):
                        hits.append((rel, i, label))
        # file names too
        for path in self.out.rglob('*'):
            rel = path.relative_to(self.out).as_posix()
            for label, rx in deny:
                if rx.search(rel):
                    hits.append((rel, 0, label))
        self.log['denylist'] = hits
        return hits

    # ---------- report ----------
    def report(self):
        L = self.log
        lines = ['# Export report', '', 'Source: `%s`' % self.src, 'Output: `%s`' % self.out, '',
                 '| Measure | Count |', '|---|---|',
                 '| Files exported | %d |' % len(L['copied']), '| Files dropped (not allow-listed or excluded) | %d |' % len(L['dropped']),
                 '| Paths renamed | %d |' % len(L['renamed']), '| Exact patches applied | %d |' % len(L['patched']),
                 '| Overlay files | %d |' % len(L['overlay']), '| Denylist hits | %d |' % len(L['denylist']), '',
                 '## Rewrites (label → replacements)', '']
        for k, v in sorted(L['rewrites'].items(), key=lambda kv: -kv[1]):
            lines.append('- %s: %d' % (k, v))
        lines += ['', '## Renamed paths', ''] + ['- `%s` → `%s`' % rn for rn in L['renamed'][:400]]
        lines += ['', '## Post commands', ''] + ['- `%s` → exit %d' % (' '.join(c), rc) for c, rc, _ in L['post']]
        lines += ['', '## Dropped files (first 600)', ''] + ['- `%s`' % d for d in sorted(L['dropped'])[:600]]
        if L['denylist']:
            lines += ['', '## DENYLIST HITS (export is NOT clean)', ''] + ['- `%s:%d` [%s]' % h for h in L['denylist'][:200]]
        text = '\n'.join(lines) + '\n'
        if self.report_path:
            Path(self.report_path).write_text(text, encoding='utf-8')
        return text

    def run(self):
        if self.check_only:
            hits = self.scan()
            print(self.report())
            return 1 if hits else 0
        self.copy()
        self.patch()
        self.rewrite()
        self.overlay()
        self.post()
        hits = self.scan()
        self.report()
        print('exported %d files, dropped %d, renamed %d, patched %d, overlay %d' % (
            len(self.log['copied']), len(self.log['dropped']), len(self.log['renamed']), len(self.log['patched']), len(self.log['overlay'])))
        if hits:
            print('DENYLIST: %d hit(s); the export is not clean. First hits:' % len(hits), file=sys.stderr)
            seen = 0
            for rel, line, label in hits:
                print('  %s:%d [%s]' % (rel, line, label), file=sys.stderr)
                seen += 1
                if seen >= 40:
                    break
            return 1
        print('denylist: clean')
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--src', default='.')
    ap.add_argument('--out', required=True)
    ap.add_argument('--rules', default='release/rules.json')
    ap.add_argument('--report')
    ap.add_argument('--check-only', action='store_true', help='only run the denylist scan over --out')
    a = ap.parse_args(argv)
    rules = json.loads(Path(a.rules).read_text(encoding='utf-8'))
    return Export(a.src, a.out, rules, a.report, a.check_only).run()


if __name__ == '__main__':
    sys.exit(main())
