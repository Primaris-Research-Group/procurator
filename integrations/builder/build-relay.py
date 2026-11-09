#!/usr/bin/env python3
"""PROCURATOR build relay (droplet side). Nov 5 2026.

This script is the ONLY thing a builder's SSH key can run on the droplet: authorized_keys pins each key with
restrict,command="/usr/bin/python3 /opt/procurator/bin/build-relay.py --builder NAME" (mac, windows…).
The name comes from the key, never from the builder's own message. Builders pull work; the droplet never runs
anything on them, and one builder can never claim or report on another builder's build.

Commands (from SSH_ORIGINAL_COMMAND; JSON on stdin, one JSON line on stdout):
  hello          Builder heartbeat: allowed repos (names only), which agents are installed, busy job.
                 Reply: ids of this builder's claimed jobs the owner cancelled.
  pull           Claim the oldest approved job meant for this builder (plan.builder = NAME or "any") whose repo
                 is on this builder's allowed list (queue/<id>.json written by the gateway plugin after /buildapprove).
  report <id>    Progress or result for a claimed job (status, PR link, test result, summary). Reply: cancelled?
                 Live builds (1.4.0, Nov 8): the report may carry `progress` (what the agent is doing), `question`
                 (status waiting_owner) and `model`; the reply also carries `notes`, the owner's messages for the
                 agent queued by the plugin (/buildnote), each delivered exactly once.

Files (state/procurator-build, shared with the gateway container as /home/node/.openclaw/procurator-build):
  queue/<id>.json, cancel/<id>     written by the gateway plugin only
  claims/<id>.json, reports/<id>.json, builders/<name>.json   written by this relay only
  notes/<id>/*.json                written by the plugin, consumed (deleted) by this relay when delivered
  progress/<id>.jsonl              appended by this relay from reports; the plugin and dashboard read the tail
Every value from the Mac is checked and size-limited here; the plugin checks again before showing it.

Browser mode (web 0.19.0, home browser lane): a SECOND restricted key per machine runs
  restrict,command="/usr/bin/python3 /opt/procurator/bin/build-relay.py --browser NAME"
and can only use: bhello (heartbeat: profiles, open browsers, vitals), bpull (long-poll for this machine's next browser job,
up to 25 s) and bresult <id> (the driver's output for a job this machine claimed, plus any trace/archive file the job asked
for). Jobs are written by the browser broker under /var/lib/procurator-web/lanes/NAME/jobs; a machine never sees another
machine's jobs, and build keys cannot use browser commands (or the reverse).
"""
import json, os, re, sys, time

STATE = os.environ.get('PROCURATOR_BUILD_DIR', '/opt/procurator/state/procurator-build')
ID = re.compile(r'^[a-f0-9]{16}$')
NAME = re.compile(r'^[a-z0-9-]{1,20}$')
REPO = re.compile(r'^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$')
PR = re.compile(r'^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/pull/\d{1,7}$')
BRANCH = re.compile(r'^procurator/[a-f0-9]{16}$')
STATUSES = ('claimed', 'preparing', 'running', 'waiting_owner', 'testing', 'pushing', 'done', 'failed', 'cancelled')
PROGRESS_KINDS = ('say', 'tool', 'result', 'question', 'note', 'info')
PROGRESS_MAX_LINES = 400
MODEL = re.compile(r'^[A-Za-z0-9._:/-]{2,64}$')
FINAL = ('done', 'failed', 'cancelled')
MAX_IN = 65536
EXPIRE_MS = 24 * 3600 * 1000


LANES = os.environ.get('PROCURATOR_LANES_DIR', '/var/lib/procurator-web/lanes')
INBOX = os.environ.get('PROCURATOR_INBOX_DIR', '/run/procurator-web/inbox')
BJOB = re.compile(r'^[a-f0-9]{24}$')
SID = re.compile(r'^[a-f0-9]{16}$')
PROFILE = re.compile(r'^[a-z0-9][a-z0-9.-]{0,60}$')
INBOX_FILE = re.compile(r'^(?:trace-[a-f0-9]{1,16}-[a-f0-9]{8}\.zip|capture-[a-f0-9]{16}\.mhtml)$')
B_MAX_IN = 40 * 1024 * 1024


