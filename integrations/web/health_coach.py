"""Health and wellness coach (web 0.26.0, Nov 8 2026). OPTIONAL: off until the owner turns it on (/coach on).

What it does
  * A short wellness questionnaire: goals (weight loss, heart health, calorie tracking, activity, stress, sleep), a few screening
    questions that decide what the coach may NOT do, and when the owner wants reminders.
  * A weight-loss calorie plan from BMI, the NUTR-SPEC-0.1 maintenance estimate (health_nutrition.py) and published guidance, proposed
    as a calorie goal the owner confirms themselves (/healthgoal, unchanged).
  * A daily energy balance: calories logged (food log) against calories burned (Apple Health resting + active energy when synced,
    otherwise the maintenance estimate), the deficit equation, a cumulative view and, after a few weeks, their maintenance as observed
    from their own intake and weight trend.
  * Metrics Apple Health does not cover here: blood pressure, resting heart rate, waist, sleep, stress, mood, water, exercise and
    meditation minutes (plus weight, stored with the nutrition module's measurements).
  * Reminders with fixed wording ("time to log today's meals"), capped per day, quiet at night, paused when anything looks unsafe.

Published basis (the numbers below cite these; method COACH-SPEC-0.1, PENDING PHYSICIAN REVIEW like NUTR-SPEC-0.1)
  * BMI categories: WHO adult classification (<18.5, 18.5-24.9, 25-29.9, >=30).
  * Deficit: 500-750 kcal/day for adults with overweight or obesity (2013 AHA/ACC/TOS obesity guideline; NHLBI 1998), which also lists
    1,200-1,500 kcal/day for women and 1,500-1,800 kcal/day for men as typical reduced intakes: the LOWER ends are the coach's floors,
    and never below their estimated resting energy. 750 kcal/day only with BMI >= 30. In the healthy BMI range the coach offers at most a
    gentle 250 kcal/day. Below BMI 18.5 it does not plan weight loss.
  * Pace: at most 1% of body weight per week (common clinical ceiling); the static rule of 7,700 kcal per kg (about 3,500 kcal per lb)
    is shown as a rough guide only: real loss slows as weight falls (Hall et al., Lancet 2011; NIH Body Weight Planner).
  * Activity: 150-300 minutes a week of moderate activity, or 75-150 vigorous (vigorous counts double) — Physical Activity Guidelines
    for Americans, 2nd ed., 2018.
  * Blood pressure categories: 2017 ACC/AHA (normal <120/<80; elevated 120-129/<80; stage 1 130-139 or 80-89; stage 2 >=140 or >=90;
    above 180 and/or above 120 needs prompt attention).
  * Sleep: 7 or more hours for adults (AASM/SRS consensus, 2015).
  * Stress: a 2-minute paced-breathing exercise (box breathing) is offered; the coach never diagnoses or treats.

Safety rules the code enforces (not just the prompt)
  * Adults only (the health module's /healthadult), not pregnant or breastfeeding (NUTR-SPEC population).
  * A past or current eating disorder (their answer) turns OFF calorie targets, deficit numbers and meal-count reminders; the coach keeps
    only general wellness (activity, sleep, stress) and suggests clinician support.
  * Conditions where a calorie deficit should be supervised (diabetes on insulin or sulfonylureas, kidney disease, heart failure,
    weight-loss medicines, bariatric surgery) mean the coach computes no target of its own: only a target from their clinician.
  * If logged intake falls below the floor on 3 of the last 7 complete days, or weight falls faster than 1%/week for 3 weeks, the
    coach pauses calorie reminders and deficit encouragement and says so once.
  * Reminders are generic (no numbers, no health details) and go to Telegram; numbers stay on their dashboard and in replies the owner asks for.
"""
import datetime as dt
import json
import re
import secrets

from health import clean, no_secrets
from health_nutrition import ACTIVITY, LB, SPEC as NUTR_SPEC, age_on, bmi_category, to_kg

SPEC = {'id': 'COACH-SPEC-0.1', 'review': 'pending physician review'}
KCAL_PER_KG = 7700
GOALS = {'weight_loss': 'Lose weight', 'heart_health': 'Improve heart (cardiovascular) health', 'track_calories': 'Track calories',
         'activity': 'Move more / exercise regularly', 'stress': 'Manage stress', 'sleep': 'Sleep better', 'maintain': 'Maintain my weight'}
SUPERVISED = {'diabetes_meds': 'diabetes treated with insulin or a sulfonylurea', 'kidney': 'kidney disease', 'heart_failure': 'heart failure',
              'weight_meds': 'a weight-loss medicine (for example a GLP-1 medicine)', 'bariatric': 'bariatric surgery'}
