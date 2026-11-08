"""Regression set and pinned judge (web 0.17.0, PROCURATOR browsing plan Phase 2, §5.3).

  python3 regression.py offline            every errand's page fixtures (plus the seeds) re-checked offline; no network
  python3 regression.py offline --recorded the same against saved snapshots (no Chrome needed; this is what the server runs)
  python3 regression.py live               the live checklist: errands to run from Telegram with gates on (nothing automated)
  python3 regression.py judge ERRAND SNAPSHOT.json [ANSWER.txt]   judge one live run's final snapshot (and the answer the
                                           assistant gave, when supplied) against the errand's key nodes
  --set compat (with live or judge)         the web compatibility battery (compat/*.json, 0.19.1) instead of the errands

The judge is one fixed model call (regression/config.json pins the model) over the final page text and the key nodes,
answering pass/fail per node; without an Anthropic key it falls back to phrase evidence and says so. The model used is
printed with every result.
"""
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SETS = {'regression': HERE / 'regression', 'compat': HERE / 'compat'}
SET = SETS['regression']

def errands(name='regression'):
    return [json.loads(p.read_text()) for p in sorted(SETS[name].glob('*.json')) if p.name != 'config.json']

def config(name='regression'):
    return json.loads((SETS[name] / 'config.json').read_text())

def offline(recorded=False):
    import fixtures
    dirs = []
    for e in errands():
        for name in e.get('fixtures', []):
            d = fixtures.SEEDS / name
            if d not in dirs:
                dirs.append(d)
    for d in fixtures.fixture_dirs([fixtures.SEEDS]):
        if d not in dirs:
            dirs.append(d)
    started = time.time()
    results = fixtures.run(dirs, recorded)
    by = {Path(r['fixture']).name: r for r in results}
    print('Offline regression (%s):' % ('recorded snapshots' if recorded else 'local headless Chrome'))
    for e in errands():
        fx = e.get('fixtures', [])
        if not fx:
            print('  -    %s: live only (no page fixture yet)' % e['name'])
            continue
        ok = all(by.get(n, {}).get('ok') for n in fx)
        print('  %s %s: %s' % ('PASS' if ok else 'FAIL', e['name'], ', '.join(fx)))
        for n in fx:
            for p in by.get(n, {}).get('problems', []):
                print('       ' + n + ': ' + p)
    extra = [r for n, r in by.items() if not any(n in e.get('fixtures', []) for e in errands())]
    for r in extra:
        print('  %s fixture %s' % ('PASS' if r['ok'] else 'FAIL', r['name']))
    print('%d/%d fixtures passed in %.1f s' % (sum(r['ok'] for r in results), len(results), time.time() - started))
    return 0 if all(r['ok'] for r in results) else 1

def live(name='regression'):
    if name == 'compat':
        cfg = config('compat')
        print('Web compatibility battery (read-only; one /webtask approval per case; location %s).' % cfg.get('location', ''))
        print('Paste each prompt into Telegram. Afterwards save the final page (/webshot), put the assistant\'s answer in a text file')
        print('and run: python3 regression.py judge --set compat NAME snapshot.json answer.txt')
        print('Run each case on both browsers: /weblane SITE cloud, then /weblane SITE home, then /weblane SITE default.')
        for e in errands('compat'):
            print('\n%s %s (%s, default lane %s) budget %d steps / %d min\n  prompt: %s\n  hard part: %s\n  pass: %s%s' % (
                e['id'], e['name'], e['site'], e['lane_default'], e['budget']['steps'], e['budget']['minutes'], e['intent'], e['hard_part'],
                e['answer_check']['description'], ' (a clean bot-check stop counts as Blocked, not Fail)' if e.get('blocked_is_acceptable') else ''))
        print('\nJudge model (pinned): ' + cfg['judge_model'])
        return 0
    print('Live regression checklist (weekly, from Telegram, with the owner present). Gates stay on: every run stops before')
    print('any final submit, payment, sign-in or consent. After each run, save the final page with /webshot and, if you want a')
    print('verdict, export its snapshot from the dashboard trace and run: python3 regression.py judge NAME snapshot.json')
    for e in errands():
        print('\n- %s (%s)\n  %s\n  start: %s' % (e['name'], e['site'], e['intent'], e['start_url']))
        for n in e['key_nodes']:
            print('  key node %s: %s' % (n['id'], n['description']))
        print('  final state: ' + e['final_state']['description'])
    print('\nJudge model (pinned): ' + config()['judge_model'])
    return 0

def answer_ok(errand, answer):
    """The compat battery's answer check: every pattern must appear in the assistant's reported answer."""
    pats = (errand.get('answer_check') or {}).get('patterns') or []
    return all(re.search(p, answer or '') for p in pats)

