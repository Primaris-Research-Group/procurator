"""Persistent owner-approved browser tasks and bounded continuation receipts."""
import json
import time
import uuid
import re
from task_policy import normalize_task,preview,routine_allowed,autonomous_decision,STOP_GUIDANCE

def site_of_spec(spec):
    import urllib.parse as _u
    if spec.get('site_domain'):return spec['site_domain']
    host=_u.urlsplit(spec.get('origin') or '').hostname or 'unknown'
    return host[4:] if host.startswith('www.') else host

class Tasks:
    def __init__(self,broker,validate_url,validate_text,hasher):
        self.b=broker;self.url=validate_url;self.text=validate_text;self.digest=hasher
        with broker.db() as db:
            if 'task_id' not in [r['name'] for r in db.execute('PRAGMA table_info(plans)')]:db.execute('ALTER TABLE plans ADD COLUMN task_id TEXT')
            cols=[r['name'] for r in db.execute('PRAGMA table_info(web_tasks)')]
            if cols and 'no_progress' not in cols:db.execute('ALTER TABLE web_tasks ADD COLUMN no_progress INTEGER DEFAULT 0')
            if cols and 'failures' not in cols:db.execute('ALTER TABLE web_tasks ADD COLUMN failures INTEGER DEFAULT 0')
            db.executescript('''CREATE TABLE IF NOT EXISTS web_tasks(
              id TEXT PRIMARY KEY,request_id TEXT UNIQUE,spec TEXT,hash TEXT,status TEXT,
              reviewed REAL,activated REAL,expires REAL,steps_used INTEGER,pending_action TEXT,
              context TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS web_task_actions(plan_id TEXT PRIMARY KEY,task_id TEXT);
            CREATE TABLE IF NOT EXISTS web_task_resumes(event_id TEXT PRIMARY KEY,task_id TEXT,status TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS web_task_requests(request_id TEXT PRIMARY KEY,task_id TEXT,fingerprint TEXT,plan_id TEXT);''')
            cols=[r['name'] for r in db.execute('PRAGMA table_info(web_tasks)')]
            if 'no_progress' not in cols:db.execute('ALTER TABLE web_tasks ADD COLUMN no_progress INTEGER DEFAULT 0')
            if 'failures' not in cols:db.execute('ALTER TABLE web_tasks ADD COLUMN failures INTEGER DEFAULT 0')
            if 'worker_key' not in cols:db.execute('ALTER TABLE web_tasks ADD COLUMN worker_key TEXT')
    def row(self,identifier):
        with self.b.db() as db:r=db.execute('SELECT * FROM web_tasks WHERE id=?',(identifier,)).fetchone()
        if not r:raise ValueError('Unknown browser task.')
        r=dict(r)
        session=self.b.session(json.loads(r['spec'])['session_id'])
        expires=r['expires'] or session['created']+session.get('timeout_seconds',900)
        if r['status'] in ('draft','reviewed','active','awaiting_approval','paused','needs_attention') and (time.time()>expires or session['status'] in ('closed','failed')):
            with self.b.db() as db:db.execute("UPDATE web_tasks SET status='expired' WHERE id=?",(r['id'],))
            r['status']='expired'
        return r
    def public(self,r):
        with self.b.db() as db:events=[dict(x) for x in db.execute('SELECT event_id,status FROM web_task_resumes WHERE task_id=? ORDER BY updated DESC LIMIT 3',(r['id'],))]
        with self.b.db() as db:checks=[dict(x) for x in db.execute("SELECT id,status,session_id FROM plans WHERE task_id=? AND status IN ('uncertain','verification_pending')",(r['id'],))]
        return {'id':r['id'],'status':r['status'],'spec':json.loads(r['spec']),'steps_used':r['steps_used'],
                'expires':r['expires'],'pending_action':r['pending_action'],'result':json.loads(r['result']) if r['result'] else None,'continuations':events,
                'pending_checks':checks,'submitted':self.b.journal.submits(r['id']) if hasattr(self.b,'journal') else [],'next_check':'Read a fresh web_browser_snapshot; do not repeat the interaction.' if any(x['status']=='verification_pending' for x in checks) else None}
    def active(self,identifier):
        r=self.row(identifier)
        if r['status']!='active':raise ValueError('Task is '+r['status']+'. Check its pending question, approval, or expiry.')
        return r
    def context(self,value):
        if not isinstance(value,dict) or set(value)-{'agentId','sessionKey','sessionId','accountId'}:raise ValueError('Missing native task conversation context.')
        if value.get('agentId')!='main' or not isinstance(value.get('sessionKey'),str) or not value['sessionKey'].startswith('agent:main:') or not isinstance(value.get('sessionId'),str) or not value['sessionId']:
            raise ValueError('The native owner conversation could not be bound to this task.')
        return {k:self.text(v,500) for k,v in value.items() if isinstance(v,str)}
    def start(self,r,spec,context,event='activated'):
        """Activate an owner-approved task scope on its browser (from /webtask, or the owner's standing /siteauto grant)."""
        b=self.b
        s=b.session(spec['session_id']);expires=s['created']+s.get('timeout_seconds',900)
        if s['status']!='open' or expires-time.time()<60:raise ValueError('The browser session is ending; open a new session and task grant.')
        b.event(r['id'],event)
        with b.db() as db:
            db.execute("UPDATE web_tasks SET status='active',activated=?,expires=?,context=? WHERE id=?",(time.time(),expires,json.dumps(context),r['id']))
            # An owner-approved revision (e.g. changed date/time) replaces older tasks on this browser.
            for old in db.execute("SELECT id,spec FROM web_tasks WHERE id<>? AND status IN ('draft','reviewed','active','awaiting_approval','paused','needs_attention')",(r['id'],)).fetchall():
                if json.loads(old['spec'])['session_id']==spec['session_id']:
                    db.execute("UPDATE web_tasks SET status='cancelled',result=? WHERE id=?",(json.dumps({'superseded_by':r['id']}),old['id']))
                    db.execute("UPDATE plans SET status='superseded' WHERE task_id=? AND status IN ('prepared','reviewed')",(old['id'],))

    def same_context(self,r,value):
        current=self.context(value);saved=json.loads(r['context']) if r['context'] else None
        # Same conversation: agent, session key and session id. accountId is compared only when both sides carry it
        # (a /siteauto task is bound from the assistant's own tool call, which may not name the Telegram account).
        if saved is None or any(current.get(k)!=saved.get(k) for k in ('agentId','sessionKey','sessionId')) or ('accountId' in current and 'accountId' in saved and current['accountId']!=saved['accountId']):
            raise ValueError('The task belongs to an earlier conversation. Create a fresh task review after a conversation reset.')
        return current
    def model_context(self,r,value):
        if not r['context']:raise ValueError('Task has not been activated by its owner.')
        current=self.context(value);saved=json.loads(r['context'])
        # Two-speed loop (0.18.0): the task's own worker session (started by the gateway plugin, not choosable by a model) may step it.
        if r.get('worker_key') and current.get('agentId')=='main' and current.get('sessionKey')==r['worker_key']:return
        if any(current.get(k)!=saved.get(k) for k in ('agentId','sessionKey','sessionId')):raise ValueError('This task is bound to another conversation. Request a fresh owner task review.')
    def before_final(self,task_id,session_id,context,plan_id):
        r=self.row(task_id)
        if r['status'] not in ('active','awaiting_approval'):raise ValueError('Task is '+r['status']+'. This approval cannot execute.')
        if json.loads(r['spec'])['session_id']!=session_id:raise ValueError('Task browser changed.')
        if r['pending_action']!=plan_id:raise ValueError('This action is no longer the task pending approval.')
        self.same_context(r,context)
        spec=json.loads(r['spec']);plan=json.loads(self.b.plan(plan_id)['plan'])
        limit=spec.get('spend_limit_usd');total=plan.get('detected_total')
        if spec.get('mode')=='autonomous' and limit is not None and total is not None and total>limit:
            raise ValueError('The order total $%.2f is above your $%.2f limit. Nothing was submitted; ask the bot to adjust the order or approve a new task with a higher limit.'%(total,limit))
    def on_review(self,task_id,plan_id):
        self.active(task_id)
        with self.b.db() as db:db.execute("UPDATE web_tasks SET status='awaiting_approval',pending_action=? WHERE id=?",(plan_id,task_id))
    def after_action(self,task_id,status):
        if not task_id:return
        r=self.row(task_id)
        if r['status'] in ('cancelled','complete','expired','paused'):return
        state='active' if status in ('completed','stale','not_dispatched','fields_incomplete','refused_duplicate') else 'needs_attention'
        with self.b.db() as db:db.execute('UPDATE web_tasks SET status=?,pending_action=NULL WHERE id=?',(state,task_id))
        if state=='needs_attention':self.forget_stuck_step(task_id,json.loads(r['spec']),'needs attention after '+str(status))
        if state=='needs_attention' and hasattr(self.b,'fixtures'):
            try:self.b.fixtures.capture(self.b.session(json.loads(r['spec'])['session_id']),'needs_attention',task_id)
            except Exception:pass
    NO_PROGRESS_LIMIT=3
    FAILURE_LIMIT=5
    def budget(self,task_id,spec,result,plan):
        """Validator signals and stop rules: repeated no-op steps or failed clicks pause the task."""
        b=self.b;status=result.get('status');out=result.get('result') or {}
        progress=out.get('progress');fills=out.get('fills') or []
        b.event(task_id,'step',{'plan':result.get('id'),'status':status,'changed':progress.get('changed') if progress else None,'reason':out.get('reason'),'lane':self.lane_of(spec)})
        with b.db() as db:t=db.execute('SELECT no_progress,failures,status FROM web_tasks WHERE id=?',(task_id,)).fetchone()
        streak,failures=t['no_progress'] or 0,t['failures'] or 0
        if status=='completed' and progress is not None:streak=0 if progress.get('changed') else streak+1
        if status in ('not_dispatched','stale','fields_incomplete'):failures+=1
        with b.db() as db:db.execute('UPDATE web_tasks SET no_progress=?,failures=? WHERE id=?',(streak,failures,task_id))
        extra={}
        bad=[f for f in fills if not f.get('ok')]
        if bad:extra['fill_warnings']='These fields did not keep the typed value (site mask or widget): '+', '.join(f['ref'] for f in bad)+'. Read the snapshot, then try the site\'s own picker/format or ask the owner.'
        if progress is not None:
            extra['validate']=('Nothing visible changed after this step. Re-read the snapshot; if the control is wrong, pick another; do not repeat the same action.' if not progress.get('changed')
                else 'State in one sentence what changed and whether the goal for this step succeeded before the next step.')
            if progress.get('field_errors'):extra['validate']+=' The site flags field errors: '+'; '.join(e['label']+': '+e['error'] for e in progress['field_errors'])
        if status=='fields_incomplete':
            extra['recover']=('Only typing and dropdown choices ran (%d of the steps finished) and nothing was clicked or submitted; the page probably '
                'reloaded itself after a choice. Take a fresh snapshot, check which fields already hold the owner\'s values, and redo only the '
                'missing or wrong ones with a new request ID. Do not ask the owner to fix it unless the same field fails again.')%int(out.get('steps_done') or 0)
        # Phase 3: the vision check helps before the no-change pause, and when the page has fields the driver cannot operate.
        why=('two_no_change' if streak==self.NO_PROGRESS_LIMIT-1 and progress is not None and not progress.get('changed') else
             'form_gaps' if (out.get('snapshot') or {}).get('form_gaps') else None)
        if why and status in ('completed','fields_incomplete'):
            q={'two_no_change':'The last two steps changed nothing. What is this page waiting for, and which control (or unlisted target) should be operated next?',
               'form_gaps':'Some labelled fields cannot be operated by ref ('+', '.join(str(g.get('label','')) for g in (out.get('snapshot') or {}).get('form_gaps',[])[:4])+'). What are these controls and how are they operated?'}[why]
            try:
                lk=b.look.look(b.session(spec['session_id']),q,task_id,auto=True)
                if lk.get('look_id'):
                    extra['look']={k:lk.get(k) for k in ('look_id','answer','page_state','refs_seen','unlisted_targets','model','looks_left')}
                    b.event(task_id,'look',{'auto':True,'why':why})
            except Exception:pass
        reason=None
        if streak>=self.NO_PROGRESS_LIMIT:reason='%d steps in a row changed nothing on the page'%streak
        elif failures>=self.FAILURE_LIMIT:reason='%d actions could not be performed'%failures
        if reason and t['status'] not in ('complete','cancelled','expired'):
            with b.db() as db:db.execute("UPDATE web_tasks SET status='needs_attention' WHERE id=? AND status='active'",(task_id,))
            b.event(task_id,'paused_by_budget',{'reason':reason})
            self.forget_stuck_step(task_id,spec,reason)
            try:b.send_with_buttons('BROWSER TASK PAUSED — '+task_id+'\n'+spec['title']+': '+reason+'. Nothing was submitted by this stop.\nI\'ll explain what I see; reply with guidance, or send /webcontinue '+task_id+' to let me try again, /webtakeover '+spec['session_id']+' to look yourself, or /webstop '+task_id+'.',
                self.buttons(task_id,spec,paused=True))
            except Exception:pass
            b.screenshot_to_owner(b.session(spec['session_id']),'Where task '+task_id+' got stuck.')
            try:b.fixtures.capture(b.session(spec['session_id']),'budget',task_id)
            except Exception:pass
            extra.update(stop_reason='budget',reason='Task paused: '+reason+'. Tell the owner what you see and what you tried; wait for guidance or /webcontinue.')
            with b.db() as db:db.execute('UPDATE web_tasks SET no_progress=0,failures=0 WHERE id=?',(task_id,))
        return {**result,**extra}
    def buttons(self,task_id,spec,approve=None,paused=False):
        """Nov 6: one-tap copy buttons under task messages (Telegram copy_text). They only copy the exact native command
        line to their clipboard; the owner still pastes and sends it themselves, so approval stays their own Telegram command."""
        rows=[]
        if approve:rows.append([{'text':'✅ Copy approval line','copy_text':{'text':approve}}])
        if paused:rows.append([{'text':'▶ Copy continue','copy_text':{'text':'/webcontinue '+task_id}},{'text':'✋ Copy take-over','copy_text':{'text':'/webtakeover '+spec['session_id']}}])
        else:rows.append([{'text':'⏸ Copy pause','copy_text':{'text':'/webpause '+task_id}}])
        rows[-1].append({'text':'⏹ Copy stop','copy_text':{'text':'/webstop '+task_id}})
        try:rows.append([{'text':'👁 Watch live (15 min)','url':self.b.watch_link(spec['session_id'])}])
        except Exception:pass
        return rows
    def forget_stuck_step(self,task_id,spec,reason):
        """Trajectory hints that relied on the control this run got stuck at are retired (Phase 4)."""
        try:
            with self.b.db() as db:last=db.execute("SELECT label FROM task_journal WHERE task_id=? AND kind IN ('click','submit','select','fill','upload') ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
            if last:self.b.memory.invalidate(site_of_spec(spec),last['label'],'stuck in task %s: %s'%(task_id,reason))
        except Exception:pass
    def metrics(self,task_id):
        with self.b.db() as db:
            r=db.execute('SELECT id,spec,status,steps_used,activated FROM web_tasks WHERE id=?',(task_id,)).fetchone()
            ev=[dict(x) for x in db.execute('SELECT ts,kind,detail FROM task_events WHERE task_id=? ORDER BY ts',(task_id,))]
        if not r:raise ValueError('Unknown browser task.')
        steps=[json.loads(e['detail']) for e in ev if e['kind']=='step' and e['detail']]
        last=max([e['ts'] for e in ev]+[r['activated'] or 0])
        return {'id':r['id'],'title':json.loads(r['spec'])['title'],'status':r['status'],'steps':len(steps),
            'steps_completed':sum(1 for x in steps if x.get('status')=='completed'),'not_dispatched':sum(1 for x in steps if x.get('status')=='not_dispatched'),
            'no_change_steps':sum(1 for x in steps if x.get('changed') is False),'owner_reviews':sum(1 for e in ev if e['kind']=='review'),
            'owner_approvals':sum(1 for e in ev if e['kind']=='owner_approval'),'takeovers':sum(1 for e in ev if e['kind']=='takeover'),
            'budget_pauses':sum(1 for e in ev if e['kind']=='paused_by_budget'),'auto_confirmed_final':any(e['kind']=='final_preapproved' for e in ev),
            'minutes':round((last-(r['activated'] or last))/60,1),**self.model_cost(ev),'site':site_of_spec(json.loads(r['spec'])),
            'autostarted':any(e['kind']=='autostarted' for e in ev),'duplicates_refused':sum(1 for e in ev if e['kind']=='duplicate_refused'),
            'injection_flags':sum(1 for e in ev if e['kind']=='injection_flag'),'look_calls':sum(1 for e in ev if e['kind']=='look'),
            'worker_runs':sum(1 for e in ev if e['kind']=='worker'),'escalated':any(e['kind'] in ('escalated','paused_by_budget') for e in ev),'lane':self.lane_of(json.loads(r['spec']))}
    def lane_of(self,spec):
        try:return self.b.session(spec['session_id']).get('lane','cloud')
        except Exception:return 'cloud'
    @staticmethod
    def model_cost(ev):
        items=[json.loads(e['detail']) for e in ev if e['kind']=='model_cost' and e['detail']]
        if not items:return {'model_cost_usd':None,'model_tokens':None}
        usd=sum(float(x.get('usd') or 0) for x in items);tokens=int(sum(int(x.get('tokens') or 0) for x in items))
        # Tokens without a price (the gateway did not report cost) are shown as tokens, never as $0.
        return {'model_cost_usd':round(usd,4) if usd>0 or not tokens else None,'model_tokens':tokens}
    def week(self,days=7,offset=0):
        """The three numbers per site (PROCURATOR browsing plan): completion without takeover, owner approvals per
        completed task, model cost per completed task — over tasks started in the window."""
        end=time.time()-offset*86400;start=end-days*86400
        with self.b.db() as db:rows=db.execute('SELECT id FROM web_tasks WHERE activated>=? AND activated<? ORDER BY activated',(start,end)).fetchall()
        sites={}
        for row in rows:
            m=self.metrics(row['id']);x=sites.setdefault(m['site'],{'site':m['site'],'lanes':{},'started':0,'completed':0,'completed_no_takeover':0,'approvals_on_completed':0,'cost_usd':0.0,'cost_known':0,'tokens':0})
            x['started']+=1;x['lanes'][m['lane']]=x['lanes'].get(m['lane'],0)+1
            if m['status']=='complete':
                x['completed']+=1;x['approvals_on_completed']+=m['owner_approvals']+(0 if m['autostarted'] else 1)
                if not m['takeovers']:x['completed_no_takeover']+=1
            if m['model_cost_usd'] is not None:x['cost_usd']+=m['model_cost_usd'];x['cost_known']+=1;x['tokens']+=m['model_tokens'] or 0
        out=[]
        for x in sorted(sites.values(),key=lambda v:-v['started']):
            out.append({**x,'cost_usd':round(x['cost_usd'],4),
                'completion_without_takeover':round(x['completed_no_takeover']/x['started'],3) if x['started'] else None,
                'approvals_per_completed':round(x['approvals_on_completed']/x['completed'],2) if x['completed'] else None,
                'cost_per_completed_usd':round(x['cost_usd']/x['completed'],4) if x['completed'] and x['cost_known'] else None})
        total={k:sum(x[k] for x in out) for k in ('started','completed','completed_no_takeover','approvals_on_completed','cost_known','tokens')}
        total['cost_usd']=round(sum(x['cost_usd'] for x in out),4)
        total.update(completion_without_takeover=round(total['completed_no_takeover']/total['started'],3) if total['started'] else None,
            approvals_per_completed=round(total['approvals_on_completed']/total['completed'],2) if total['completed'] else None,
            cost_per_completed_usd=round(total['cost_usd']/total['completed'],4) if total['completed'] and total['cost_known'] else None)
        try:total['routes']=self.b.router.share(start)
        except Exception:pass
        total['look_calls']=sum(self.metrics(row['id'])['look_calls'] for row in rows)
        try:total["playbooks"]=self.b.playbooks.stats()
        except Exception:pass
        return {'days':days,'from':start,'to':end,'sites':out,'total':total,
            'notes':'Approvals count the task approval (/webtask, unless your standing /siteauto permission started it) plus each /webapprove. Cost is the assistant\'s model usage while it worked on the task (reported by the gateway).'}
    def call(self,action,args,privileged=False):
        b=self.b
        if action=='task_stats':
            with b.db() as db:rows=db.execute('SELECT id FROM web_tasks WHERE activated IS NOT NULL ORDER BY activated DESC LIMIT ?',(min(int(args.get('limit',10)),30),)).fetchall()
            items=[self.metrics(x['id']) for x in rows]
            done=[x for x in items if x['status']=='complete']
            return {'tasks':items,'summary':{'tasks':len(items),'completed':len(done),
                'avg_steps_completed_tasks':round(sum(x['steps'] for x in done)/len(done),1) if done else None,
                'owner_interventions':sum(x['owner_approvals']+x['takeovers'] for x in items),'budget_pauses':sum(x['budget_pauses'] for x in items)}}
        if action=='task_speed':
            # Two-speed loop setting (Phase 6): routine steps on a cheaper model in the task's own session, plan and gated checks on the main model.
            with b.db() as db:r0=db.execute("SELECT value FROM owner_settings WHERE key='two_speed'").fetchone()
            cur=json.loads(r0['value']) if r0 else {'on':True,'cheap_model':'haiku'}
            if args.get('set') is not None:
                if not privileged:raise ValueError('Owner only.')
                new=dict(cur)
                if args['set'] in ('on','off'):new['on']=args['set']=='on'
                elif args['set'] in ('haiku','terra','sonnet'):new['cheap_model']=args['set']
                else:raise ValueError('Use /webspeed on|off|haiku|terra|sonnet.')
                with b.db() as db:db.execute("INSERT OR REPLACE INTO owner_settings VALUES('two_speed',?)",(json.dumps(new),))
                cur=new
            return cur
        if action=='task_week':return self.week(max(1,min(90,int(args.get('days',7)))),max(0,min(365,int(args.get('offset_days',0)))))
        if action=='task_cost':
            # Gateway usage hook (plugin): model usage while the assistant worked on this task. Owner socket only.
            if not privileged:raise ValueError('Owner only.')
            r=self.row(args.get('id'))
            usd=args.get('cost_usd',0);tokens=args.get('tokens',0)
            if type(usd) not in (int,float) or not 0<=usd<50 or type(tokens) is not int or not 0<=tokens<5_000_000:raise ValueError('Bad usage report.')
            if r['status'] in ('expired','cancelled') and r['activated'] and time.time()-r['activated']>86400:return {'recorded':False}
            b.event(r['id'],'model_cost',{'usd':round(float(usd),6),'tokens':tokens,'model':str(args.get('model') or '')[:60]})
            return {'recorded':True}
        if action=='task_list':
            with b.db() as db:rows=db.execute("SELECT id FROM web_tasks WHERE status NOT IN ('complete','cancelled','expired') ORDER BY rowid DESC LIMIT 20").fetchall()
            return {'tasks':[self.public(self.row(x['id'])) for x in rows]}
        if action=='task_prepare':
            s=b.session(args.get('session_id'))
            if s.get('project_read_only'):raise ValueError('Connected project browsers are read-only; they cannot grant interaction tasks.')
            if s['status']!='open':raise ValueError('Open the task browser first.')
            rid=self.text(args.get('request_id'),100)
            if len(rid)<8:raise ValueError('Use a stable task request_id.')
            spec=normalize_task(args,s,self.url,self.text);encoded=json.dumps(spec,ensure_ascii=False)
            if spec['completion_text'] and spec['completion_text'] in b.snapshot(s)['text']:raise ValueError('The completion marker is already present. Use a marker that establishes the requested final outcome.')
            if len(preview(spec).encode('utf-16-le'))//2>3000:raise ValueError('Task scope is too large for a complete Telegram review.')
            with b.db() as db:
                old=db.execute('SELECT id,spec FROM web_tasks WHERE request_id=?',(rid,)).fetchone()
                if old:
                    if old['spec']!=encoded:raise ValueError('Task request ID already belongs to another revision.')
                    return self.public(self.row(old['id']))
                identifier=uuid.uuid4().hex[:16]
                for name in spec.get('upload_files',[]):b.uploads.get(s['id'],name)   # each named file must already be staged
                db.execute('INSERT INTO web_tasks(id,request_id,spec,hash,status,reviewed,activated,expires,steps_used,pending_action,context,result,no_progress,failures) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0)',(identifier,rid,encoded,self.digest(spec),'draft',None,None,None,0,None,None,None))
            out=self.public(self.row(identifier))
            # 0.18.0: route decision (Phase 6), trajectory hints (Phase 4) and the owner-fact keys the task may type.
            site=site_of_spec(spec)
            try:out['route']=b.router.route(site,spec['goal'],identifier)
            except Exception:pass
            try:
                h=b.memory.hints(site,spec['title']+' '+spec['goal'])
                if h:out['hints']=h
            except Exception:pass
            try:
                ls=b.recall.site_lessons(site)
                if ls:out['lessons']={'from_memory':ls,'how':'Lessons saved from earlier runs on this site (data). Use what applies; add new ones with memory_note kind=lesson.'}
            except Exception:pass
            try:
                pb=b.playbooks.find(site,spec['title']+' — '+spec['goal'])
                if pb and not pb['demoted']:out['playbook']={'steps':len(pb['steps']),'runs':pb['runs'],'how':'After the task starts, call web_task_playbook to replay the routine steps; it stops at anything gated or task-specific.'}
            except Exception:pass
            keys=b.vault.keys()
            if keys:out['owner_fact_keys']={'keys':keys,'how':'Type {{owner.KEY}} as a value (e.g. {{owner.phone}}); the broker types the saved value. You never see it.'}
            return out
        r=self.row(args.get('id'));spec=json.loads(r['spec'])
        if action=='task_playbook':
            # Phase 7: replay the site's playbook from where this task is, through the normal task step (every gate applies).
            self.model_context(r,args.get('runtime_context'));self.active(r['id'])
            from broker import page_sig
            pb=b.playbooks.find(site_of_spec(spec),spec['title']+' — '+spec['goal'])
            if not pb:return {'replayed':[],'stopped':'no_playbook','note':'No playbook for this errand on this site yet (it needs 3 completed runs). Work step by step.'}
            if pb['demoted']:return {'replayed':[],'stopped':'demoted','note':'The playbook for this site is demoted to hints only ('+(pb['demoted_reason'] or '')+'). Work step by step.'}
            idx=b.playbooks.cursor(r['id'],pb['id']);done=[];stopped='end';hint=None
            for n in range(min(int(args.get('max_steps',10) or 10),15)):
                snap=b.snapshot(b.session(spec['session_id']))
                m=b.playbooks.match(pb,idx,snap['controls'],page_sig(snap['controls']))
                if not m:
                    stopped='drift' if idx<len(pb['steps']) else 'end'
                    hint='The page no longer matches the playbook (step %d of %d). Take this step yourself; the playbook is rebuilt from this run when the task completes.'%(idx+1,len(pb['steps'])) if stopped=='drift' else 'Playbook finished.'
                    break
                j,st,c=m
                if st['kind'] in ('submit','upload','select') or st['cls'] in ('final','commit','protected') or (st['kind']=='fill' and not st.get('value_key')):
                    stopped='needs_you';b.playbooks.advance(r['id'],pb['id'],j)
                    hint='Next playbook step: %s "%s" (ref %s). %s'%(st['kind'],st['label'],c['ref'],'It is gated: prepare it with web_task_step and send the review.' if st['kind']=='submit' or st['cls'] in ('final','commit','protected') else 'It needs a value or choice from this task: do it with web_task_step.')
                    break
                step={'kind':st['kind'],'ref':c['ref']}
                if st['kind']=='fill':step['value']='{{'+st['value_key']+'}}'
                res=self.call('task_step',{'id':r['id'],'runtime_context':args.get('runtime_context'),'request_id':'pb-%s-%d-%d-%d'%(r['id'][:8],pb['id'],j,int(time.time()*1000)%10**7),'steps':[step]})
                ok=res.get('status')=='completed' and ((res.get('result') or {}).get('progress') or {}).get('changed') is not False
                b.playbooks.note(pb['id'],ok)
                done.append({'step':j+1,'kind':st['kind'],'label':st['label'],'status':res.get('status'),'stop_reason':res.get('stop_reason')})
                if not ok:
                    stopped='step_failed';hint='A replayed step did not work (%s). Continue yourself from a fresh snapshot.'%(res.get('stop_reason') or res.get('status'))
                    if res.get('action_id'):hint+=' action_id '+res['action_id']+' needs a review.'
                    break
                idx=j+1;b.playbooks.advance(r['id'],pb['id'],idx)
            b.event(r['id'],'playbook',{'replayed':len(done),'stopped':stopped})
            return {'replayed':done,'stopped':stopped,'next':hint,'playbook':{'runs':pb['runs'],'steps':len(pb['steps']),'at_step':idx+1}}
        if action=='task_lane':
            # Plugin only: should this continuation run in the worker session on the cheap model, or in their conversation?
            if not privileged:raise ValueError('Owner only.')
            with b.db() as db:ev=[x['kind'] for x in db.execute('SELECT kind FROM task_events WHERE task_id=?',(r['id'],))]
            speed=self.call('task_speed',{},True)
            escalated='paused_by_budget' in ev or 'escalated' in ev
            return {'id':r['id'],'status':r['status'],'autonomous':spec.get('mode')=='autonomous','two_speed':bool(speed.get('on')),'cheap_model':speed.get('cheap_model','haiku'),
                    'escalated':escalated,'worker':bool(speed.get('on')) and spec.get('mode') in ('autonomous','research') and not escalated,'worker_key':r.get('worker_key')}
        if action=='task_delegate':
            if not privileged:raise ValueError('Owner only.')
            key=self.text(args.get('worker_key'),120)
            if not re.fullmatch(r'agent:main:webtask-[a-f0-9]{16}',key) or key!='agent:main:webtask-'+r['id']:raise ValueError('Bad worker session.')
            with b.db() as db:db.execute('UPDATE web_tasks SET worker_key=? WHERE id=?',(key,r['id']))
            b.event(r['id'],'worker',{'model':self.text(str(args.get('model') or ''),60)})
            return {'id':r['id'],'worker_key':key}
        if action=='task_escalate':
            if not privileged:raise ValueError('Owner only.')
            b.event(r['id'],'escalated',{'why':self.text(str(args.get('why') or ''),200)});return {'id':r['id'],'escalated':True}
        if action=='task_get':
            if r['context']:
                self.model_context(r,args.get('runtime_context'))
                with b.db() as db:db.execute("UPDATE web_task_resumes SET status='observed',updated=? WHERE task_id=? AND status IN ('wake_requested','run_started')",(time.time(),r['id']))
            return self.public(r)
        if action=='task_review':
            if r['status'] not in ('draft','reviewed'):raise ValueError('This task grant cannot be reviewed again.')
            # 0.20.6: /webbench. Their run is one approval for READ-ONLY tasks in the test's own conversation, only while a case
            # runs. Anything else is not approved by the run: no review goes to them and the assistant is told to stop there.
            rc=args.get('runtime_context') or {}
            if getattr(b,'bench',None) and b.bench.is_bench_session_key(rc.get('sessionKey')):
                if spec.get('mode')=='research':
                    self.start(r,spec,self.context(rc),'bench_autostarted')
                    return {**self.public(self.row(r['id'])),'autostarted':True,'next':'Started under the owner\'s /webbench approval (read-only). Continue with web_task_step; finish with web_task_complete and your answer.'}
                b.event(r['id'],'bench_review_withheld',{'mode':spec.get('mode')})
                return {'id':r['id'],'status':'needs_owner_approval','review_delivered_to_owner':False,
                        'next':'This is an automated web test and the owner is not approving tasks now. Do not continue or retry this task. Report that it needs their approval (what you would do, with the price), and stop.'}
            # Owner's standing permission for this saved login (/siteauto NAME on): an autonomous task starts at once.
            # Authority is that native owner setting; final submissions, payment, sign-in and account settings still stop.
            if spec.get('mode') in ('autonomous','research') and spec.get('site_login') and spec['site_login'] in b.task_autostart() and args.get('runtime_context'):
                context=self.context(args.get('runtime_context'))
                self.start(r,spec,context,'autostarted')
                b.send_with_buttons('BROWSER TASK STARTED — '+r['id']+' (your standing permission for your '+spec['site_login']+' login; /siteauto '+spec['site_login']+' off turns it off)\n\n'+preview(spec)+
                    '\n\nNothing is submitted without you: the final submit comes to you for approval with every entry listed.\nPause: /webpause '+r['id']+'  Stop: /webstop '+r['id'],self.buttons(r['id'],spec))
                return {**self.public(self.row(r['id'])),'autostarted':True,'next':'Started under the owner\'s standing permission for this saved login; no /webtask is needed. Continue now with web_task_step and fill the whole form yourself; send web_browser_review only for the final submit (or another stop).'}
            b.send_with_buttons('BROWSER TASK REVIEW — '+r['id']+'\n\n'+preview(spec)+'\n\nApprove this task scope: tap ✅ Copy approval line below, then paste and send it (or send the entire line):\n/webtask '+r['id']+' '+r['hash'][:16]+'\n'+(('Free booking inside these limits: no further approval; you get the confirmation. Anything with a price or outside the limits comes back to you. ' if spec.get('free_booking_auto_confirm') else 'You will get one more approval before the final order/booking. ') if spec.get('mode')=='autonomous' else 'Final submissions still require /webapprove. ')+'Approving replaces any older task on this browser. '+'Pause: /webpause '+r['id']+'  Stop: /webstop '+r['id'],self.buttons(r['id'],spec,approve='/webtask '+r['id']+' '+r['hash'][:16]))
            with b.db() as db:db.execute("UPDATE web_tasks SET status='reviewed',reviewed=? WHERE id=?",(time.time(),r['id']))
            return {'id':r['id'],'status':'reviewed','review_delivered_to_owner':True}
        if action=='task_activate':
            if not privileged:raise ValueError('Only the native Telegram owner command can activate a browser task.')
            if args.get('hash')!=r['hash'][:16]:raise ValueError('Approval does not match this exact task scope.')
            context=self.context(args.get('context'))
            if r['status']=='active':self.same_context(r,context);return self.public(r)
            if r['status']!='reviewed' or not r['reviewed'] or not 0<=time.time()-r['reviewed']<=600:raise ValueError('A fresh task review is required.')
            self.start(r,spec,context)
            return self.public(self.row(r['id']))
        if action=='task_step':
            self.model_context(r,args.get('runtime_context'))
            self.active(r['id']);b.hold()
            rid=self.text(args.get('request_id'),100);fingerprint=self.digest(args.get('steps'))
            with b.db() as db:previous=db.execute('SELECT * FROM web_task_requests WHERE request_id=?',(rid,)).fetchone()
            if previous:
                if previous['task_id']!=r['id'] or previous['fingerprint']!=fingerprint:raise ValueError('Request ID belongs to another task step revision.')
                saved=b.plan(previous['plan_id'])
                if saved['status'] in ('completed','stale','uncertain','verification_pending','acknowledged_uncertain','executing','not_dispatched','fields_incomplete'):return b.public_plan(saved)
            p=b.call('prepare',{'id':spec['session_id'],'request_id':args.get('request_id'),'steps':args.get('steps'),'task_id':r['id']})
            saved=b.plan(p['id'])
            with b.db() as db:db.execute('INSERT OR IGNORE INTO web_task_requests VALUES(?,?,?,?)',(rid,r['id'],fingerprint,p['id']))
            if saved['status'] in ('completed','stale','uncertain','verification_pending','acknowledged_uncertain','executing','not_dispatched','fields_incomplete'):return b.public_plan(saved)
            plan=json.loads(saved['plan'])
            preapproved=False
            if spec.get('mode')=='research':
                # 0.19.2: read-only task. Anything outside it is refused here; no review is offered and nothing runs.
                from task_policy import research_decision
                allowed,reason=research_decision(spec,plan)
                if not allowed:
                    with b.db() as db:db.execute("UPDATE plans SET status='cancelled' WHERE id=? AND status='prepared'",(p['id'],))
                    b.event(r['id'],'read_only_refused',{'reason':reason})
                    guide=STOP_GUIDANCE['read_only'] if reason=='read_only' else STOP_GUIDANCE.get(reason,STOP_GUIDANCE['read_only']).replace('SESSION_ID',spec['session_id'])+' (This is a read-only task: do not send web_browser_review.)'
                    return {'id':r['id'],'status':'refused_read_only','stop_reason':reason,'reason':guide}
            elif spec.get('mode')=='autonomous':
                allowed,reason=autonomous_decision(spec,plan)
                preapproved=allowed and reason=='final_preapproved'
                if not allowed:
                    out={'id':r['id'],'status':'over_limit' if reason=='over_limit' else 'needs_separate_review','stop_reason':reason,
                        'action_id':p['id'],'reason':STOP_GUIDANCE[reason].replace('SESSION_ID',spec['session_id'])}
                    if reason=='over_limit':
                        out.update(detected_total=plan.get('detected_total'),spend_limit_usd=spec.get('spend_limit_usd'))
                        with b.db() as db:db.execute("UPDATE plans SET status='cancelled' WHERE id=? AND status='prepared'",(p['id'],))
                    return out
            elif not routine_allowed(spec,plan):
                return {'id':r['id'],'status':'needs_separate_review','action_id':p['id'],'reason':'This step is outside the approved routine scope. Use web_browser_review; do not execute it automatically.'}
            from task_policy import space
            # Exact grants match by label, so duplicates are ambiguous. Autonomous
            # steps are frozen to the snapshot ref and a unique private selector.
            snap=b.snapshot(b.session(spec['session_id'])) if spec.get('mode') not in ('autonomous','research') else {'controls':[]}
            for step in (plan['steps'] if spec.get('mode') not in ('autonomous','research') else []):
                if step['kind']!='navigate' and sum(space(c['label'])==space(step['control']['label']) for c in snap['controls'])!=1:
                    return {'id':r['id'],'status':'needs_separate_review','action_id':p['id'],'reason':'The approved label matches multiple controls. Separate exact action review is required.'}
            with b.db() as db:
                old=db.execute('SELECT task_id FROM web_task_actions WHERE plan_id=?',(p['id'],)).fetchone()
                if not old:
                    current=db.execute('SELECT steps_used FROM web_tasks WHERE id=?',(r['id'],)).fetchone()
                    if current['steps_used']>=spec['max_steps']:raise ValueError('Task action limit reached. A fresh owner-approved task is required.')
                    db.execute('INSERT INTO web_task_actions VALUES(?,?)',(p['id'],r['id']))
                    db.execute('UPDATE web_tasks SET steps_used=steps_used+1 WHERE id=?',(r['id'],))
            if preapproved:
                # Authority: the owner's /webtask approval of this exact free-booking scope.
                with b.db() as db:db.execute('UPDATE plans SET reviewed=? WHERE id=?',(time.time(),p['id']))
                saved=b.plan(p['id'])
            result=b.perform(saved);self.after_action(r['id'],result['status'])
            result=self.budget(r['id'],spec,result,plan)
            if preapproved:
                b.event(r['id'],'final_preapproved',{'plan':p['id'],'status':result['status']})
                label=plan['steps'][0]['control']['label'] if plan['steps'] and plan['steps'][0]['kind']=='click' else ''
                try:b.provider.telegram('FREE BOOKING CONFIRMED UNDER YOUR TASK APPROVAL — '+r['id']+'\nClicked "'+label[:80]+'" on '+plan.get('url','')[:200]+'\nNo price or payment field was on the page, and it showed a date/time/place inside your limits.\nResult: '+result['status']+'. I will send the site\'s confirmation next; nothing else is changed.')
                except Exception:pass
                b.screenshot_to_owner(b.session(spec['session_id']),'Page right after the free booking was confirmed (task '+r['id']+').')
                result={**result,'final_preapproved':True,'next':'Read a fresh snapshot, then call web_task_complete to record the visible confirmation, and send the owner the confirmation details.'}
            return result
        if action=='task_complete':
            self.model_context(r,args.get('runtime_context'))
            if r.get('worker_key') and (args.get('runtime_context') or {}).get('sessionKey')==r['worker_key']:
                raise ValueError('The final check belongs to the main conversation: stop here and report; it will read the confirmation and complete the task.')
            if r['status']=='complete':return self.public(r)
            self.active(r['id']);b.hold()
            if spec.get('mode')=='research':
                # 0.19.2: a read-only task ends with the answer it read; there is no final action to verify.
                snap=b.snapshot(b.session(spec['session_id']))
                outcome={'research':True,'answer':self.text(str(args.get('answer') or ''),1500),'url':snap.get('url','')[:500],'title':snap.get('title','')[:200],
                         'lane':self.lane_of(spec),'browser_session':spec['session_id']}
                b.event(r['id'],'complete',{'confirmation':'research answer','url':outcome['url'][:200]})
                try:
                    with b.db() as db:
                        rows=db.execute('SELECT kind,label,cls,page_sig,value_key,tag,ctype FROM task_journal WHERE task_id=? ORDER BY id',(r['id'],)).fetchall()
                    intent=spec['title']+' — '+spec['goal']
                    b.memory.record(site_of_spec(spec),intent,rows,1,r['id']);b.playbooks.learn(site_of_spec(spec),intent,rows)
                except Exception:pass
                with b.db() as db:db.execute("UPDATE web_tasks SET status='complete',result=? WHERE id=?",(json.dumps(outcome),r['id']))
                try:b.call('close',{'id':spec['session_id']});outcome['browser_closed']=True
                except Exception:outcome['browser_closed']=False
                with b.db() as db:db.execute('UPDATE web_tasks SET result=? WHERE id=?',(json.dumps(outcome),r['id']))
                return self.public(self.row(r['id']))
            if not spec['completion_text'] and spec.get('mode')!='autonomous':raise ValueError('This task has no approved completion marker; ask the owner to verify the result before stopping.')
            with b.db() as db:
                final=db.execute("SELECT id,plan FROM plans WHERE task_id=? AND status='completed' AND reviewed IS NOT NULL ORDER BY rowid DESC LIMIT 1",(r['id'],)).fetchone()
            if not final:raise ValueError('A separately owner-approved final action has not completed.')
            from task_policy import COMMIT
            steps=json.loads(final['plan'])['steps']
            from task_policy import FINAL,SOFT_FINAL
            final_label=steps[0]['control']['label'] if steps and steps[0]['kind']=='click' else ''
            autonomous_final=spec.get('mode')=='autonomous' and (FINAL.search(final_label) or SOFT_FINAL.search(final_label))
            if len(steps)!=1 or steps[0]['kind']!='click' or not (steps[0]['control']['type']=='submit' or COMMIT.search(final_label) or autonomous_final):
                raise ValueError('The last approved action was not a final submission/commitment.')
            snap=b.snapshot(b.session(spec['session_id']))
            if spec['completion_text']:
                if spec['completion_text'] not in snap['text']:raise ValueError('The expected confirmation is not visible. Inspect the site; never repeat a submission.')
                outcome={'confirmation':spec['completion_text'],'approved_final_action_id':final['id']}
            else:
                # Nov 4 CVS: "Good news! The owner is all set." was not recognized, so a real booking stayed open.
                found=re.search(r"[^\n]{0,80}\b(thank you|order (?:number|#|no\.?)|confirmation (?:number|#|code)|(?:order|booking|reservation|appointment|visit|vaccination|absence|request|application|claim) (?:is |was |has been )?(?:confirmed|placed|received|booked|scheduled|submitted)|(?:you'?re|you are|is|are) (?:all set|booked|confirmed|scheduled)|see you (?:on|at|then|soon))\b[^\n]{0,120}",snap['text'],re.I)
                if not found:raise ValueError('No confirmation is visible yet. Inspect the page and report what it shows; never repeat the final submission.')
                outcome={'confirmation':' '.join(found.group(0).split())[:200],'confirmation_rule':'visible confirmation phrase after the owner-approved final action','approved_final_action_id':final['id']}
            b.event(r['id'],'complete',{'confirmation':outcome.get('confirmation','')[:200]})
            try:
                with b.db() as db:
                    rows=db.execute('SELECT kind,label,cls,page_sig,value_key,tag,ctype FROM task_journal WHERE task_id=? ORDER BY id',(r['id'],)).fetchall()
                    ev=[x['kind'] for x in db.execute('SELECT kind FROM task_events WHERE task_id=?',(r['id'],))]
                intent=spec['title']+' — '+spec['goal']
                b.memory.record(site_of_spec(spec),intent,rows,ev.count('owner_approval')+(0 if 'autostarted' in ev else 1),r['id'])
                b.playbooks.learn(site_of_spec(spec),intent,rows)   # Phase 7: 3+ similar successes -> playbook (rebuilt = healed)
            except Exception:pass
            b.screenshot_to_owner(b.session(spec['session_id']),'Task '+r['id']+' complete: '+spec['title']+'\nConfirmation seen: '+outcome.get('confirmation','')[:200])
            with b.db() as db:db.execute("UPDATE web_tasks SET status='complete',result=? WHERE id=?",(json.dumps(outcome),r['id']))
            try:b.call('close',{'id':spec['session_id']});outcome['browser_closed']=True
            except Exception:outcome['browser_closed']=False
            with b.db() as db:db.execute('UPDATE web_tasks SET result=? WHERE id=?',(json.dumps(outcome),r['id']))
            return self.public(self.row(r['id']))
        if action in ('task_pause','task_resume','task_stop'):
            if not privileged:raise ValueError('Only the authenticated owner can change task execution state.')
            # 0.19.1 (Nov 6 QA): /webstop works from any conversation and on a task never activated: stopping only removes authority.
            if action!='task_stop':self.same_context(r,args.get('context'))
            if action=='task_resume':
                if r['status'] in ('paused','awaiting_approval'):
                    with b.db() as db:
                        db.execute("UPDATE plans SET status='cancelled' WHERE id=? AND task_id=? AND status IN ('prepared','reviewed')",(r['pending_action'],r['id']))
                        db.execute("UPDATE web_tasks SET status='active',pending_action=NULL WHERE id=?",(r['id'],))
                elif r['status']=='needs_attention':
                    b.hold()
                    with b.db() as db:db.execute("UPDATE web_tasks SET status='active',pending_action=NULL WHERE id=?",(r['id'],))
                elif r['status']!='active':raise ValueError('Task cannot resume: '+r['status'])
            elif action=='task_pause':
                if r['status'] not in ('active','awaiting_approval'):raise ValueError('Task cannot pause: '+r['status'])
                with b.db() as db:db.execute("UPDATE web_tasks SET status='paused' WHERE id=?",(r['id'],))
            else:
                if r['status'] in ('complete','cancelled'):return self.public(r)
                with b.db() as db:
                    db.execute("UPDATE web_tasks SET status='cancelled' WHERE id=?",(r['id'],))
                    db.execute("UPDATE plans SET status='cancelled' WHERE task_id=? AND status IN ('prepared','reviewed')",(r['id'],))
                try:b.call('close',{'id':spec['session_id']})
                except Exception:pass
                b.event(r['id'],'stopped')
            return self.public(self.row(r['id']))
        if action=='task_resume_claim':
            if not privileged:raise ValueError('Continuation receipts are native-owner only.')
            self.same_context(r,args.get('context'))
            if r['status'] not in ('active','awaiting_approval','needs_attention'):return {'claimed':False,'status':r['status']}
            event=self.text(args.get('event_id'),120)
            with b.db() as db:
                old=db.execute('SELECT status FROM web_task_resumes WHERE event_id=?',(event,)).fetchone()
                if old:return {'claimed':False,'status':old['status']}
                # 0.19.2 (Nov 6 compat run): a continuation that never ran (a heartbeat wake the gateway dropped) no longer blocks
                # /webcontinue forever: in-progress receipts older than 15 minutes are stale.
                running=db.execute("SELECT status FROM web_task_resumes WHERE task_id=? AND status IN ('claimed','accepted','wake_requested','run_started') AND updated>?",(r['id'],time.time()-900)).fetchone()
                if running:return {'claimed':False,'status':'continuation_in_progress'}
                db.execute('INSERT INTO web_task_resumes VALUES(?,?,?,?)',(event,r['id'],'claimed',time.time()))
            return {'claimed':True,'id':r['id'],'event_id':event}
        if action=='task_resume_receipt':
            if not privileged:raise ValueError('Continuation receipts are native-owner only.')
            self.same_context(r,args.get('context'));state=args.get('status')
            if state not in ('accepted','wake_requested','run_started','completed','failed','cancelled','admission_unconfirmed'):raise ValueError('Invalid continuation status.')
            with b.db() as db:db.execute('UPDATE web_task_resumes SET status=?,updated=? WHERE task_id=? AND event_id=?',(state,time.time(),r['id'],args.get('event_id')))
            return {'recorded':True}
        raise ValueError('Unknown browser task operation.')