PACES = {'gentle': 250, 'standard': 500, 'faster': 750}
FLOOR = {'female': 1200, 'male': 1500}
PROMPTS = {   # kind: (default local time, weekdays 0=Mon..6 or None for daily, fixed text)
    'meals': ('19:30', None, 'Coach: time to log today\'s meals — tell me what you ate (or send "skip"). Mark the day complete when it\'s all in.'),
    'exercise': ('20:30', None, 'Coach: did you move today? Log your exercise in one line, e.g. "30 min brisk walk".'),
    'weigh_in': ('07:30', (0,), 'Coach: weekly weigh-in — send your weight, or let the Apple Health app sync it.'),
    'stress': ('15:00', (0, 1, 2, 3, 4), 'Coach: quick check-in — how is your stress, 1 (calm) to 5 (very stressed)?'),
    'breathing': ('12:30', None, 'Coach: two-minute breathing break? In 4 seconds, hold 4, out 4, hold 4 — four rounds.'),
    'sleep': ('21:00', None, 'Coach: wind-down time — screens off soon to protect your 7+ hours of sleep.'),
    'blood_pressure': ('09:00', (5,), 'Coach: weekly blood pressure check — sit quietly 5 minutes, then send the reading, e.g. "124/78".'),
}
DEFAULT_PROMPTS = {'weight_loss': ['meals', 'weigh_in'], 'track_calories': ['meals'], 'activity': ['exercise'], 'heart_health': ['exercise', 'blood_pressure'],
                   'stress': ['stress'], 'sleep': ['sleep'], 'maintain': ['weigh_in']}
CALORIE_PROMPTS = ('meals',)
MAX_PER_DAY = 3
QUIET = ('21:30', '07:00')
METRICS = {   # kind: (unit, min, max)
    'blood_pressure': ('mmHg', 60, 260), 'resting_hr': ('bpm', 25, 220), 'waist': ('cm', 40, 250), 'sleep_hours': ('h', 0, 24),
    'stress': ('1-5', 1, 5), 'mood': ('1-5', 1, 5), 'water': ('ml', 0, 10000), 'exercise_minutes': ('min', 1, 600),
    'meditation_minutes': ('min', 1, 300), 'steps': ('count', 0, 100000),
}
BREATHING = ('Box breathing, about two minutes: breathe in through the nose for 4 seconds, hold 4, breathe out slowly for 4, hold 4. '
             'Repeat four rounds. If stress stays high for days or feels unmanageable, talk with your clinician or someone you trust.')
QUESTIONS = [
    {'id': 'goals', 'ask': 'Which goals should the coach help with? (any of: lose weight, heart health, track calories, move more, manage stress, sleep better, maintain weight)', 'type': 'goals'},
    {'id': 'target_weight', 'ask': 'If weight loss: is there a weight you are aiming for? (optional, with unit)', 'type': 'weight', 'optional': True},
    {'id': 'pace', 'ask': 'If weight loss: which pace — gentle (about 250 kcal/day), standard (about 500) or faster (about 750, only with BMI 30 or more)?', 'type': 'pace', 'optional': True},
    {'id': 'activity_level', 'ask': 'Which best describes a usual week: ' + '; '.join('%s (%s)' % (k, v[1]) for k, v in ACTIVITY.items()) + '?', 'type': 'activity'},
    {'id': 'eating_disorder', 'ask': 'Have you ever been diagnosed with or treated for an eating disorder, or does your eating ever feel out of control? (yes / no / prefer not to say)', 'type': 'yes_no_skip'},
    {'id': 'supervised', 'ask': 'Do any apply: diabetes treated with insulin or a sulfonylurea, kidney disease, heart failure, a weight-loss medicine, bariatric surgery? (list them, or none)', 'type': 'supervised'},
    {'id': 'pregnant', 'ask': 'Are you currently pregnant or breastfeeding? (skip if not applicable)', 'type': 'yes_no_skip', 'optional': True},
    {'id': 'reminders', 'ask': 'Which reminders do you want, and at what times? (defaults follow your goals: meal log 7:30 pm, exercise 8:30 pm, weekly weigh-in Monday 7:30 am, stress check-in 3 pm weekdays, sleep wind-down 9 pm, weekly blood pressure Saturday 9 am)', 'type': 'reminders', 'optional': True},
]


def bp_category(sys_, dia):
    if sys_ > 180 or dia > 120:
        return 'severe (above 180/120)'
    if sys_ >= 140 or dia >= 90:
        return 'stage 2 hypertension range'
    if sys_ >= 130 or dia >= 80:
        return 'stage 1 hypertension range'
    if sys_ >= 120:
        return 'elevated'
    return 'normal'


BP_URGENT = ('A reading above 180/120 needs prompt attention. Rest 5 minutes and measure again. If it is still this high, or with chest pain, '
             'shortness of breath, back pain, numbness or weakness, vision or speech changes, call 911. Otherwise contact your clinician today.')


def hhmm(s):
    m = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', str(s or '').strip())
    if not m:
        raise ValueError('Times are HH:MM (24-hour), e.g. 19:30.')
    return '%02d:%s' % (int(m.group(1)), m.group(2))