def phrase_judge(errand, text, answer=None):
    low = (text or '').lower()
    hit = lambda node: any(p.lower() in low for p in node.get('evidence_any', []))
    nodes = [{'id': n['id'], 'pass': hit(n)} for n in errand['key_nodes']]
    out = {'nodes': nodes, 'final': hit(errand['final_state']), 'judge': 'phrase evidence (no model key)'}
    if answer is not None:
        out['answer_supported'] = answer_ok(errand, answer)
    return out

def model_judge(errand, text, key, model, max_tokens=400, answer=None):
    prompt = ('You judge one browser-assistant run. Given the page text the run read (the FINAL page first, then earlier pages, '
              'page-vision readings and the values it typed) and the errand\'s key nodes, answer whether the run reached each key node '
              '(evidence on any of those pages or implied by them) and whether the final state holds. ' +
              ('Also judge the assistant\'s reported ANSWER: answer_supported is true only if it answers the errand and nothing in it '
               'contradicts the final page. ' if answer is not None else '') +
              'Reply with JSON only: {"nodes":[{"id":"...","pass":true|false}],"final":true|false,' + ('"answer_supported":true|false,' if answer is not None else '') +
              '"why":"one sentence"}.\n\n'
              'Errand: ' + errand['intent'] + '\nKey nodes:\n' + '\n'.join('- %s: %s' % (n['id'], n['description']) for n in errand['key_nodes']) +
              '\nFinal state: ' + errand['final_state']['description'] +
              ('\nThe answer should contain: ' + errand['answer_check']['description'] if answer is not None and errand.get('answer_check') else '') +
              '\n\n<pages>\n' + (text or '')[:24000].replace('</pages>', '') + '\n</pages>' +
              ('\n\n<answer>\n' + answer[:3000].replace('</answer>', '') + '\n</answer>' if answer is not None else ''))
    req = urllib.request.Request('https://api.anthropic.com/v1/messages', data=json.dumps({'model': model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]}).encode(),
                                 headers={'x-api-key': key, 'anthropic-version': '2023-06-01', 'content-type': 'application/json'})
    with urllib.request.urlopen(req, timeout=75) as r:   # 0.21.1: the judge now reads up to 24k chars of evidence
        out = json.load(r)
    raw = ''.join(b.get('text', '') for b in out.get('content', []) if b.get('type') == 'text')
    from bench import parse_json_object   # 0.20.7: fenced or chatty replies no longer fall back to phrase matching
    data = parse_json_object(raw)
    if not isinstance(data, dict) or 'nodes' not in data:
        raise ValueError('judge reply was not the expected JSON: ' + raw[:120])
    res = {'nodes': [{'id': str(n.get('id')), 'pass': bool(n.get('pass'))} for n in data.get('nodes', [])], 'final': bool(data.get('final')), 'why': str(data.get('why', ''))[:300], 'judge': model}
    if answer is not None:
        res['answer_supported'] = bool(data.get('answer_supported')) and answer_ok(errand, answer)
    return res

def judge(name, snapshot_path, key=None, setname='regression', answer_path=None, caller=None):
    errand = next((e for e in errands(setname) if e['name'] == name or e.get('id') == name), None)
    if not errand:
        raise SystemExit('No errand named ' + name)
    snap = json.loads(Path(snapshot_path).read_text())
    text = snap.get('text') if isinstance(snap, dict) else str(snap)
    answer = Path(answer_path).read_text() if answer_path else None
    key = key or os.environ.get('ANTHROPIC_API_KEY')
    cfg = config(setname)
    verdict = (caller or model_judge)(errand, text, key, cfg['judge_model'], cfg.get('judge_max_tokens', 400), answer=answer) if key else phrase_judge(errand, text, answer)
    verdict['pass'] = verdict['final'] and all(n['pass'] for n in verdict['nodes']) and verdict.get('answer_supported', True)
    print(json.dumps({'errand': errand['name'], **verdict}, indent=1))
    return 0 if verdict['pass'] else 1

def main(argv):
    argv = list(argv)
    setname = 'regression'
    if '--set' in argv:
        i = argv.index('--set')
        setname = argv[i + 1] if i + 1 < len(argv) else ''
        del argv[i:i + 2]
        if setname not in SETS:
            print('Unknown set %r (use regression or compat)' % setname)
            return 2
    if not argv or argv[0] not in ('offline', 'live', 'judge'):
        print(__doc__)
        return 2
    if argv[0] == 'offline':
        return offline('--recorded' in argv)
    if argv[0] == 'live':
        return live(setname)
    if len(argv) not in (3, 4):
        print('Usage: python3 regression.py judge [--set compat] ERRAND SNAPSHOT.json [ANSWER.txt]')
        return 2
    return judge(argv[1], argv[2], setname=setname, answer_path=argv[3] if len(argv) == 4 else None)

if __name__ == '__main__':
    sys.path.insert(0, str(HERE))
    sys.exit(main(sys.argv[1:]))
