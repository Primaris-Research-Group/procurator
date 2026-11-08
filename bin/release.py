#!/usr/bin/env python3
"""Release checks (CI and local).

  python3 bin/release.py check      versions in the plugin manifests agree with RELEASE.json
  python3 bin/release.py literals   no personal literal patterns in the tree (public IPs outside the documentation
                                    ranges, consumer mailbox addresses outside example domains, home-folder paths,
                                    9-11 digit chat ids in runtime code)
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {'.git', 'node_modules', '__pycache__', 'vendor', 'dist', 'fixtures'}
TEXT_EXT = {'.py', '.mjs', '.cjs', '.js', '.json', '.sh', '.in', '.md', '.yaml', '.yml', '.html', '.css', '.txt', '.service', '.timer', '.conf', '.toml'}
SVG_PATH = re.compile(r'\bd="|<path|<svg')   # SVG path data looks like dotted numbers; skip those lines for the IP rule

IPV4 = re.compile(r'\b(?!(?:127|10|0|255|100|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168|203\.0\.113|198\.51\.100|192\.0\.2|198\.1[89]|93\.184\.216)\.)(?:\d{1,3}\.){3}\d{1,3}\b')
# a real-looking mailbox at a consumer provider: six or more characters before the @, not a placeholder word
MAILBOX = re.compile(r'(?<![<\w-])(?!(?:user|owner|other|you|alice|bob|test|someone|example|sample)\b)[A-Za-z0-9._%+-]{6,}@(?:gmail|icloud|me|outlook|hotmail|yahoo|proton|protonmail)\.(?:com|me)\b')
HOME = re.compile(r'/Users/[A-Za-z]|C:\\\\Users\\\\[A-Za-z]|/home/(?!node\b|relayadmin\b|mailrelay\b)[a-z][a-z0-9_-]+')
CHAT_ID = re.compile(r"OWNER\s*=\s*['\"]\d{9,11}['\"]")


def files():
    for p in ROOT.rglob('*'):
        if not p.is_file() or set(p.relative_to(ROOT).parts[:-1]) & SKIP_DIRS or p.suffix not in TEXT_EXT:
            continue
        yield p


def check():
    rel = json.loads((ROOT / 'RELEASE.json').read_text())
    bad = []
    for plugin, key in (('web-plugin', 'web-plugin'), ('mail-plugin', 'mail-plugin'), ('voice-plugin', 'voice-plugin')):
        for f in ('package.json', 'openclaw.plugin.json'):
            p = ROOT / 'integrations' / plugin / f
            if not p.exists():
                continue
            v = json.loads(p.read_text()).get('version')
            if v != rel['components'][key]:
                bad.append('%s/%s says %s, RELEASE.json says %s' % (plugin, f, v, rel['components'][key]))
    if rel['components']['web-plugin'] != rel['version']:
        bad.append('web-plugin version is the product version; RELEASE.json disagrees with itself')
    for b in bad:
        print('VERSION: ' + b)
    return 1 if bad else 0


def literals():
    hits = []
    for p in files():
        rel = p.relative_to(ROOT).as_posix()
        try:
            text = p.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            continue
        is_test = p.name.startswith('test_') or '.test.' in p.name or rel.startswith('tests/')
        for i, line in enumerate(text.splitlines(), 1):
            for name, rx in (('ip', IPV4), ('mailbox', MAILBOX), ('home-path', HOME), ('chat-id', CHAT_ID)):
                if is_test and name in ('chat-id', 'home-path'):
                    continue
                if name == 'ip' and SVG_PATH.search(line):
                    continue
                m = rx.search(line)
                if m and not ('example' in m.group(0)):
                    hits.append('%s:%d [%s] %s' % (rel, i, name, m.group(0)[:40]))
    for h in hits[:200]:
        print('LITERAL: ' + h)
    return 1 if hits else 0


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'check'
    sys.exit({'check': check, 'literals': literals}.get(cmd, check)())