def plan_for(bmi, sex, maintenance, ree, weight_kg, pace='standard', screening=None, target_kg=None, height_cm=None):
    """Pure: the COACH-SPEC-0.1 weight-loss calorie plan. Returns a dict with target_kcal or a 'no_plan' reason."""
    s = screening or {}
    if s.get('eating_disorder') == 'yes':
        return {'no_plan': 'eating_disorder', 'why': 'With a history of an eating disorder the coach does not set calorie targets or deficits. A clinician or '
                'dietitian can set a plan with you; the coach can still help with activity, sleep and stress.'}
    sup = [SUPERVISED[k] for k in s.get('supervised') or [] if k in SUPERVISED]
    if sup:
        return {'no_plan': 'supervised', 'why': 'With %s, a calorie deficit should be set with your clinician. Ask them for a daily target; the coach '
                'can store it as a clinician target and track against it.' % ', '.join(sup)}
    if bmi < 18.5:
        return {'no_plan': 'underweight', 'why': 'Your BMI is below 18.5; the coach does not plan weight loss. Talk with your clinician if weight is a concern.'}
    if pace not in PACES:
        raise ValueError('pace is gentle, standard or faster.')
    notes = []
    if bmi < 25 and pace != 'gentle':
        pace = 'gentle'
        notes.append('Your BMI is in the healthy range, so the coach offers at most a gentle 250 kcal/day deficit; activity and strength may matter more than intake.')
    if pace == 'faster' and bmi < 30:
        pace = 'standard'
        notes.append('The faster pace (750 kcal/day) is only offered with a BMI of 30 or more; using the standard 500.')
    deficit = PACES[pace]
    floor = max(FLOOR.get(sex, 1500), int(round(ree / 10.0) * 10))
    target = int(round((maintenance - deficit) / 10.0) * 10)
    if target < floor:
        notes.append('The target was raised to the floor of %d kcal (%s), so the deficit is smaller than the %s pace.' % (
            floor, 'your estimated resting energy' if floor > FLOOR.get(sex, 1500) else 'the guideline minimum for %ss' % ('woman' if sex == 'female' else 'man'), pace))
        target = floor
    deficit = int(round(maintenance - target))
    if deficit <= 0:
        return {'no_plan': 'no_room', 'why': 'Your estimated maintenance is already at the floor; the coach will not set a lower target. More activity is the way to create a deficit.'}
    per_week_kg = deficit * 7 / KCAL_PER_KG
    if per_week_kg > 0.01 * weight_kg:
        cap = int(0.01 * weight_kg * KCAL_PER_KG / 7)
        target = int(round((maintenance - cap) / 10.0) * 10)
        deficit = int(round(maintenance - target))
        per_week_kg = deficit * 7 / KCAL_PER_KG
        notes.append('Limited to about 1% of body weight per week.')
    out = {'pace': pace, 'maintenance_kcal': int(round(maintenance / 10.0) * 10), 'deficit_kcal': deficit, 'target_kcal': target, 'floor_kcal': floor,
           'rough_weekly_change_kg': round(-per_week_kg, 2), 'rough_weekly_change_lb': round(-per_week_kg / LB, 1), 'notes': notes,
           'equation': 'daily deficit = calories burned − calories eaten; target = maintenance (%d) − %d = %d kcal/day' % (int(round(maintenance / 10.0) * 10), deficit, target),
           'pace_note': 'Rough guide: about %d kcal per kg (3,500 per lb). Real loss slows as weight falls; the weight trend over 3-4 weeks is the real measure.' % KCAL_PER_KG}
    if target_kg and height_cm:
        tb = target_kg / (height_cm / 100) ** 2
        if tb < 18.5:
            out['target_weight'] = {'refused': True, 'why': 'That weight would be a BMI of %.1f (below 18.5); the coach will not plan toward it.' % tb}
        else:
            to_lose = weight_kg - target_kg
            weeks = (to_lose / per_week_kg) if per_week_kg > 0 and to_lose > 0 else None
            out['target_weight'] = {'bmi_at_target': round(tb, 1), 'category_at_target': bmi_category(tb), 'to_lose_kg': round(to_lose, 1),
                                    'rough_weeks_static': int(weeks + 0.999) if weeks else None,
                                    'note': 'The week count uses the static rule and will be optimistic; expect it to take longer.'}
    return out