class RelayError(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def ensure_dirs():
    if not os.path.isdir(STATE):
        parent = os.path.dirname(STATE.rstrip('/'))
        st = os.stat(parent)
        os.makedirs(STATE, mode=0o700)
        os.chown(STATE, st.st_uid, st.st_gid)
    st = os.stat(STATE)
    for sub in ('queue', 'cancel', 'claims', 'reports', 'builders', 'notes', 'progress'):
        p = os.path.join(STATE, sub)
        if not os.path.isdir(p):
            os.makedirs(p, mode=0o700)
            os.chown(p, st.st_uid, st.st_gid)
    return st.st_uid, st.st_gid


def write_json(path, value, exclusive=False):
    uid, gid = ensure_dirs()
    data = json.dumps(value, separators=(',', ':')).encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, data); os.fsync(fd)
        finally:
            os.close(fd)
        os.chown(path, uid, gid)
        return
    tmp = '%s.%d.tmp' % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data); os.fsync(fd)
    finally:
        os.close(fd)
    os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def text(v, limit, field):
    if v is None:
        return None
    if not isinstance(v, str):
        raise RelayError('%s must be text' % field)
    v = ''.join(ch for ch in v if ch == '\n' or ch == '\t' or ord(ch) >= 32).strip()
    return v[:limit]


def integer(v, field, lo=0, hi=10**9):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        raise RelayError('%s out of range' % field)
    return int(v)


def vitals_of(v):
    """Machine vitals from worker 1.3.0 (Nov 6): numbers only, size-checked; anything odd is dropped."""
    if not isinstance(v, dict):
        return {}
    out = {}
    for k, hi in (('load1', 10000), ('cpus', 1024), ('mem_used', 10**13), ('mem_total', 10**13), ('disk_free', 10**15), ('disk_total', 10**15), ('uptime', 10**9)):
        x = v.get(k)
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= hi:
            continue
        out[k] = round(float(x), 2) if k == 'load1' else int(x)
    if isinstance(v.get('os'), str):
        out['os'] = text(v['os'], 20, 'os')
    return out


def hello(name, data):
    repos = data.get('repos') or []
    if not isinstance(repos, list) or len(repos) > 50 or not all(isinstance(r, str) and REPO.match(r) for r in repos):
        raise RelayError('repos must be a list of owner/name')
    agents = data.get('agents') or {}
    if not isinstance(agents, dict):
        raise RelayError('agents must be an object')
    busy = data.get('busy')
    if busy is not None and not (isinstance(busy, str) and ID.match(busy)):
        raise RelayError('busy must be a build id')
    defaults = data.get('defaults') or {}
    info = {
        'name': name,
        'label': text(data.get('label'), 30, 'label') or name,
        'seen_at': now_ms(),
        'version': text(data.get('version'), 40, 'version'),
        'host': text(data.get('host'), 60, 'host'),
        'repos': sorted(set(repos)),
        'agents': {k: bool(agents.get(k)) for k in ('claude', 'codex')},
        'default_agent': defaults.get('agent') if defaults.get('agent') in ('claude', 'codex') else None,
        'branches': {r: b for r, b in (defaults.get('branches') or {}).items()
                     if isinstance(r, str) and REPO.match(r) and isinstance(b, str) and re.match(r'^[A-Za-z0-9._/-]{1,100}$', b)} if isinstance(defaults.get('branches'), dict) else {},
        'busy': busy,
        'vitals': vitals_of(data.get('vitals')),
    }
    write_json(os.path.join(STATE, 'builders', name + '.json'), info)
    cancel = sorted(i for i in os.listdir(os.path.join(STATE, 'cancel')) if ID.match(i)
                    and claimed_by(i) == name)
    return {'ok': True, 'cancel': cancel, 'time': info['seen_at']}


def claimed_by(build_id):
    claim = read_json(os.path.join(STATE, 'claims', build_id + '.json'))
    return claim.get('builder', 'mac') if isinstance(claim, dict) else None


