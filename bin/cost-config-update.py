#!/usr/bin/env python3
"""Cost controls for the assistant (Nov 5 2026). Runs on the droplet during bin/deploy-procurator-web.sh. Idempotent.

Why: the Console showed ~117M input tokens vs ~0.24M output in one day. The model is Sonnet 5.5 with a
1,000,000-token window, so the Telegram conversation was allowed to grow to hundreds of thousands of tokens
before anything trimmed it, and every model call (each browser step, each tool loop turn) re-sent all of it.
Overnight, OpenClaw's default heartbeat (every 30 min with an API key) re-ran that whole conversation twice an
hour, and because the prompt cache only lives 5 minutes, each of those was billed at the full cache-write price.

Changes (each printed before -> after):
  1. Heartbeat cadence OFF (every "0m"). Nothing of ours depends on it: the morning brief, reminders, mission
     work sessions, finished browser tasks and finished calls all wake the assistant through events, which
     still work with the cadence off.
  2. A fresh conversation every night (session.reset daily, 4 AM Eastern). Missions, approvals, reminders,
     preferences, the activity log and saved logins live in their own stores and are not affected.
  3. Anthropic server-side compaction at 80,000 input tokens (120,000 until 0.20.4) for Sonnet 5.5 (instead of ~700,000): older
     conversation is summarized by Anthropic so later calls stay small.
  4. Main assistant prompt cache kept 1 hour instead of 5 minutes, so a reply 10-50 minutes after the last one
     reads the cache (10% price) instead of rewriting it (125%). The phone agent keeps the 5-minute cache.
"""
import json, os, shutil, sys, time

MODEL = 'anthropic/claude-sonnet-5-5'
THRESHOLD = 80000   # 0.20.4 (Nov 6): was 120000; the turn-by-turn context now stays smaller and faster


def reset_hour(tz):
    # session.reset.atHour is the gateway's own clock. The container runs on UTC unless OPENCLAW_TZ is set.
    tz = (tz or 'UTC').strip()
    if tz in ('America/New_York', 'US/Eastern', 'EST5EDT'):
        return 4
    if tz in ('UTC', 'Etc/UTC', 'GMT', ''):
        return 9   # 4 AM EST (5 AM during daylight time)
    raise ValueError('Unexpected gateway time zone %r; set session.reset.atHour by hand.' % tz)


def apply(c, tz='UTC'):
    """Return (config, changes). Raises if the config is not the shape this was written for."""
    agents = c.setdefault('agents', {})
    d = agents.setdefault('defaults', {})
    if (d.get('model') or {}).get('primary') != MODEL:
        raise ValueError('Main model is not %s; inspect before applying cost settings.' % MODEL)
    entries = agents.get('entries') or {}
    changes = []

    def note(what, before, after):
        if before != after:
            changes.append('%s: %s -> %s' % (what, json.dumps(before), json.dumps(after)))

    hb = d.setdefault('heartbeat', {})
    note('heartbeat.every', hb.get('every', '(default 30m)'), '0m'); hb['every'] = '0m'
    for name, e in entries.items():
        if isinstance(e, dict) and isinstance(e.get('heartbeat'), dict):
            note('agents.entries.%s.heartbeat.every' % name, e['heartbeat'].get('every'), '0m'); e['heartbeat']['every'] = '0m'

    s = c.setdefault('session', {})
    cur = s.get('reset')
    if not cur or cur.get('mode') in (None, 'none'):
        new = {'mode': 'daily', 'atHour': reset_hour(tz)}
        note('session.reset', cur or '(none)', new); s['reset'] = new
    else:
        changes.append('session.reset left as set: %s' % json.dumps(cur))

    p = d.setdefault('models', {}).setdefault(MODEL, {}).setdefault('params', {})
    for k, v in (('anthropicServerCompaction', True), ('anthropicCompactThreshold', THRESHOLD)):
        note('%s params.%s' % (MODEL, k), p.get(k), v); p[k] = v

    if 'main' not in entries:
        raise ValueError('agents.entries.main not found; inspect before applying cost settings.')
    mp = entries['main'].setdefault('params', {})
    note('main agent cacheRetention', mp.get('cacheRetention', p.get('cacheRetention', '(default short)')), 'long'); mp['cacheRetention'] = 'long'
    if isinstance(entries.get('phone'), dict):
        pp = entries['phone'].setdefault('params', {})
        if 'cacheRetention' not in pp:
            note('phone agent cacheRetention', '(inherits)', 'short'); pp['cacheRetention'] = 'short'
    return c, changes


def retune_compaction(c):
    """0.20.4: every configured model (Sonnet, Opus, Sol, Terra; main default or per agent) compacts at THRESHOLD. /ai keeps
    a model's existing params, so models switched to before 0.20.4 still carry 120000; lower any value above THRESHOLD.
    Runs whatever the main model is."""
    changes = []
    agents = c.get('agents') or {}
    holders = [('agents.defaults.models.%s' % k, v) for k, v in ((agents.get('defaults') or {}).get('models') or {}).items()]
    holders += [('agents.entries.%s' % k, v) for k, v in (agents.get('entries') or {}).items()]
    for where, h in holders:
        p = h.get('params') if isinstance(h, dict) else None
        if not isinstance(p, dict):
            continue
        for key in ('anthropicCompactThreshold', 'responsesCompactThreshold'):
            if isinstance(p.get(key), int) and p[key] > THRESHOLD:
                changes.append('%s params.%s: %s -> %s' % (where, key, p[key], THRESHOLD)); p[key] = THRESHOLD
    return c, changes