class Coach:
    def __init__(self, health, nutrition):
        self.h, self.n = health, nutrition
        with self.h.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS coach(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER,enabled_at REAL,answers TEXT,goals TEXT,screening TEXT,
                    prompts TEXT,paused_until REAL,caution TEXT,caution_told REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS coach_metrics(id TEXT PRIMARY KEY,kind TEXT,value REAL,value2 REAL,unit TEXT,local_date TEXT,local_time TEXT,
                    detail TEXT,source TEXT,deleted INTEGER DEFAULT 0,created REAL);
                CREATE TABLE IF NOT EXISTS coach_sent(kind TEXT,local_date TEXT,sent_at REAL,PRIMARY KEY(kind,local_date));
            ''')

    # ---------- state ----------
    def state(self):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM coach WHERE id=1').fetchone()
        s = dict(r) if r else {'enabled': 0}
        for k in ('answers', 'goals', 'screening', 'prompts', 'caution'):
            try:
                s[k] = json.loads(s.get(k) or ('[]' if k == 'goals' else '{}'))
            except ValueError:
                s[k] = [] if k == 'goals' else {}
        return s

    def save(self, **kw):
        s = self.state()
        s.update(kw)
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO coach VALUES(1,?,?,?,?,?,?,?,?,?,?)', (int(bool(s.get('enabled'))), s.get('enabled_at'), json.dumps(s.get('answers') or {}),
                       json.dumps(s.get('goals') or []), json.dumps(s.get('screening') or {}), json.dumps(s.get('prompts') or {}), s.get('paused_until'),
                       json.dumps(s.get('caution') or {}), s.get('caution_told'), self.h.now()))
        return self.state()

    def need_on(self):
        if not self.state().get('enabled'):
            raise ValueError('The wellness coach is off. The owner turns it on with /coach on (it is optional).')

    def now_local(self):
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.fromtimestamp(self.h.now(), ZoneInfo(self.n.tz))
        except Exception:
            return dt.datetime.utcfromtimestamp(self.h.now())

    def status(self):
        s = self.state()
        out = {'enabled': bool(s.get('enabled')), 'spec': SPEC['id'] + ' (' + SPEC['review'] + ')'}
        if not s.get('enabled'):
            return out | {'next': 'Off. The owner turns it on with /coach on; then run the questionnaire (health_coach_questionnaire).'}
        out.update({'goals': [GOALS[g] for g in s['goals'] if g in GOALS], 'questionnaire_done': bool(s.get('answers')),
                    'reminders': {k: v for k, v in (s.get('prompts') or {}).items()}, 'paused_until': s.get('paused_until'),
                    'limits': self.limits(s), 'caution': (s.get('caution') or {}).get('why')})
        if not s.get('answers'):
            out['next'] = 'Run the questionnaire: health_coach_questionnaire, ask the questions one or two at a time, then health_coach_answers.'
        return out

    def limits(self, s=None):
        s = s or self.state()
        sc = s.get('screening') or {}
        lim = []
        if sc.get('eating_disorder') == 'yes':
            lim.append('no calorie targets, deficits or meal reminders (eating-disorder history)')
        if sc.get('supervised'):
            lim.append('calorie target only from their clinician (%s)' % ', '.join(SUPERVISED[k] for k in sc['supervised'] if k in SUPERVISED))
        if sc.get('pregnant') == 'yes':
            lim.append('no deficit while pregnant or breastfeeding')
        if (s.get('caution') or {}).get('why'):
            lim.append('calorie reminders paused: ' + s['caution']['why'])
        return lim

    # ---------- questionnaire ----------
    def questionnaire(self):
        self.need_on()
        return {'questions': QUESTIONS, 'current': self.state().get('answers') or {},
                'how': 'Ask one or two questions at a time in plain words; accept "skip". Never pressure them about the screening questions. '
                       'Then send everything with health_coach_answers. Weight and height go through health_nutrition_measure, birth date and the '
                       'equation\'s sex input through health_nutrition_profile, if they are not on file.'}

    def answers(self, a):
        self.need_on()
        goals = a.get('goals') or []
        if not isinstance(goals, list) or not goals or any(g not in GOALS for g in goals):
            raise ValueError('goals is a list from: ' + ', '.join(GOALS))
        ed = str(a.get('eating_disorder') or 'skip').lower()
        if ed not in ('yes', 'no', 'skip'):
            raise ValueError('eating_disorder is yes, no or skip.')
        sup = a.get('supervised') or []
        if not isinstance(sup, list) or any(x not in SUPERVISED for x in sup):
            raise ValueError('supervised is a list from: ' + ', '.join(SUPERVISED) + ' (empty for none).')
        preg = str(a.get('pregnant') or 'skip').lower()
        if preg not in ('yes', 'no', 'skip'):
            raise ValueError('pregnant is yes, no or skip.')
        pace = a.get('pace') or 'standard'
        if pace not in PACES:
            raise ValueError('pace is gentle, standard or faster.')
        target = None
        if a.get('target_weight') not in (None, ''):
            try:
                target = round(to_kg(float(a['target_weight']), str(a.get('target_weight_unit') or 'kg')), 2)
            except (TypeError, ValueError):
                raise ValueError('target_weight is a number with target_weight_unit (kg or lb).')
        if a.get('activity_level'):
            self.n.set_profile({'activity': a['activity_level']})
        if preg in ('yes', 'no'):
            try:
                self.n.set_profile({'pregnant': preg == 'yes'})
            except ValueError:
                pass
        reminders = {}
        for g in goals:
            for k in DEFAULT_PROMPTS.get(g, []):
                reminders[k] = PROMPTS[k][0]
        custom = a.get('reminders') or {}
        if not isinstance(custom, dict) or any(k not in PROMPTS for k in custom):
            raise ValueError('reminders maps a reminder (%s) to HH:MM, or to "off".' % ', '.join(PROMPTS))
        for k, v in custom.items():
            if str(v).lower() == 'off':
                reminders.pop(k, None)
            else:
                reminders[k] = hhmm(v)
        screening = {'eating_disorder': ed, 'supervised': sup, 'pregnant': preg}
        if ed == 'yes':
            for k in CALORIE_PROMPTS:
                reminders.pop(k, None)
        note = clean(a.get('note') or '', 'note', 300, required=False)
        no_secrets(note, 'coach note')
        s = self.save(answers={'goals': goals, 'pace': pace, 'target_weight_kg': target, 'note': note, 'at': self.h.now()}, goals=goals, screening=screening, prompts=reminders)
        out = {'saved': True, 'goals': [GOALS[g] for g in goals], 'reminders': reminders, 'limits': self.limits(s)}
        if ed == 'yes':
            out['say'] = ('Thank them for telling you. Say the coach will not set calorie targets or count deficits, and will focus on activity, sleep and '
                          'stress; suggest working with their clinician or a registered dietitian, and that the National Alliance for Eating Disorders '
                          'offers free support. Do not ask follow-up questions about it.')
        elif 'weight_loss' in goals:
            out['next'] = 'Make the calorie plan: health_coach_plan (it needs a recent weight, height, birth date and the equation\'s sex input).'
        return out

    # ---------- weight-loss plan ----------
    def plan(self, a):
        self.need_on()
        s = self.state()
        sc = s.get('screening') or {}
        if 'weight_loss' not in (s.get('goals') or []):
            return {'no_plan': 'not_a_goal', 'why': 'Weight loss is not one of their coach goals. Add it with the questionnaire if the owner wants a plan.'}
        if sc.get('eating_disorder') == 'yes' or sc.get('supervised'):
            return plan_for(25, 'male', 2000, 1500, 80, 'standard', sc)   # the screening answer decides before any number is computed
        est = self.n.estimate_now()
        if sc.get('pregnant') == 'yes':
            return {'no_plan': 'pregnant', 'why': 'No calorie deficit while pregnant or breastfeeding; a clinician can advise.'}
        if not est.get('estimate'):
            return {'no_plan': 'missing_inputs', 'why': est.get('why'), 'missing': est.get('missing')}
        p = self.n.profile()
        w, ht = self.n.latest('weight'), self.n.latest('height')
        e = est['estimate']
        pace = (a.get('pace') or (s.get('answers') or {}).get('pace') or 'standard')
        out = plan_for(est['bmi']['value'], p.get('sex_for_equation'), e['maintenance_kcal'], e['resting_kcal'], w['value_si'], pace, sc,
                       (s.get('answers') or {}).get('target_weight_kg'), ht['value_si'])
        out |= {'bmi': est['bmi'], 'spec': '%s + %s (%s)' % (SPEC['id'], NUTR_SPEC['id'], SPEC['review']),
                'say': 'Present it as an ESTIMATE from published guidance, not a prescription. Offer to set it as their calorie goal: it only becomes '
                       'their goal when the owner confirms with /healthgoal.'}
        if out.get('target_kcal') and a.get('propose'):
            g = self.n.propose_goal({'kcal': out['target_kcal'], 'basis': 'estimate', 'note': 'Coach plan (%s): maintenance %d − %d' % (out['pace'], out['maintenance_kcal'], out['deficit_kcal'])})
            out['goal_proposal'] = g
        return out

    # ---------- energy balance ----------
    def stats(self, day):
        with self.h.db() as db:
            rows = db.execute('SELECT type,value,unit,updated FROM daily_stats WHERE date=? ORDER BY updated DESC', (day,)).fetchall() \
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_stats'").fetchone() else []
        out = {}
        for r in rows:
            if r['type'] not in out:
                v = r['value'] / 4.184 if r['unit'] == 'kJ' else r['value']
                out[r['type']] = v
        return out

    def balance_day(self, day, est):
        d = self.n.day({'date': day})
        intake = d['totals']['confirmed']['energy_kcal']
        st = self.stats(day)
        e = (est or {}).get('estimate') or {}
        if 'active_energy' in st:
            basal = st.get('basal_energy') or e.get('resting_kcal')
            burned = (basal or 0) + st['active_energy'] if basal else None
            how = ('Apple Health resting %d + active %d' % (basal, st['active_energy'])) if 'basal_energy' in st else \
                  ('estimated resting %s + Apple Health active %d' % (e.get('resting_kcal'), st['active_energy']))
            mode = 'device'
        else:
            burned, how, mode = e.get('maintenance_kcal'), 'maintenance estimate (activity level)', 'estimate'
        out = {'date': day, 'eaten_kcal': round(intake), 'burned_kcal': round(burned) if burned else None, 'burned_from': how, 'mode': mode,
               'complete': d['complete'], 'unknown_items': d.get('unknown_counts', {}).get('energy_kcal', 0)}
        if burned:
            out['deficit_kcal'] = round(burned - intake)
        return out

    def balance(self, a):
        self.need_on()
        sc = self.state().get('screening') or {}
        if sc.get('eating_disorder') == 'yes':
            return {'balance': None, 'why': 'Calorie deficits are not tracked (their answer in the questionnaire). Activity, sleep and stress are.'}
        days = min(max(int(a.get('days') or 1), 1), 60)
        end = dt.date.fromisoformat(clean(a.get('to') or self.n.today(), 'to', 10))
        est = self.n.estimate_now()
        rows = [self.balance_day((end - dt.timedelta(days=i)).isoformat(), est) for i in range(days)][::-1]
        full = [r for r in rows if r['complete'] and r.get('deficit_kcal') is not None]
        out = {'days': rows if days <= 14 else rows[-14:], 'equation': 'deficit = calories burned − calories eaten (positive = deficit)',
               'goal': self.n.active_goal(), 'spec': SPEC['id'] + ' (' + SPEC['review'] + ')',
               'notes': ['Only days the owner marked complete count toward totals; a partial day understates what the owner ate.',
                         'Wearable active-calorie numbers are estimates and often run high; the weight trend is the check.',
                         'Apple Health "active" is added to RESTING energy, never to the activity-level maintenance (that would count exercise twice).']}
        if full:
            tot = sum(r['deficit_kcal'] for r in full)
            out['summary'] = {'complete_days': len(full), 'total_deficit_kcal': tot, 'average_daily_deficit_kcal': round(tot / len(full)),
                              'rough_change_kg': round(-tot / KCAL_PER_KG, 2), 'rough_change_lb': round(-tot / KCAL_PER_KG / LB, 1),
                              'rule': '%d kcal ≈ 1 kg (rough; real change is usually smaller over time)' % KCAL_PER_KG}
        obs = self.observed_maintenance(end)
        if obs:
            out['observed_maintenance'] = obs
        self.check_caution()
        caution = (self.state().get('caution') or {}).get('why')
        if caution:
            out['caution'] = caution
            out['say'] = 'Mention the caution plainly and kindly; do not encourage a bigger deficit; suggest checking in with their clinician.'
        return out

    def observed_maintenance(self, end, days=28):
        """Energy balance from their own data: maintenance ≈ average intake + (weight lost × 7,700) / days. Needs >= 14 days, 10 complete days, 2 weights."""
        start = end - dt.timedelta(days=days - 1)
        t = self.n.trends({'days': days, 'to': end.isoformat()})
        w = t.get('weight')
        if not w or t.get('days_complete', 0) < 10 or not t.get('average_kcal_complete_days'):
            return None
        span = (dt.date.fromisoformat(w['last'].split(' on ')[1]) - dt.date.fromisoformat(w['first'].split(' on ')[1])).days
        if span < 14:
            return None
        m = t['average_kcal_complete_days'] - w['change_kg'] * KCAL_PER_KG / span
        est = (self.n.estimate_now().get('estimate') or {}).get('maintenance_kcal')
        out = {'kcal': int(round(m / 10.0) * 10), 'from': '%s to %s: average %d kcal eaten on %d complete days, weight change %+.1f kg over %d days' % (
            start.isoformat(), end.isoformat(), t['average_kcal_complete_days'], t['days_complete'], w['change_kg'], span),
            'estimate_kcal': est}
        if est and abs(m - est) / est > 0.10:
            out['note'] = 'This differs from the equation estimate by more than 10%; their own trend is usually the better guide. A new plan can use it.'
        return out

    # ---------- metrics ----------
    def metric(self, a):
        self.need_on()
        kind = a.get('kind')
        if kind == 'weight':
            return self.n.add_measurement({'kind': 'weight', 'value': a.get('value'), 'unit': a.get('unit') or 'kg', 'measured_at': a.get('date')})
        if kind not in METRICS:
            raise ValueError('kind is weight or one of: ' + ', '.join(METRICS))
        unit, lo, hi = METRICS[kind]
        value2 = None
        if kind == 'blood_pressure':
            m = re.fullmatch(r'\s*(\d{2,3})\s*/\s*(\d{2,3})\s*', str(a.get('value') or ''))
            if not m:
                raise ValueError('Blood pressure is "SYS/DIA", e.g. "124/78".')
            value, value2 = float(m.group(1)), float(m.group(2))
            if not (lo <= value <= hi and 30 <= value2 <= 160 and value > value2):
                raise ValueError('That reading looks off; check it and send it again as SYS/DIA.')
        else:
            try:
                value = float(a.get('value'))
            except (TypeError, ValueError):
                raise ValueError('value is a number.')
            if kind == 'waist' and str(a.get('unit') or '').lower() in ('in', 'inch', 'inches'):
                value = value * 2.54
            if kind == 'water' and str(a.get('unit') or '').lower() in ('cup', 'cups'):
                value = value * 240
            if kind == 'water' and str(a.get('unit') or '').lower() in ('oz', 'fl oz', 'ounces'):
                value = value * 29.5735
            if not lo <= value <= hi:
                raise ValueError('%s must be between %s and %s %s.' % (kind, lo, hi, unit))
        detail = {}
        if kind == 'exercise_minutes':
            inten = str(a.get('intensity') or 'moderate').lower()
            if inten not in ('light', 'moderate', 'vigorous'):
                raise ValueError('intensity is light, moderate or vigorous.')
            detail = {'intensity': inten, 'activity': clean(a.get('activity') or '', 'activity', 60, required=False)}
        if kind == 'meditation_minutes' or kind == 'exercise_minutes':
            no_secrets(json.dumps(detail), 'activity')
        now = self.now_local()
        day = clean(a.get('date') or now.date().isoformat(), 'date', 10)
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or day > now.date().isoformat():
            raise ValueError('date is today or earlier (YYYY-MM-DD).')
        mid = secrets.token_hex(8)
        with self.h.db() as db:
            db.execute('INSERT INTO coach_metrics VALUES(?,?,?,?,?,?,?,?,?,0,?)', (mid, kind, value, value2, unit, day, now.strftime('%H:%M'), json.dumps(detail), 'manual', self.h.now()))
        out = {'saved': mid, 'kind': kind, 'value': '%d/%d' % (value, value2) if kind == 'blood_pressure' else round(value, 1), 'unit': unit, 'date': day}
        if kind == 'blood_pressure':
            cat = bp_category(value, value2)
            out['category'] = cat + ' (2017 ACC/AHA categories; one reading is not a diagnosis — the average of several matters)'
            if cat.startswith('severe'):
                out['urgent'] = BP_URGENT
                out['say'] = 'Give them the urgent text word for word first. Do not interpret further.'
        if kind == 'stress' and value >= 4:
            out['offer'] = BREATHING
            out['say'] = 'Offer the breathing exercise kindly, in a sentence or two; ask if the owner wants a reminder later. No diagnosis or advice beyond that.'
        if kind == 'sleep_hours' and value < 7:
            out['note'] = 'Adults are advised 7 or more hours (AASM/SRS).'
        return out

    def metrics(self, a):
        self.need_on()
        days = min(max(int(a.get('days') or 14), 1), 180)
        end = self.now_local().date()
        start = (end - dt.timedelta(days=days - 1)).isoformat()
        with self.h.db() as db:
            rows = [dict(r) for r in db.execute('SELECT * FROM coach_metrics WHERE deleted=0 AND local_date>=? ORDER BY local_date,local_time', (start,))]
        out = {'from': start, 'to': end.isoformat(), 'metrics': {}}
        for kind in METRICS:
            rs = [r for r in rows if r['kind'] == kind]
            if not rs:
                continue
            if kind == 'blood_pressure':
                s_avg = sum(r['value'] for r in rs) / len(rs)
                d_avg = sum(r['value2'] for r in rs) / len(rs)
                out['metrics'][kind] = {'readings': len(rs), 'average': '%d/%d' % (round(s_avg), round(d_avg)), 'category_of_average': bp_category(s_avg, d_avg),
                                        'latest': '%d/%d on %s' % (rs[-1]['value'], rs[-1]['value2'], rs[-1]['local_date'])}
            else:
                vals = [r['value'] for r in rs]
                total_kinds = ('exercise_minutes', 'meditation_minutes', 'water')
                out['metrics'][kind] = {'entries': len(rs), 'latest': '%s %s on %s' % (round(vals[-1], 1), METRICS[kind][0], rs[-1]['local_date']),
                                        ('total' if kind in total_kinds else 'average'): round(sum(vals) if kind in total_kinds else sum(vals) / len(vals), 1)}
        out['activity_week'] = self.activity_week(end)
        return out

    def activity_week(self, end):
        """Moderate-equivalent minutes over the last 7 days (vigorous counts double) vs 150 (PAG 2018). Apple exercise minutes count as moderate."""
        start = (end - dt.timedelta(days=6)).isoformat()
        with self.h.db() as db:
            manual = db.execute("SELECT value,detail FROM coach_metrics WHERE kind='exercise_minutes' AND deleted=0 AND local_date>=?", (start,)).fetchall()
            has_stats = db.execute("SELECT 1 FROM sqlite_master WHERE name='daily_stats'").fetchone()
            apple = db.execute("SELECT date,MAX(value) AS v FROM daily_stats WHERE type='exercise_minutes' AND date>=? GROUP BY date", (start,)).fetchall() if has_stats else []
        mins = 0.0
        for r in manual:
            inten = json.loads(r['detail'] or '{}').get('intensity', 'moderate')
            mins += r['value'] * (2 if inten == 'vigorous' else 1 if inten == 'moderate' else 0)
        apple_total = sum(r['v'] for r in apple)
        # Apple counts the same workouts the owner may also log by hand: use whichever is larger rather than adding them.
        total = max(mins, apple_total)
        return {'moderate_equivalent_minutes': round(total), 'goal_minutes': 150, 'met': total >= 150,
                'source': 'Apple Health exercise minutes' if apple_total > mins else 'their logged exercise', 'basis': 'Physical Activity Guidelines for Americans, 2nd ed. (2018)'}

    # ---------- safety ----------
    def check_caution(self):
        """Sets or clears the caution flag from the last 7 complete days and the weight trend."""
        s = self.state()
        if not s.get('enabled'):
            return None
        p = self.n.profile()
        est = (self.n.estimate_now().get('estimate') or {})
        floor = max(FLOOR.get(p.get('sex_for_equation'), 1200), est.get('resting_kcal') or 0)
        end = dt.date.fromisoformat(self.n.today())
        low = 0
        for i in range(7):
            d = (end - dt.timedelta(days=i)).isoformat()
            day = self.n.day({'date': d})
            if day['complete'] and day['entries'] and day['totals']['confirmed']['energy_kcal'] < floor:
                low += 1
        why = None
        if low >= 3:
            why = 'logged intake was below the coach\'s minimum (%d kcal) on %d of the last 7 complete days' % (floor, low)
        t = self.n.trends({'days': 21, 'to': end.isoformat()})
        w = t.get('weight')
        if not why and w and w['measurements'] >= 3 and w['per_week_kg'] < 0:
            latest = self.n.latest('weight')
            if latest and -w['per_week_kg'] > 0.01 * latest['value_si']:
                why = 'weight has been falling faster than 1% a week over the last 3 weeks'
        cur = (s.get('caution') or {}).get('why')
        if why != cur:
            self.save(caution={'why': why, 'since': self.h.now()} if why else {}, caution_told=None if why else s.get('caution_told'))
        return why

    # ---------- reminders ----------
    def due(self):
        """Called every few minutes by the plugin timer (owner socket). Returns the fixed reminder texts to send now and records them."""
        s = self.state()
        if not s.get('enabled') or not s.get('answers'):
            return {'send': []}
        now = self.now_local()
        if s.get('paused_until') and self.h.now() < s['paused_until']:
            return {'send': []}
        hm, today = now.strftime('%H:%M'), now.date().isoformat()
        out = []
        caution = self.check_caution()
        s = self.state()
        if caution and not s.get('caution_told') and not (QUIET[0] <= hm or hm < QUIET[1]):
            out.append({'kind': 'caution', 'text': 'Coach: I have paused calorie reminders because %s. Please check in with your clinician before continuing a calorie deficit. '
                                                   'Send /coach resume when you are ready.' % caution})
            self.save(caution_told=self.h.now())
        if QUIET[0] <= hm or hm < QUIET[1]:
            return {'send': out}
        with self.h.db() as db:
            sent_today = db.execute('SELECT COUNT(*) FROM coach_sent WHERE local_date=?', (today,)).fetchone()[0]
            already = {r['kind'] for r in db.execute('SELECT kind FROM coach_sent WHERE local_date=?', (today,))}
        for kind, at in sorted((s.get('prompts') or {}).items(), key=lambda x: x[1]):
            if kind not in PROMPTS or kind in already or sent_today >= MAX_PER_DAY:
                continue
            days = PROMPTS[kind][1]
            if days is not None and now.weekday() not in days:
                continue
            if not (at <= hm < (dt.datetime.strptime(at, '%H:%M') + dt.timedelta(minutes=45)).strftime('%H:%M')):
                continue   # a 45-minute window: a restart or a slow timer still sends it, a late start does not send it hours later
            if kind in CALORIE_PROMPTS and (caution or (s.get('screening') or {}).get('eating_disorder') == 'yes'):
                continue
            if kind == 'meals':
                try:
                    if self.n.day({'date': today})['complete']:
                        continue
                except Exception:
                    pass
            if kind == 'exercise' and self.activity_today(today) >= 30:
                continue
            out.append({'kind': kind, 'text': PROMPTS[kind][2]})
            with self.h.db() as db:
                db.execute('INSERT OR IGNORE INTO coach_sent VALUES(?,?,?)', (kind, today, self.h.now()))
            sent_today += 1
        return {'send': out}

    def activity_today(self, today):
        st = self.stats(today)
        with self.h.db() as db:
            m = db.execute("SELECT COALESCE(SUM(value),0) FROM coach_metrics WHERE kind='exercise_minutes' AND deleted=0 AND local_date=?", (today,)).fetchone()[0]
        return max(st.get('exercise_minutes') or 0, m or 0)

    # ---------- owner settings ----------
    def settings(self, a):
        what = a.get('set')
        if what == 'on':
            s = self.save(enabled=1, enabled_at=self.h.now())
            return {'enabled': True, 'questionnaire_done': bool(s.get('answers'))}
        if what == 'off':
            self.save(enabled=0)
            return {'enabled': False}
        if what == 'pause':
            days = int(a.get('days') or 1)
            if not 1 <= days <= 60:
                raise ValueError('Pause for 1-60 days.')
            self.save(paused_until=self.h.now() + days * 86400)
            return {'paused_days': days}
        if what == 'resume':
            self.save(paused_until=None, caution={}, caution_told=None)
            return {'resumed': True, 'note': 'Calorie reminders resume; the caution check keeps running.'}
        if what == 'reminder':
            kind = a.get('kind')
            if kind not in PROMPTS:
                raise ValueError('Reminders: ' + ', '.join(PROMPTS))
            p = dict(self.state().get('prompts') or {})
            if str(a.get('time')).lower() == 'off':
                p.pop(kind, None)
            else:
                if kind in CALORIE_PROMPTS and (self.state().get('screening') or {}).get('eating_disorder') == 'yes':
                    raise ValueError('Meal-count reminders stay off with their questionnaire answer; the owner can change that answer by retaking the questionnaire.')
                p[kind] = hhmm(a.get('time'))
            self.save(prompts=p)
            return {'reminders': p}
        if what == 'forget':
            with self.h.db() as db:
                n = db.execute('DELETE FROM coach_metrics').rowcount
                db.execute('DELETE FROM coach_sent')
                db.execute('DELETE FROM coach')
            return {'deleted_metrics': n, 'coach': 'reset and off'}
        return self.status()

    # ---------- dispatch ----------
    def call(self, action, a, privileged=False):
        if action == 'health_coach_settings':
            if not privileged:
                raise ValueError('Only the owner turns the coach on or off or changes its reminders (/coach).')
            return self.settings(a)
        if action == 'health_coach_due':
            if not privileged:
                raise ValueError('Reminders are sent by the coach timer only.')
            return self.due()
        if action == 'health_coach_status':
            return self.status()
        if action == 'health_coach_questionnaire':
            return self.questionnaire()
        if action == 'health_coach_answers':
            return self.answers(a)
        if action == 'health_coach_plan':
            return self.plan(a)
        if action == 'health_coach_balance':
            return self.balance(a)
        if action == 'health_coach_metric':
            return self.metric(a)
        if action == 'health_coach_metrics':
            return self.metrics(a)
        raise ValueError('Unknown coach action.')