def pull(name, _data):
    me = read_json(os.path.join(STATE, 'builders', name + '.json')) or {}
    repos = set(me.get('repos') or [])
    qdir = os.path.join(STATE, 'queue')
    jobs = []
    for fname in os.listdir(qdir):
        i = fname[:-5]
        if not fname.endswith('.json') or not ID.match(i):
            continue
        if os.path.exists(os.path.join(STATE, 'claims', i + '.json')) or os.path.exists(os.path.join(STATE, 'cancel', i)):
            continue
        job = read_json(os.path.join(qdir, fname))
        if not isinstance(job, dict) or job.get('id') != i:
            continue
        plan = job.get('plan') or {}
        if (plan.get('builder') or 'any') not in ('any', name) or plan.get('repo') not in repos:
            continue
        jobs.append(job)
    for job in sorted(jobs, key=lambda j: j.get('approved_at') or 0):
        i = job['id']
        claim = os.path.join(STATE, 'claims', i + '.json')
        try:
            write_json(claim, {'id': i, 'claimed_at': now_ms(), 'builder': name}, exclusive=True)
        except FileExistsError:
            continue
        if now_ms() - (job.get('approved_at') or 0) > EXPIRE_MS:
            write_json(os.path.join(STATE, 'reports', i + '.json'), {'id': i, 'status': 'failed', 'updated_at': now_ms(), 'builder': name,
                       'message': 'Expired: no builder picked this build up within 24 hours. Prepare it again.', 'events': []})
            continue
        return {'ok': True, 'job': job}
    return {'ok': True, 'job': None}


def report(name, build_id, data):
    if not ID.match(build_id or ''):
        raise RelayError('bad build id')
    owner = claimed_by(build_id)
    if owner is None:
        raise RelayError('build not claimed')
    if owner != name:
        raise RelayError('build belongs to another builder')
    path = os.path.join(STATE, 'reports', build_id + '.json')
    current = read_json(path, {}) or {}
    if current.get('status') in FINAL:
        raise RelayError('build already finished')
    status = data.get('status')
    if status not in STATUSES:
        raise RelayError('unknown status')
    out = dict(current)
    out.update({'id': build_id, 'status': status, 'updated_at': now_ms(), 'builder': name})
    for key, limit in (('message', 600), ('summary', 3000), ('agent', 20), ('base', 100), ('question', 1500)):
        v = text(data.get(key), limit, key)
        if v is not None:
            out[key] = v
    if status != 'waiting_owner' and 'question' in out and data.get('question') is None:
        out.pop('question', None)                      # answered or moved on: the question no longer stands
    if data.get('model') is not None:
        if not isinstance(data['model'], str) or not MODEL.match(data['model']):
            raise RelayError('model must be a short model name')
        out['model'] = data['model']
    turn = integer(data.get('turn'), 'turn', 0, 1000)
    if turn is not None:
        out['turn'] = turn
    progress = data.get('progress')
    if progress is not None:
        if not isinstance(progress, list) or len(progress) > 60:
            raise RelayError('progress must be a list of up to 60 entries')
        lines = []
        for e in progress:
            if not isinstance(e, dict) or e.get('t') not in PROGRESS_KINDS:
                raise RelayError('progress entry invalid')
            lines.append({'t': e['t'], 'text': text(e.get('text'), 400, 'progress.text') or '', 'name': text(e.get('name'), 40, 'progress.name'),
                          'at': integer(e.get('at'), 'progress.at', 0, 10**13) or now_ms()})
        if lines:
            append_progress(build_id, lines)
            out['progress_count'] = integer(current.get('progress_count', 0), 'progress_count', 0, 10**7) + len(lines)
            out['last_progress'] = lines[-1]
    if data.get('pr_url') is not None:
        if not isinstance(data['pr_url'], str) or not PR.match(data['pr_url']):
            raise RelayError('pr_url must be a github.com pull request link')
        out['pr_url'] = data['pr_url']
    if data.get('branch') is not None:
        if not isinstance(data['branch'], str) or not BRANCH.match(data['branch']):
            raise RelayError('branch must be procurator/<id>')
        out['branch'] = data['branch']
    tests = data.get('tests')
    if tests is not None:
        if not isinstance(tests, dict) or tests.get('status') not in ('passed', 'failed', 'not_run', 'timed_out'):
            raise RelayError('tests.status invalid')
        out['tests'] = {'status': tests['status'], 'command': text(tests.get('command'), 200, 'tests.command'),
                        'output_tail': text(tests.get('output_tail'), 2000, 'tests.output_tail')}
    files = data.get('files')
    if files is not None:
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise RelayError('files must be a list')
        out['files'] = [text(f, 200, 'file') for f in files[:60]]
        out['file_count'] = integer(data.get('file_count', len(files)), 'file_count', 0, 100000)
    for key in ('insertions', 'deletions', 'duration_s'):
        v = integer(data.get(key), key)
        if v is not None:
            out[key] = v
    if data.get('cost_usd') is not None:
        c = data['cost_usd']
        if isinstance(c, bool) or not isinstance(c, (int, float)) or not 0 <= c < 10000:
            raise RelayError('cost_usd out of range')
        out['cost_usd'] = round(float(c), 4)
    events = list(current.get('events') or [])[-19:]
    if not events or events[-1].get('status') != status:          # a timeline of status changes, not of progress reports
        events.append({'status': status, 'at': out['updated_at']})
    out['events'] = events
    write_json(path, out)
    return {'ok': True, 'cancel': os.path.exists(os.path.join(STATE, 'cancel', build_id)), 'notes': take_notes(build_id)}


