#!/usr/bin/env python3
"""Configure the OpenClaw gateway for PROCURATOR on a fresh box (run by bin/install.sh; idempotent; safe to re-run).

Mirrors the settings a long-running PROCURATOR box has accumulated, minus anything personal: the gateway basics, the main
agent's defaults (model, timeouts, prompt-cache friendly pruning and compaction, no heartbeat), the PROCURATOR plugins loaded
from this checkout with their hooks allowed, every tool those plugins declare allow-listed (experimental modules excepted),
DuckDuckGo web search, a daily fresh conversation at 4 AM, environment-variable secrets, and the bundled skills PROCURATOR does
not use switched off. Telegram and the owner are configured later by setup (integrations/web/setup_core.telegram_batch).

The settings go through OpenClaw's own `config set --batch-file` in a one-off container, so its schema validation applies. If the
batch is refused, each setting is retried alone and the ones the installed OpenClaw does not accept are listed (not fatal unless
the plugin load itself fails).

    sudo python3 bin/configure-gateway.py [--timezone America/New_York] [--with-voice] [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGINS = ['web-plugin', 'mail-plugin']                  # voice-plugin joins with --with-voice (after Twilio is connected)
EXPERIMENTAL_PREFIXES = ('social_', 'home_')            # modules shipped off by default (docs/modules.md)
SKILLS_OFF = ['1password', 'blogwatcher', 'blucli', 'camsnap', 'coding-agent', 'eightctl', 'gemini', 'gh-issues', 'gifgrep', 'github', 'gog',
              'goplaces', 'himalaya', 'mcporter', 'model-usage', 'nano-pdf', 'obsidian', 'openai-whisper', 'openai-whisper-api', 'openhue', 'oracle',
              'ordercli', 'sag', 'sherpa-onnx-tts', 'songsee', 'sonoscli', 'spotify-player', 'summarize', 'tmux', 'trello', 'xurl']
CONTAINER_STATE = '/home/node/.openclaw'


def plugin_manifest(name):
    return json.loads((ROOT / 'integrations' / name / 'openclaw.plugin.json').read_text())


def batch(timezone, with_voice=False, existing=None, ddg=True):
    plugins = PLUGINS + (['voice-plugin'] if with_voice else [])
    container_root = os.environ.get('PROCURATOR_CONTAINER_ROOT', str(ROOT))   # compose mounts the plugins at the same path
    paths = [container_root + '/integrations/' + p for p in plugins]
    tools, entries = set(), {}
    for p in plugins:
        m = plugin_manifest(p)
        tools |= set((m.get('contracts') or {}).get('tools') or [])
        e = {'enabled': True}
        if p in ('web-plugin', 'voice-plugin'):
            e['hooks'] = {'allowConversationAccess': True, 'allowPromptInjection': True}
        entries[m['id']] = e
    tools = {t for t in tools if not t.startswith(EXPERIMENTAL_PREFIXES)} | {'web_search', 'web_fetch'}
    prev = set(((existing or {}).get('tools') or {}).get('alsoAllow') or [])
    also = sorted(tools | prev)                                   # keep what the owner (or an earlier release) added
    out = []
    for item in json.loads((ROOT / 'config.bootstrap.json').read_text()):
        if item['path'] != 'agents.defaults.timeoutSeconds':
            out.append(item)
    out += [
        {'path': 'agents.defaults.maxConcurrent', 'value': 2},
        {'path': 'agents.defaults.timeoutSeconds', 'value': 600},
        {'path': 'agents.defaults.heartbeat', 'value': {'agentId': 'main', 'every': '0m'}},
        {'path': 'agents.defaults.systemAgent', 'value': {'agentId': 'main'}},
        {'path': 'agents.defaults.userTimezone', 'value': timezone},
        {'path': 'agents.defaults.models', 'value': {'anthropic/claude-sonnet-5-5': {'alias': 'sonnet', 'params': {
            'cacheRetention': 'short', 'anthropicServerCompaction': True, 'anthropicCompactThreshold': 80000}}}},
        {'path': 'agents.defaults.contextPruning', 'value': {'mode': 'cache-ttl', 'ttl': '5m', 'tools': {'allow': ['web_browser_snapshot', 'web_fetch', 'web_search']},
            'hardClear': {'enabled': True, 'placeholder': '[Older read-only page result pruned. Inspect a fresh source; saved approvals and task receipts remain authoritative.]'}}},
        {'path': 'agents.defaults.compaction', 'value': {'mode': 'safeguard', 'identifierPolicy': 'strict', 'recentTurnsPreserve': 6, 'midTurnPrecheck': {'enabled': True}}},
        {'path': 'plugins.load.paths', 'value': paths},
    ]
    for pid, e in entries.items():
        out.append({'path': 'plugins.entries.' + pid, 'value': e})
    out += [
        {'path': 'plugins.entries.anthropic', 'value': {'enabled': True, 'config': {'sessionCatalog': {'enabled': False}}}},
        {'path': 'tools.profile', 'value': 'messaging'},
        {'path': 'tools.alsoAllow', 'value': also},
        {'path': 'tools.deny', 'value': ['voice_call']},
        {'path': 'tools.loopDetection', 'value': {'enabled': True}},
        {'path': 'tools.web', 'value': {**({'search': {'enabled': True, 'provider': 'duckduckgo', 'maxResults': 5, 'timeoutSeconds': 20}} if ddg else {}),
                                        'fetch': {'enabled': True, 'maxChars': 12000, 'maxCharsCap': 20000, 'maxResponseBytes': 750000}}},
        {'path': 'session.reset', 'value': {'mode': 'daily', 'atHour': 4}},
        {'path': 'secrets.providers.default', 'value': {'source': 'env'}},
    ]
    if ddg:
        out.append({'path': 'plugins.entries.duckduckgo', 'value': {'enabled': True}})
    for s in SKILLS_OFF:
        out.append({'path': 'skills.entries.' + s, 'value': {'enabled': False}})
    return out


ONE_OFF = ['docker', 'compose', 'run', '--rm', '--no-deps', '--entrypoint', 'node', 'openclaw-gateway']


def live_runner(run):
    """A running gateway holds the config lock, and a one-off container cannot take it (different PID namespace:
    "GatewayLockNamespaceError"). When the gateway is up (re-running the installer), run the same CLI commands inside it."""
    ps = run(['docker', 'compose', 'ps', '--status', 'running', '-q', 'openclaw-gateway'])
    if getattr(ps, 'returncode', 1) != 0 or not str(getattr(ps, 'stdout', '') or '').strip():
        return run

    def inside(cmd):
        if cmd[:len(ONE_OFF)] == ONE_OFF:
            cmd = ['docker', 'compose', 'exec', '-T', 'openclaw-gateway', 'node'] + cmd[len(ONE_OFF):]
        return run(cmd)
    return inside


def ensure_config(run, state_dir):
    """A brand-new box has no openclaw.json, and `config set` needs one. Create it the way OpenClaw documents for scripts
    (non-interactive local onboarding, no channels/skills/daemon/UI, gateway token from the environment). If this OpenClaw does not
    accept those flags, seed a minimal file instead; the batch below fills in the rest. Returns how it was created, or None."""
    cfg = state_dir / 'openclaw.json'
    if cfg.exists():
        return None
    onboard = ['docker', 'compose', 'run', '--rm', '--no-deps', '--entrypoint', 'node', 'openclaw-gateway', 'dist/index.js', 'onboard',
               '--non-interactive', '--accept-risk', '--mode', 'local', '--flow', 'quickstart', '--auth-choice', 'skip', '--skip-channels',
               '--skip-skills', '--skip-daemon', '--skip-ui', '--gateway-auth', 'token', '--gateway-token-ref-env', 'OPENCLAW_GATEWAY_TOKEN']
    if run(onboard).returncode == 0 and cfg.exists():
        return 'onboard'
    cfg.write_text(json.dumps({'gateway': {'mode': 'local'}}, indent=2) + '\n')
    os.chmod(cfg, 0o600)
    try:
        os.chown(cfg, 1000, 1000)
    except (PermissionError, OSError):
        pass
    return 'seeded'


def ensure_duckduckgo(run, state_dir):
    """Web search without an API key: OpenClaw's official DuckDuckGo plugin (installed once, kept in the state directory)."""
    ext = state_dir / 'extensions'
    if ext.exists() and any('duckduckgo' in p.name for p in ext.iterdir()):
        return True
    cmd = ['docker', 'compose', 'run', '--rm', '--no-deps', '--entrypoint', 'node', 'openclaw-gateway', 'dist/index.js', 'plugins', 'install',
           '@openclaw/duckduckgo-plugin']
    return run(cmd).returncode == 0