MCP_IDLE_MS = 600000   # 10 minutes


def mcp_idle(c):
    """0.20.7 (Nov 7 browser batteries): OpenClaw 2026.10.9 keeps one set of stdio MCP helper processes (Google, gog,
    workspace) per session and, with mcp.sessionIdleTtlMs unset, never evicts them (OpenClaw #166017, #161311); 12 finished
    test conversations held the gateway at 256 threads and 1.49 of 1.5 GiB. An explicit idle TTL lets the gateway retire the
    helpers of a session idle for 10 minutes; finished background sessions are also deleted by the plugin (retireSession)."""
    mcp = c.setdefault('mcp', {})
    before = mcp.get('sessionIdleTtlMs')
    if isinstance(before, int) and 0 < before <= MCP_IDLE_MS:
        return c, []
    mcp['sessionIdleTtlMs'] = MCP_IDLE_MS
    return c, ['mcp.sessionIdleTtlMs: %s -> %d' % (json.dumps(before) if before is not None else '(unset: never evicted)', MCP_IDLE_MS)]


def self_test():
    base = {'agents': {'defaults': {'model': {'primary': MODEL}, 'heartbeat': {'agentId': 'main'},
                                    'models': {MODEL: {'params': {'cacheRetention': 'short'}}}},
                       'entries': {'main': {'workspace': '/w'}, 'phone': {'model': MODEL}}}}
    c, ch = apply(json.loads(json.dumps(base)))
    d = c['agents']['defaults']
    assert d['heartbeat'] == {'agentId': 'main', 'every': '0m'}
    assert c['session']['reset'] == {'mode': 'daily', 'atHour': 9}
    assert d['models'][MODEL]['params'] == {'cacheRetention': 'short', 'anthropicServerCompaction': True, 'anthropicCompactThreshold': 80000}
    assert c['agents']['entries']['main']['params'] == {'cacheRetention': 'long'}
    assert c['agents']['entries']['phone']['params'] == {'cacheRetention': 'short'}
    assert len(ch) == 6, ch
    again, ch2 = apply(c)
    assert ch2 == ['session.reset left as set: {"mode": "daily", "atHour": 9}'], ch2
    assert apply(json.loads(json.dumps(base)), 'America/New_York')[0]['session']['reset']['atHour'] == 4
    for bad in ({'agents': {'defaults': {'model': {'primary': 'openai/x'}}}},
                {'agents': {'defaults': {'model': {'primary': MODEL}}, 'entries': {}}}):
        try:
            apply(bad); raise AssertionError('should refuse')
        except ValueError:
            pass
    try:
        reset_hour('Asia/Tokyo'); raise AssertionError('should refuse')
    except ValueError:
        pass
    many = {'agents': {'defaults': {'models': {'openai/gpt-6.1-terra': {'params': {'responsesServerCompaction': True, 'responsesCompactThreshold': 120000}},
                                               'anthropic/claude-opus-5-5': {'params': {'anthropicCompactThreshold': 60000}}}},
                       'entries': {'main': {'params': {'cacheRetention': 'long', 'anthropicCompactThreshold': 120000}}}}}
    tuned, ch3 = retune_compaction(many)
    assert tuned['agents']['defaults']['models']['openai/gpt-6.1-terra']['params']['responsesCompactThreshold'] == THRESHOLD
    assert tuned['agents']['defaults']['models']['anthropic/claude-opus-5-5']['params']['anthropicCompactThreshold'] == 60000   # lower values kept
    assert tuned['agents']['entries']['main']['params']['anthropicCompactThreshold'] == THRESHOLD and len(ch3) == 2, ch3
    assert retune_compaction(tuned)[1] == []
    m, chm = mcp_idle({'mcp': {'servers': {'google': {}}}})
    assert m['mcp']['sessionIdleTtlMs'] == MCP_IDLE_MS and chm == ['mcp.sessionIdleTtlMs: (unset: never evicted) -> 600000'], chm
    assert mcp_idle(m)[1] == [] and mcp_idle({'mcp': {'sessionIdleTtlMs': 120000}})[1] == []
    assert mcp_idle({'mcp': {'sessionIdleTtlMs': 0}})[1] == ['mcp.sessionIdleTtlMs: 0 -> 600000']
    print('cost-config self-test OK')


if __name__ == '__main__':
    if sys.argv[1:2] == ['--self-test']:
        self_test(); sys.exit(0)
    path = sys.argv[1] if len(sys.argv) > 1 else '/opt/procurator/state/openclaw.json'
    tz = sys.argv[2] if len(sys.argv) > 2 else 'UTC'
    c = json.load(open(path))
    c, changes = retune_compaction(c)
    c, idle = mcp_idle(c)
    changes += idle
    refused = None
    try:
        c, more = apply(c, tz)
        changes += more
    except ValueError as e:
        refused = str(e)      # the compaction retune above still applies
    real = [x for x in changes if not x.startswith('session.reset left')]
    if real:
        backup = path + '.before-cost-' + time.strftime('%Y%m%dT%H%M%S')
        shutil.copy2(path, backup)
        st = os.stat(path); tmp = path + '.tmp'
        json.dump(c, open(tmp, 'w'), indent=2); os.chown(tmp, st.st_uid, st.st_gid); os.chmod(tmp, st.st_mode & 0o777); os.replace(tmp, path)
        print('Cost settings (backup %s):' % backup)
    else:
        print('Cost settings: already applied.')
    for x in changes:
        print('  ' + x)
    if refused:
        print('Other cost settings not applied: ' + refused)
        sys.exit(1)