def append_progress(build_id, lines):
    """progress/<id>.jsonl: one JSON object per line, the newest last, capped at PROGRESS_MAX_LINES (oldest dropped)."""
    uid, gid = ensure_dirs()
    path = os.path.join(STATE, 'progress', build_id + '.jsonl')
    old = []
    try:
        with open(path) as f:
            old = [l for l in f.read().splitlines() if l.strip()]
    except OSError:
        pass
    keep = (old + [json.dumps(l, separators=(',', ':')) for l in lines])[-PROGRESS_MAX_LINES:]
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('\n'.join(keep) + '\n')
    os.chmod(tmp, 0o600)
    os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def take_notes(build_id):
    """The owner's queued messages for this build (plugin: notes/<id>/<ms>-<rand>.json), oldest first, each returned once."""
    d = os.path.join(STATE, 'notes', build_id)
    if not os.path.isdir(d):
        return []
    out = []
    for name in sorted(os.listdir(d))[:20]:
        if not re.match(r'^[0-9]{10,16}-[a-f0-9]{4,16}\.json$', name):
            continue
        path = os.path.join(d, name)
        try:
            n = read_json(path, {}) or {}
            t = text(n.get('text'), 2000, 'note')
            if t:
                out.append({'id': name[:-5], 'text': t, 'at': integer(n.get('at'), 'note.at', 0, 10**13) or 0})
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    return out


def lane_dir(name):
    base = os.path.join(LANES, name)
    for sub in ('', 'jobs', 'claims', 'results'):
        os.makedirs(os.path.join(base, sub), mode=0o700, exist_ok=True)
    return base


def lane_write(path, value, exclusive=False):
    data = json.dumps(value, separators=(',', ':')).encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        return
    tmp = path + '.tmp%d' % os.getpid()
    with open(tmp, 'wb') as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def bhello(name, data):
    base = lane_dir(name)
    profiles = data.get('profiles') or []
    if not isinstance(profiles, list) or len(profiles) > 100:
        raise RelayError('profiles must be a list')
    clean = []
    for p in profiles:
        if not isinstance(p, dict) or not isinstance(p.get('name'), str) or not PROFILE.match(p['name']):
            raise RelayError('bad profile')
        clean.append({'name': p['name'], 'ready': bool(p.get('ready'))})
    sessions = data.get('sessions') or []
    if not isinstance(sessions, list) or len(sessions) > 10 or not all(isinstance(x, str) and SID.match(x) for x in sessions):
        raise RelayError('sessions must be browser ids')
    info = {'name': name, 'label': text(data.get('label'), 30, 'label') or name, 'seen_at': now_ms(),
            'version': text(data.get('version'), 40, 'version'), 'chrome': text(data.get('chrome'), 80, 'chrome'),
            'headed': bool(data.get('headed')), 'profiles': clean, 'sessions': sessions, 'vitals': vitals_of(data.get('vitals'))}
    lane_write(os.path.join(base, 'hello.json'), info)
    return {'ok': True, 'time': info['seen_at']}


def bpull(name, data, sleep=time.sleep):
    base = lane_dir(name)
    wait = data.get('wait', 20)
    wait = wait if isinstance(wait, int) and 0 <= wait <= 25 else 20
    lane_write(os.path.join(base, 'alive.json'), {'at': now_ms()})
    deadline = time.time() + wait
    while True:
        jobs = []
        for fname in os.listdir(os.path.join(base, 'jobs')):
            i = fname[:-5]
            if fname.endswith('.json') and BJOB.match(i) and not os.path.exists(os.path.join(base, 'claims', i + '.json')):
                jobs.append((os.path.getmtime(os.path.join(base, 'jobs', fname)), i))
        for _, i in sorted(jobs):
            try:
                lane_write(os.path.join(base, 'claims', i + '.json'), {'by': name, 'at': now_ms()}, exclusive=True)
            except FileExistsError:
                continue                       # the broker withdrew it (timed out) or it was already taken
            job = read_json(os.path.join(base, 'jobs', i + '.json'))
            if isinstance(job, dict) and job.get('id') == i:
                return {'ok': True, 'job': job}
        if time.time() >= deadline:
            return {'ok': True, 'job': None}
        sleep(0.25)