def apply(items, run, state_dir):
    """Write the batch where the container sees it and apply it. Returns the paths the installed OpenClaw refused."""
    state_dir.mkdir(parents=True, exist_ok=True)
    f = state_dir / 'procurator-config.json'
    cmd = ['docker', 'compose', 'run', '--rm', '--no-deps', '--entrypoint', 'node', 'openclaw-gateway', 'dist/index.js', 'config', 'set',
           '--batch-file', CONTAINER_STATE + '/procurator-config.json']

    def write(xs):
        f.write_text(json.dumps(xs))
        os.chmod(f, 0o600)
        try:
            os.chown(f, 1000, 1000)
        except (PermissionError, OSError):
            pass

    write(items)
    refused = []
    first = run(cmd)
    if first.returncode != 0:
        # Show why (the CLI's own message, last lines only; it never contains a secret: the batch holds none).
        msg = ((getattr(first, 'stdout', '') or '') + (getattr(first, 'stderr', '') or '')).strip().splitlines()[-12:]
        if msg:
            print('OpenClaw refused the batch:\n  ' + '\n  '.join(msg))
        for it in items:
            write([it])
            if run(cmd).returncode != 0:
                refused.append(it['path'])
    try:
        f.unlink()
    except OSError:
        pass
    return refused


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--timezone', default=os.environ.get('TZ') or 'America/New_York')
    ap.add_argument('--with-voice', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='print the settings and change nothing')
    a = ap.parse_args(argv)
    state = ROOT / 'state'
    existing = None
    try:
        existing = json.loads((state / 'openclaw.json').read_text())
    except (OSError, ValueError):
        pass
    if a.dry_run:
        print(json.dumps(batch(a.timezone, a.with_voice, existing), indent=1))
        return 0
    if os.geteuid() != 0:
        raise SystemExit('Run as root: sudo python3 bin/configure-gateway.py')
    run = lambda cmd: subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    run = live_runner(run)
    how = ensure_config(run, state)
    if how:
        print('Created the gateway config file (%s).' % how)
    ddg = ensure_duckduckgo(run, state)
    if not ddg:
        print('Web search plugin (DuckDuckGo) could not be installed now; web pages can still be read. Re-run this script later to add it.')
    items = batch(a.timezone, a.with_voice, existing, ddg=ddg)
    refused = apply(items, run, state)
    fatal = [p for p in refused if p.startswith('plugins.')]
    print('Gateway configured: %d settings, %d tools allowed%s.' % (len(items) - len(refused), len(next(i for i in items if i['path'] == 'tools.alsoAllow')['value']),
          (', not accepted by this OpenClaw: ' + ', '.join(refused)) if refused else ''))
    if fatal:
        raise SystemExit('The PROCURATOR plugins could not be registered with the gateway (%s). See: docker compose logs --tail 50' % ', '.join(fatal))
    return 0


if __name__ == '__main__':
    sys.exit(main())