def bresult(name, job_id, data):
    if not BJOB.match(job_id or ''):
        raise RelayError('bad job id')
    base = lane_dir(name)
    claim = read_json(os.path.join(base, 'claims', job_id + '.json'))
    if not isinstance(claim, dict) or claim.get('by') != name:
        raise RelayError('job not claimed by this machine')
    out_path = os.path.join(base, 'results', job_id + '.json')
    if os.path.exists(out_path):
        raise RelayError('result already reported')
    stdout = data.get('stdout')
    if not isinstance(stdout, str) or len(stdout) > 30 * 1024 * 1024:
        raise RelayError('stdout must be text')
    files = data.get('files') or {}
    if not isinstance(files, dict) or len(files) > 3:
        raise RelayError('files must be an object')
    import base64
    for path, b64 in files.items():
        if not isinstance(path, str) or os.path.dirname(path) != INBOX or not INBOX_FILE.match(os.path.basename(path)) or not isinstance(b64, str):
            raise RelayError('file path not allowed')
        blob = base64.b64decode(b64.encode(), validate=True)
        if len(blob) > 60 * 1024 * 1024:
            raise RelayError('file too large')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with os.fdopen(fd, 'wb') as f:
            f.write(blob)
        try:
            os.chown(path, 0, 1000)
        except OSError:
            pass
    lane_write(out_path, {'id': job_id, 'stdout': stdout, 'at': now_ms(), 'by': name})
    return {'ok': True}


def browser_main(argv_cmd, stdin, name):
    if not NAME.match(name or ''):
        raise RelayError('bad machine name')
    parts = (argv_cmd or '').split()
    if not parts:
        raise RelayError('no command')
    raw = stdin.read(B_MAX_IN + 1) if parts[0] in ('bhello', 'bpull', 'bresult') else ''
    if len(raw) > B_MAX_IN:
        raise RelayError('input too large')
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise RelayError('input must be a JSON object')
    if parts[0] == 'bhello' and len(parts) == 1:
        return bhello(name, data)
    if parts[0] == 'bpull' and len(parts) == 1:
        return bpull(name, data)
    if parts[0] == 'bresult' and len(parts) == 2:
        return bresult(name, parts[1], data)
    raise RelayError('unknown command')


def main(argv_cmd, stdin, name='mac'):
    if not NAME.match(name or ''):
        raise RelayError('bad builder name')
    parts = (argv_cmd or '').split()
    if not parts:
        raise RelayError('no command')
    raw = stdin.read(MAX_IN + 1) if parts[0] in ('hello', 'report') else ''
    if len(raw) > MAX_IN:
        raise RelayError('input too large')
    data = json.loads(raw) if raw.strip() else {}
    if not isinstance(data, dict):
        raise RelayError('input must be a JSON object')
    ensure_dirs()
    if parts[0] == 'hello' and len(parts) == 1:
        return hello(name, data)
    if parts[0] == 'pull' and len(parts) == 1:
        return pull(name, data)
    if parts[0] == 'report' and len(parts) == 2:
        return report(name, parts[1], data)
    raise RelayError('unknown command')


if __name__ == '__main__':
    try:
        args = sys.argv[1:]
        name = 'mac'
        if args[:1] == ['--browser']:
            if len(args) < 2:
                raise RelayError('--browser needs a name')
            command = os.environ['SSH_ORIGINAL_COMMAND'] if 'SSH_ORIGINAL_COMMAND' in os.environ else ' '.join(args[2:])
            print(json.dumps(browser_main(command, sys.stdin, args[1]), separators=(',', ':')))
            sys.exit(0)
        if args[:1] == ['--builder']:
            if len(args) < 2:
                raise RelayError('--builder needs a name')
            name, args = args[1], args[2:]
        command = os.environ['SSH_ORIGINAL_COMMAND'] if 'SSH_ORIGINAL_COMMAND' in os.environ else ' '.join(args)
        result = main(command, sys.stdin, name)
        print(json.dumps(result, separators=(',', ':')))
    except (RelayError, ValueError) as e:
        print(json.dumps({'ok': False, 'error': str(e)[:200]}))
        sys.exit(1)
