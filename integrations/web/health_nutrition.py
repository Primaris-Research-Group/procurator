"""Nutrition and calorie tracking v1 (web 0.23.0; build-plans/health/06-nutrition-tracking.md).

What it does: an editable food log (manual, custom labeled foods, USDA FoodData Central), daily totals with honest completeness,
measurements (weight/height; shared with the Apple Health import), BMI, an ESTIMATED maintenance energy need, user-confirmed calorie
goals, and simple trends. The conversational model collects and explains inputs; every number comes from this file.

Calculation specification NUTR-SPEC-0.1 — PENDING PHYSICIAN REVIEW. Until a reviewer signs it off (SPEC['review']), every estimate
says so. The method:
  * Resting energy: Mifflin–St Jeor (1990).  REE = 10·kg + 6.25·cm − 5·age + s,  s = +5 (male) / −161 (female).
    Requires the equation's sex input (male/female) — asked for as such, never inferred, and stored apart from gender identity.
  * Total daily energy (maintenance) = REE × one activity factor chosen by the user from the definitions below. The equation itself
    contains no activity term, so the factor is applied exactly once. Wearable/exercise calories are NOT added on top.
  * Population: adults 18+, not pregnant, not breastfeeding. Outside it, no estimate is made; logging and a clinician-supplied target
    still work.
  * Rounding: REE and maintenance reported to the nearest 10 kcal; the unrounded values are kept for tests.
  * Uncertainty: equation-based estimates commonly differ from an individual's measured needs by 10% or more.
  * BMI = kg / m², shown with its WHO adult category; BMI alone never sets a target.
  * Inputs: the latest weight within 30 days and the latest height (any age if the person is an adult) — older weights are "stale".
  * FoodData Central key: FDC_API_KEY in the broker environment, or health-tokens/fdc-api-key (bin/set-health-token.sh --fdc).
  * Goals: always confirmed by the owner (/healthgoal ID HASH). A goal below 1,200 kcal or below the estimated resting energy is only
    accepted as a clinician-supplied target. No weight-change planner and no date-specific predictions in v1.
"""
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from health import canonical, clean, no_secrets, payload_hash

SPEC = {'id': 'NUTR-SPEC-0.1', 'review': 'pending physician review',
        'ree': 'Mifflin-St Jeor 1990: 10*kg + 6.25*cm - 5*age + (5 male | -161 female)',
        'population': 'adults 18+, not pregnant, not breastfeeding',
        'uncertainty': 'An equation estimate; individual needs commonly differ by 10% or more. Weight trend over several weeks is the better guide.'}
ACTIVITY = {  # factor, definition (what the user chooses from)
    'sedentary': (1.2, 'mostly sitting; little or no exercise'),
    'light': (1.375, 'light exercise or sports 1–3 days a week'),
    'moderate': (1.55, 'moderate exercise or sports 3–5 days a week'),
    'very': (1.725, 'hard exercise or sports 6–7 days a week'),
    'extra': (1.9, 'very hard exercise or a physical job plus training'),
}
LB = 0.45359237
OZ = 28.349523125
IN = 2.54
KJ = 4.184
MASS_G = {'g': 1.0, 'gram': 1.0, 'grams': 1.0, 'kg': 1000.0, 'oz': OZ, 'ounce': OZ, 'ounces': OZ, 'lb': LB * 1000, 'lbs': LB * 1000, 'pound': LB * 1000, 'pounds': LB * 1000}
AMBIGUOUS = re.compile(r'(?i)^(bowl|plate|handful|piece|portion|some|a little|lots?|helping|scoop|slice|serving)s?$')
NUTRIENTS = {  # our key: (FDC nutrient numbers by preference, unit)
    'energy_kcal': ((1008, 2048, 2047), 'kcal'),
    'protein_g': ((1003,), 'g'), 'fat_g': ((1004,), 'g'), 'carbs_g': ((1005,), 'g'),
    'fiber_g': ((1079,), 'g'), 'sugars_g': ((2000,), 'g'), 'sodium_mg': ((1093,), 'mg'),
}
NKEYS = tuple(NUTRIENTS)
FDC_BASE = 'https://api.nal.usda.gov/fdc/v1/'
CACHE_DAYS = 30
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TIME = re.compile(r'^\d{2}:\d{2}$')


class NutritionError(Exception):
    def __init__(self, kind, detail=''):
        super().__init__(kind + (': ' + detail if detail else ''))
        self.kind, self.detail = kind, detail


def r10(x):
    return int(round(x / 10.0) * 10)


def age_on(birth, on):
    b, d = dt.date.fromisoformat(birth), dt.date.fromisoformat(on)
    return d.year - b.year - ((d.month, d.day) < (b.month, b.day))


def to_kg(value, unit):
    u = str(unit or '').lower()
    if u in ('kg', 'kgs', 'kilogram', 'kilograms'):
        return float(value)
    if u in ('lb', 'lbs', 'pound', 'pounds', '[lb_av]'):
        return float(value) * LB
    if u in ('g', 'gram', 'grams'):
        return float(value) / 1000
    raise ValueError('weight unit is kg or lb')


def to_cm(value, unit):
    u = str(unit or '').lower()
    if u in ('cm', 'centimeter', 'centimeters'):
        return float(value)
    if u in ('m', 'meter', 'meters'):
        return float(value) * 100
    if u in ('in', 'inch', 'inches', '[in_i]'):
        return float(value) * IN
    if u in ('ft', 'feet'):
        return float(value) * 12 * IN
    raise ValueError('height unit is cm, m, in or ft')


def bmi_category(b):
    return 'underweight' if b < 18.5 else 'healthy range' if b < 25 else 'overweight' if b < 30 else 'obesity'


def estimate(sex, age, kg, cm, activity):
    """The NUTR-SPEC-0.1 numbers (unrounded and rounded). Pure; the reference vectors test this."""
    if sex not in ('male', 'female'):
        raise ValueError('The equation needs its sex input: male or female.')
    if activity not in ACTIVITY:
        raise ValueError('activity is one of ' + ', '.join(ACTIVITY))
    ree = 10 * kg + 6.25 * cm - 5 * age + (5 if sex == 'male' else -161)
    tdee = ree * ACTIVITY[activity][0]
    return {'ree': ree, 'maintenance': tdee, 'ree_kcal': r10(ree), 'maintenance_kcal': r10(tdee), 'maintenance_kj': r10(tdee * KJ),
            'range_kcal': [r10(tdee * 0.9), r10(tdee * 1.1)]}


class FDC:
    """USDA FoodData Central adapter: search, details, cache, rate limits. The key stays server-side (FDC_API_KEY)."""
    def __init__(self, nutrition, key=None, fetch=None, timeout=15):
        self.n, self.timeout = nutrition, timeout
        self.key = key if key is not None else os.environ.get('FDC_API_KEY', '')
        self.fetch = fetch or self._fetch

    def _fetch(self, path, params):
        url = FDC_BASE + path + '?' + urllib.parse.urlencode({**params, 'api_key': self.key})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'Accept': 'application/json'}), timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:2000]
        except (urllib.error.URLError, OSError) as e:
            raise NutritionError('unavailable', 'food search is unreachable (%s)' % type(e).__name__)

    def get(self, path, params):
        if not self.key:
            raise NutritionError('not_configured', 'FoodData Central has no API key on this server (FDC_API_KEY)')
        st, body = self.fetch(path, params)
        if st == 429:
            raise NutritionError('rate_limited', 'FoodData Central quota reached; try later')
        if st in (401, 403):
            raise NutritionError('not_configured', 'FoodData Central refused the API key')
        if st == 404:
            raise NutritionError('not_found', 'no such food')
        if st != 200:
            raise NutritionError('unavailable', 'FoodData Central answered HTTP %s' % st)
        try:
            return json.loads(body)
        except ValueError:
            raise NutritionError('corrupt', 'FoodData Central returned an unreadable answer')

    @staticmethod
    def per100(food):
        """Nutrients per 100 g from a food-details or search item; missing ones stay None (unknown), never 0."""
        out = {k: None for k in NKEYS}
        found = {}
        for fn in food.get('foodNutrients') or []:
            if not isinstance(fn, dict):
                continue
            num = fn.get('nutrientNumber') or (fn.get('nutrient') or {}).get('number')
            nid = fn.get('nutrientId') or (fn.get('nutrient') or {}).get('id')
            amt = fn.get('value', fn.get('amount'))
            unit = (fn.get('unitName') or (fn.get('nutrient') or {}).get('unitName') or '').lower()
            if amt is None:
                continue
            try:
                amt = float(amt)
            except (TypeError, ValueError):
                continue
            for k in (nid, num):
                try:
                    found.setdefault(int(float(k)), (amt, unit))
                except (TypeError, ValueError):
                    pass
        # nutrientNumber "208" = kcal energy; map common numbers to ids too
        alias = {208: 1008, 203: 1003, 204: 1004, 205: 1005, 291: 1079, 269: 2000, 307: 1093, 957: 2047, 958: 2048}
        for num, nid in alias.items():
            if num in found and nid not in found:
                found[nid] = found[num]
        for k, (ids, unit) in NUTRIENTS.items():
            for i in ids:
                if i in found:
                    amt, u = found[i]
                    if k == 'energy_kcal' and u == 'kj':
                        amt = amt / KJ
                    if k == 'sodium_mg' and u == 'g':
                        amt *= 1000
                    out[k] = round(amt, 3)
                    break
        if out['energy_kcal'] is None:
            for fn in food.get('foodNutrients') or []:      # only a kJ energy row (nutrient 1062)
                if isinstance(fn, dict) and int(float(fn.get('nutrientId') or (fn.get('nutrient') or {}).get('id') or 0)) == 1062 and fn.get('value', fn.get('amount')) is not None:
                    out['energy_kcal'] = round(float(fn.get('value', fn.get('amount'))) / KJ, 3)
        return out

    @staticmethod
    def portions(food):
        out = []
        for p in food.get('foodPortions') or []:
            try:
                g = float(p.get('gramWeight'))
            except (TypeError, ValueError):
                continue
            name = ' '.join(str(x) for x in (p.get('amount') or '', ((p.get('measureUnit') or {}).get('name') if (p.get('measureUnit') or {}).get('name') not in (None, 'undetermined') else ''),
                                            p.get('modifier') or p.get('portionDescription') or '') if x).strip()
            if g > 0 and name:
                out.append({'portion': name[:80], 'grams': round(g, 2)})
        if food.get('servingSize') and str(food.get('servingSizeUnit', '')).lower() in ('g', 'grm', 'gram'):
            out.insert(0, {'portion': 'serving (label: %s)' % (food.get('householdServingFullText') or '%s g' % food['servingSize']), 'grams': float(food['servingSize'])})
        return out[:12]

    def normalize(self, food):
        if not isinstance(food, dict) or not food.get('fdcId'):
            raise NutritionError('corrupt', 'a food without an fdcId')
        return {'fdc_id': int(food['fdcId']), 'description': str(food.get('description') or '')[:200], 'brand': (food.get('brandOwner') or food.get('brandName') or None),
                'data_type': food.get('dataType'), 'published': food.get('publicationDate') or food.get('modifiedDate'),
                'per_100g': self.per100(food), 'portions': self.portions(food)}

    def search(self, query, page_size=8, data_types=None):
        q = clean(query, 'query', 120)
        try:
            body = self.get('foods/search', {'query': q, 'pageSize': min(max(int(page_size or 8), 1), 25),
                                              'dataType': ','.join(data_types or ['Foundation', 'SR Legacy', 'Branded', 'Survey (FNDDS)'])})
        except NutritionError as e:
            cached = self.n.cache_search(q)
            if cached:
                return {'foods': cached, 'source': 'cache (FoodData Central unavailable: %s)' % e.kind, 'complete': False}
            raise
        if not isinstance(body, dict) or not isinstance(body.get('foods'), list):
            raise NutritionError('corrupt', 'search answer without a food list')
        foods = []
        for f in body['foods'][:25]:
            try:
                nf = self.normalize(f)
            except NutritionError:
                continue
            foods.append(nf)
            self.n.cache_put(nf, partial=True)
        return {'foods': [{k: f[k] for k in ('fdc_id', 'description', 'brand', 'data_type', 'per_100g')} for f in foods], 'source': 'FoodData Central', 'complete': True}

    def details(self, fdc_id):
        fid = int(fdc_id)
        hit = self.n.cache_get(fid)
        if hit and not hit['partial'] and hit['age_days'] < CACHE_DAYS:
            return hit['food'] | {'source': 'cache', 'cache_age_days': round(hit['age_days'], 1)}
        try:
            nf = self.normalize(self.get('food/%d' % fid, {}))
        except NutritionError as e:
            if hit:
                return hit['food'] | {'source': 'cache (stale; FoodData Central %s)' % e.kind, 'cache_age_days': round(hit['age_days'], 1)}
            raise
        self.n.cache_put(nf, partial=False)
        return nf | {'source': 'FoodData Central'}


class Nutrition:
    def __init__(self, health, fdc=None, tz='America/New_York'):
        self.h, self.tz = health, tz
        self.fdc = fdc or FDC(self)
        self.fdc.n = self
        if self.fdc.key is None or (self.fdc.key == '' and fdc is None):
            try:      # server-side key file (bin/set-health-token.sh --fdc); never in chat or tool results
                self.fdc.key = (self.h.tokens / 'fdc-api-key').read_text().strip()
            except OSError:
                self.fdc.key = ''
        with self.h.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS nutrition_profile(id INTEGER PRIMARY KEY CHECK(id=1),birth_date TEXT,sex_for_equation TEXT,gender_identity TEXT,
                    activity TEXT,pregnant INTEGER,breastfeeding INTEGER,updated REAL);
                CREATE TABLE IF NOT EXISTS measurements(id TEXT PRIMARY KEY,kind TEXT,value_si REAL,value REAL,unit TEXT,measured_at TEXT,source TEXT,
                    source_id TEXT UNIQUE,device TEXT,deleted INTEGER DEFAULT 0,created REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS food_cache(fdc_id INTEGER PRIMARY KEY,body TEXT,partial INTEGER,fetched REAL);
                CREATE TABLE IF NOT EXISTS custom_foods(id TEXT PRIMARY KEY,name TEXT,brand TEXT,serving_g REAL,serving_text TEXT,per_serving TEXT,basis TEXT,created REAL);
                CREATE TABLE IF NOT EXISTS food_log(id TEXT PRIMARY KEY,entry_key TEXT UNIQUE,payload_hash TEXT,version INTEGER,local_date TEXT,local_time TEXT,tz TEXT,
                    food_ref TEXT,description TEXT,brand TEXT,preparation TEXT,quantity REAL,unit TEXT,grams REAL,nutrients TEXT,basis TEXT,status TEXT,
                    needs TEXT,source TEXT,deleted INTEGER DEFAULT 0,created REAL,updated REAL);
                CREATE TABLE IF NOT EXISTS food_log_history(entry_id TEXT,version INTEGER,at REAL,change TEXT,before TEXT);
                CREATE TABLE IF NOT EXISTS day_status(local_date TEXT PRIMARY KEY,complete INTEGER,at REAL);
                CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,kcal INTEGER,basis TEXT,note TEXT,hash TEXT,status TEXT,created REAL,confirmed REAL,estimate TEXT);
            ''')

    # ---------- time ----------
    def today(self):
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.fromtimestamp(self.h.now(), ZoneInfo(self.tz)).date().isoformat()
        except Exception:
            return dt.datetime.utcfromtimestamp(self.h.now()).date().isoformat()

    # ---------- profile ----------
    def profile(self):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM nutrition_profile WHERE id=1').fetchone()
        return dict(r) if r else {}

    def set_profile(self, a):
        cur = self.profile()
        new = dict(cur)
        if 'birth_date' in a:
            b = clean(a['birth_date'], 'birth_date', 10)
            if not DATE.match(b):
                raise ValueError('birth_date is YYYY-MM-DD.')
            dt.date.fromisoformat(b)
            new['birth_date'] = b
        if 'sex_for_equation' in a:
            s = a['sex_for_equation']
            if s not in ('male', 'female', None, ''):
                raise ValueError('sex_for_equation is the equation input, male or female, as the owner states it (never inferred). Gender identity is a separate, optional field.')
            new['sex_for_equation'] = s or None
        if 'gender_identity' in a:
            new['gender_identity'] = clean(a.get('gender_identity') or '', 'gender_identity', 60, required=False) or None
        if 'activity' in a:
            if a['activity'] not in ACTIVITY:
                raise ValueError('activity is one of: ' + '; '.join('%s (%s)' % (k, v[1]) for k, v in ACTIVITY.items()))
            new['activity'] = a['activity']
        for k in ('pregnant', 'breastfeeding'):
            if k in a:
                if a[k] not in (True, False, None):
                    raise ValueError('%s is true, false or null (unknown).' % k)
                new[k] = None if a[k] is None else int(bool(a[k]))
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO nutrition_profile VALUES(1,?,?,?,?,?,?,?)', (new.get('birth_date'), new.get('sex_for_equation'), new.get('gender_identity'),
                       new.get('activity'), new.get('pregnant'), new.get('breastfeeding'), self.h.now()))
        return self.public_profile()

    def public_profile(self):
        p = self.profile()
        return {k: p.get(k) for k in ('birth_date', 'sex_for_equation', 'gender_identity', 'activity', 'pregnant', 'breastfeeding')} | \
               {'activity_choices': {k: v[1] for k, v in ACTIVITY.items()}}

    # ---------- measurements ----------
    def add_measurement(self, a, source='manual'):
        kind = a.get('kind')
        if kind not in ('weight', 'height'):
            raise ValueError('kind is weight or height.')
        try:
            value = float(a.get('value'))
        except (TypeError, ValueError):
            raise ValueError('value is a number.')
        unit = clean(str(a.get('unit') or ''), 'unit', 12)
        si = to_kg(value, unit) if kind == 'weight' else to_cm(value, unit)
        if (kind == 'weight' and not 20 <= si <= 400) or (kind == 'height' and not 90 <= si <= 250):
            raise ValueError('That %s (%s %s) is outside a plausible adult range; check the number and unit.' % (kind, value, unit))
        when = clean(a.get('measured_at') or self.today(), 'measured_at', 25)
        if not re.match(r'^\d{4}-\d{2}-\d{2}', when):
            raise ValueError('measured_at is a date (YYYY-MM-DD) or ISO time.')
        if when[:10] > self.today():
            raise ValueError('measured_at is in the future.')
        sid = clean(a.get('source_id') or '', 'source_id', 200, required=False) or None
        now = self.h.now()
        with self.h.db() as db:
            if sid:
                old = db.execute('SELECT id FROM measurements WHERE source_id=?', (sid,)).fetchone()
                if old:
                    db.execute('UPDATE measurements SET kind=?,value_si=?,value=?,unit=?,measured_at=?,deleted=0,updated=? WHERE id=?', (kind, si, value, unit, when, now, old['id']))
                    return {'measurement': old['id'], 'updated': True}
            mid = secrets.token_hex(8)
            db.execute('INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?,?,0,?,?)', (mid, kind, si, value, unit, when, source, sid, a.get('device'), now, now))
        return {'measurement': mid, 'kind': kind, 'value': value, 'unit': unit, 'si': round(si, 3), 'measured_at': when, 'source': source}

    def delete_measurement(self, mid=None, source_id=None):
        with self.h.db() as db:
            if source_id:
                n = db.execute('UPDATE measurements SET deleted=1,updated=? WHERE source_id=?', (self.h.now(), source_id)).rowcount
            else:
                n = db.execute('UPDATE measurements SET deleted=1,updated=? WHERE id=?', (self.h.now(), clean(mid, 'measurement', 16))).rowcount
        return {'deleted': n}

    def latest(self, kind):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM measurements WHERE kind=? AND deleted=0 ORDER BY measured_at DESC, created DESC LIMIT 1', (kind,)).fetchone()
        return dict(r) if r else None

    # ---------- estimate ----------
    def screening(self, p, on):
        missing, outside = [], []
        if not p.get('birth_date'):
            missing.append('birth date')
        if not p.get('sex_for_equation'):
            missing.append('the equation\'s sex input (male/female)')
        if not p.get('activity'):
            missing.append('activity level')
        if p.get('birth_date') and age_on(p['birth_date'], on) < 18:
            outside.append('under 18')
        if p.get('sex_for_equation') == 'female':
            if p.get('pregnant') is None or p.get('breastfeeding') is None:
                missing.append('whether currently pregnant or breastfeeding')
            if p.get('pregnant'):
                outside.append('pregnant')
            if p.get('breastfeeding'):
                outside.append('breastfeeding')
        return missing, outside

    def estimate_now(self, a=None):
        on = (a or {}).get('on') or self.today()
        p = self.profile()
        w, ht = self.latest('weight'), self.latest('height')
        out = {'spec': SPEC['id'], 'review': SPEC['review'], 'on': on}
        if w and ht:
            b = w['value_si'] / (ht['value_si'] / 100) ** 2
            out['bmi'] = {'value': round(b, 1), 'category': bmi_category(b), 'note': 'BMI describes weight for height; it does not set energy needs.'}
        missing, outside = self.screening(p, on)
        if not w:
            missing.append('a weight')
        if not ht:
            missing.append('a height')
        stale = None
        if w and (dt.date.fromisoformat(on) - dt.date.fromisoformat(w['measured_at'][:10])).days > 30:
            stale = 'the latest weight is from %s (over 30 days old)' % w['measured_at'][:10]
            missing.append('a weight from the last 30 days')
        if outside:
            return out | {'estimate': None, 'why': 'Outside the supported population (%s). Logging works, and a target from their clinician can be set (/healthgoal).' % ', '.join(outside)}
        if missing:
            return out | {'estimate': None, 'missing': missing, 'why': 'Ask the owner for: ' + '; '.join(missing) + (' (%s)' % stale if stale else '')}
        age = age_on(p['birth_date'], on)
        e = estimate(p['sex_for_equation'], age, w['value_si'], ht['value_si'], p['activity'])
        return out | {'estimate': {'resting_kcal': e['ree_kcal'], 'maintenance_kcal': e['maintenance_kcal'], 'maintenance_kj': e['maintenance_kj'],
                                   'likely_range_kcal': e['range_kcal'], 'activity': '%s (%s, ×%s)' % (p['activity'], ACTIVITY[p['activity']][1], ACTIVITY[p['activity']][0])},
                      'inputs': {'age': age, 'weight': '%s %s on %s (%s)' % (w['value'], w['unit'], w['measured_at'][:10], w['source']),
                                 'height': '%s %s on %s (%s)' % (ht['value'], ht['unit'], ht['measured_at'][:10], ht['source']), 'sex_for_equation': p['sex_for_equation']},
                      'uncertainty': SPEC['uncertainty'],
                      'say': 'Call it an ESTIMATE of maintenance needs (method %s, not yet reviewed by a physician). Do not add exercise calories on top: the activity level already counts them.' % SPEC['id']}

    # ---------- goals ----------
    def propose_goal(self, a):
        try:
            kcal = int(a.get('kcal'))
        except (TypeError, ValueError):
            raise ValueError('kcal is a whole number.')
        basis = a.get('basis') or 'own'
        if basis not in ('own', 'estimate', 'clinician'):
            raise ValueError('basis is own (their choice), estimate (the maintenance estimate) or clinician (a target their clinician gave).')
        if not 800 <= kcal <= 6000:
            raise ValueError('That target is outside 800–6,000 kcal.')
        est = self.estimate_now().get('estimate') or {}
        floor = max(1200, est.get('resting_kcal') or 0)
        if kcal < floor and basis != 'clinician':
            raise ValueError('A target below %d kcal (%s) is only set as a clinician-supplied target (basis "clinician"). Suggest the owner ask their clinician; do not '
                             'propose a lower number.' % (floor, 'their estimated resting energy' if floor > 1200 else 'the general floor'))
        note = clean(a.get('note') or '', 'note', 200, required=False)
        no_secrets(note, 'goal note')
        gid = secrets.token_hex(8)
        h = hashlib.sha256(canonical({'id': gid, 'kcal': kcal, 'basis': basis}).encode()).hexdigest()[:10]
        with self.h.db() as db:
            db.execute('INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?)', (gid, kcal, basis, note, h, 'proposed', self.h.now(), None, json.dumps(est) if est else None))
        return {'proposal': gid, 'kcal': kcal, 'basis': basis, 'hash': h,
                'next': 'Ask the owner to confirm by sending: /healthgoal %s %s (nothing changes until the owner does).' % (gid, h)}

    def confirm_goal(self, a):
        gid, h = clean(a.get('proposal'), 'proposal', 16), clean(a.get('hash'), 'hash', 10)
        with self.h.db() as db:
            g = db.execute("SELECT * FROM goals WHERE id=? AND status='proposed'", (gid,)).fetchone()
            if not g or not secrets.compare_digest(g['hash'], h):
                raise ValueError('Unknown goal proposal or wrong code.')
            if g['created'] < self.h.now() - 7 * 86400:
                raise ValueError('That proposal expired; ask for a new one.')
            db.execute("UPDATE goals SET status='retired' WHERE status='active'")
            db.execute("UPDATE goals SET status='active',confirmed=? WHERE id=?", (self.h.now(), gid))
        return {'goal': gid, 'kcal': g['kcal'], 'basis': g['basis'], 'status': 'active'}

    def active_goal(self):
        with self.h.db() as db:
            g = db.execute("SELECT id,kcal,basis,note,confirmed FROM goals WHERE status='active'").fetchone()
        return dict(g) if g else None

    # ---------- foods ----------
    def cache_put(self, food, partial):
        with self.h.db() as db:
            old = db.execute('SELECT partial FROM food_cache WHERE fdc_id=?', (food['fdc_id'],)).fetchone()
            if old and not old['partial'] and partial:
                return
            db.execute('INSERT OR REPLACE INTO food_cache VALUES(?,?,?,?)', (food['fdc_id'], json.dumps(food), int(partial), self.h.now()))

    def cache_get(self, fid):
        with self.h.db() as db:
            r = db.execute('SELECT * FROM food_cache WHERE fdc_id=?', (fid,)).fetchone()
        if not r:
            return None
        return {'food': json.loads(r['body']), 'partial': bool(r['partial']), 'age_days': (self.h.now() - r['fetched']) / 86400}

    def cache_search(self, q):
        words = [w for w in q.lower().split() if len(w) > 2][:4]
        with self.h.db() as db:
            rows = db.execute('SELECT body FROM food_cache ORDER BY fetched DESC LIMIT 500').fetchall()
        out = []
        for r in rows:
            f = json.loads(r['body'])
            if words and all(w in f['description'].lower() for w in words):
                out.append({k: f[k] for k in ('fdc_id', 'description', 'brand', 'data_type', 'per_100g')})
        return out[:8]

    def add_custom_food(self, a):
        name = clean(a.get('name'), 'name', 120)
        per = a.get('per_serving')
        if not isinstance(per, dict):
            raise ValueError('per_serving is an object of nutrient values from the label (energy_kcal or energy_kj, protein_g, ...); omit unknown ones.')
        ns = {k: None for k in NKEYS}
        for k, v in per.items():
            if k == 'energy_kj' and v is not None:
                ns['energy_kcal'] = round(float(v) / KJ, 3)
            elif k in ns and v is not None:
                ns[k] = float(v)
            elif k not in ns and k != 'energy_kj':
                raise ValueError('Unknown nutrient %r; known: %s, energy_kj' % (k, ', '.join(NKEYS)))
        if any(v is not None and v < 0 for v in ns.values()):
            raise ValueError('Nutrient values are not negative.')
        sg = a.get('serving_g')
        fid = secrets.token_hex(8)
        with self.h.db() as db:
            db.execute('INSERT INTO custom_foods VALUES(?,?,?,?,?,?,?,?)', (fid, name, clean(a.get('brand') or '', 'brand', 80, required=False) or None,
                       float(sg) if sg else None, clean(a.get('serving_text') or '', 'serving_text', 80, required=False) or None, json.dumps(ns), a.get('basis') or 'label', self.h.now()))
        return {'custom_food': fid, 'name': name, 'per_serving': ns}

    # ---------- log ----------
    def resolve(self, item):
        """(food_ref, description, brand, grams, nutrients, basis, needs) for one item. Missing nutrients stay None."""
        qty = item.get('quantity')
        unit = str(item.get('unit') or '').strip().lower()
        try:
            qty = float(qty) if qty is not None else None
        except (TypeError, ValueError):
            raise ValueError('quantity is a number.')
        needs = None
        if qty is None or qty <= 0 or not unit:
            needs = 'how much (a number and a unit: g, oz, cup, serving...)'
        elif AMBIGUOUS.match(unit) and unit not in ('serving', 'servings'):
            needs = 'the portion: "%s" is ambiguous — ask for grams, ounces, cups or a label serving' % unit
        if item.get('custom_food'):
            with self.h.db() as db:
                f = db.execute('SELECT * FROM custom_foods WHERE id=?', (clean(item['custom_food'], 'custom_food', 16),)).fetchone()
            if not f:
                raise ValueError('Unknown custom food.')
            per = json.loads(f['per_serving'])
            servings = None
            if not needs:
                if unit.startswith('serving'):
                    servings = qty
                elif unit in MASS_G and f['serving_g']:
                    servings = qty * MASS_G[unit] / f['serving_g']
                else:
                    needs = 'servings of "%s" (it has %s)' % (f['name'], ('a %s g serving' % f['serving_g']) if f['serving_g'] else 'no gram weight')
            nut = {k: (None if v is None or servings is None else round(v * servings, 2)) for k, v in per.items()}
            return 'custom:' + f['id'], f['name'], f['brand'], (round(servings * f['serving_g'], 1) if servings and f['serving_g'] else None), nut, f['basis'], needs
        if item.get('fdc_id'):
            food = self.fdc.details(item['fdc_id'])
            grams = None
            if not needs:
                if unit in MASS_G:
                    grams = qty * MASS_G[unit]
                else:
                    pick = item.get('portion')
                    match = [p for p in food['portions'] if (pick and p['portion'].lower() == str(pick).lower()) or (not pick and unit.rstrip('s') in p['portion'].lower())]
                    if len(match) == 1:
                        grams = qty * match[0]['grams']
                    else:
                        needs = 'which portion: ' + ('; '.join(p['portion'] for p in food['portions'][:6]) or 'no household portions listed — ask for grams')
            nut = {k: (None if v is None or grams is None else round(v * grams / 100, 2)) for k, v in food['per_100g'].items()}
            return 'fdc:%d' % food['fdc_id'], food['description'], food.get('brand'), (round(grams, 1) if grams else None), nut, 'calculated (FDC per 100 g, %s)' % (food.get('published') or 'undated'), needs
        # Manual entry: whatever the owner states (label numbers or their own), with unknowns kept unknown.
        name = clean(item.get('name') or item.get('description') or '', 'name', 120)
        ns = {k: None for k in NKEYS}
        for k, v in (item.get('nutrients') or {}).items():
            if k == 'energy_kj' and v is not None:
                ns['energy_kcal'] = round(float(v) / KJ, 2)
            elif k in ns and v is not None:
                ns[k] = float(v)
        basis = item.get('basis') or 'manual'
        if basis not in ('manual', 'label', 'estimate'):
            raise ValueError('basis is manual, label or estimate (a model estimate is always a draft).')
        grams = qty * MASS_G[unit] if (qty and unit in MASS_G) else None
        return 'manual', name, item.get('brand'), grams, ns, basis, needs if ns['energy_kcal'] is None else None

    def log(self, a):
        key = a.get('entry_key')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_:.-]{7,99}', key):
            raise ValueError('entry_key is a stable id for this entry (8–100 letters, digits, _ : . -); the same message logged twice uses the same key.')
        items = a.get('items')
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError('items is a list of 1–20 foods.')
        local_date = clean(a.get('date') or self.today(), 'date', 10)
        local_time = clean(a.get('time') or '', 'time', 5, required=False) or None
        tz = clean(a.get('tz') or self.tz, 'tz', 40)
        if not DATE.match(local_date) or (local_time and not TIME.match(local_time)):
            raise ValueError('date is YYYY-MM-DD and time HH:MM (local).')
        no_secrets(canonical(items), 'food log')
        h = payload_hash({'items': items, 'date': local_date, 'time': local_time, 'tz': tz})
        with self.h.db() as db:
            old = db.execute('SELECT * FROM food_log WHERE substr(entry_key,1,?)=? ORDER BY entry_key', (len(key) + 1, key + '#')).fetchall()
        if old:
            if old[0]['payload_hash'] != h:
                raise ValueError('entry_key %s was already used for different food; this looks like a new entry (new key) or an edit (health_nutrition_edit).' % key)
            return {'entries': [self.public_entry(r) for r in old], 'duplicate': True, 'note': 'already logged; nothing added'}
        out = []
        now = self.h.now()
        resolved = [self.resolve(it) for it in items]
        with self.h.db() as db:
            for i, (it, (ref, desc, brand, grams, nut, basis, needs)) in enumerate(zip(items, resolved)):
                status = 'draft' if (needs or basis == 'estimate' or a.get('status') == 'draft') else 'confirmed'
                eid = secrets.token_hex(8)
                db.execute('INSERT INTO food_log VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)',
                           (eid, '%s#%02d' % (key, i), h, 1, local_date, local_time, tz, ref, desc, brand, clean(it.get('preparation') or '', 'preparation', 80, required=False) or None,
                            it.get('quantity'), it.get('unit'), grams, json.dumps(nut), basis, status, needs, a.get('source') or 'chat', now, now))
                db.execute('INSERT INTO food_log_history VALUES(?,?,?,?,?)', (eid, 1, now, 'created', None))
                out.append(self.public_entry(db.execute('SELECT * FROM food_log WHERE id=?', (eid,)).fetchone()))
        drafts = [e for e in out if e['status'] == 'draft']
        return {'entries': out, 'next': ('Ask the owner: ' + '; '.join('%s — %s' % (e['description'], e['needs'] or 'confirm the estimate') for e in drafts) +
                                         '. Then health_nutrition_edit with the answer.') if drafts else 'Logged.'}

    @staticmethod
    def public_entry(r):
        r = dict(r)
        return {'entry': r['id'], 'date': r['local_date'], 'time': r['local_time'], 'tz': r['tz'], 'food': r['food_ref'], 'description': r['description'], 'brand': r['brand'],
                'quantity': r['quantity'], 'unit': r['unit'], 'grams': r['grams'], 'nutrients': json.loads(r['nutrients']), 'basis': r['basis'], 'status': r['status'],
                'needs': r['needs'], 'version': r['version'], 'deleted': bool(r['deleted'])}

    def edit(self, a):
        eid = clean(a.get('entry'), 'entry', 16)
        with self.h.db() as db:
            r = db.execute('SELECT * FROM food_log WHERE id=?', (eid,)).fetchone()
        if not r or r['deleted']:
            raise ValueError('Unknown or deleted entry.')
        if a.get('version') is not None and int(a['version']) != r['version']:
            raise ValueError('That entry changed since you read it (now version %d); read it again.' % r['version'])
        cur = dict(r)
        if a.get('delete'):
            with self.h.db() as db:
                db.execute('UPDATE food_log SET deleted=1,version=version+1,updated=? WHERE id=?', (self.h.now(), eid))
                db.execute('INSERT INTO food_log_history VALUES(?,?,?,?,?)', (eid, r['version'] + 1, self.h.now(), 'deleted', json.dumps(self.public_entry(r))))
            return {'entry': eid, 'deleted': True, 'day': self.day({'date': r['local_date']})['totals']}
        item = {'quantity': a.get('quantity', cur['quantity']), 'unit': a.get('unit', cur['unit']), 'portion': a.get('portion')}
        ref = a.get('fdc_id') and ('fdc:%s' % a['fdc_id']) or a.get('custom_food') and ('custom:' + a['custom_food']) or cur['food_ref']
        if ref.startswith('fdc:'):
            item['fdc_id'] = int(ref[4:])
        elif ref.startswith('custom:'):
            item['custom_food'] = ref[7:]
        else:
            item['name'] = a.get('name') or cur['description']
            item['nutrients'] = a.get('nutrients') if a.get('nutrients') is not None else json.loads(cur['nutrients'])
            item['basis'] = a.get('basis') or (cur['basis'] if cur['basis'] in ('manual', 'label', 'estimate') else 'manual')
        new_ref, desc, brand, grams, nut, basis, needs = self.resolve(item)
        status = 'draft' if needs else ('confirmed' if a.get('confirm', basis != 'estimate') else 'draft')
        date = clean(a.get('date') or cur['local_date'], 'date', 10)
        tm = a.get('time', cur['local_time'])
        if not DATE.match(date) or (tm and not TIME.match(tm)):
            raise ValueError('date is YYYY-MM-DD and time HH:MM.')
        with self.h.db() as db:
            db.execute('UPDATE food_log SET version=version+1,food_ref=?,description=?,brand=?,quantity=?,unit=?,grams=?,nutrients=?,basis=?,status=?,needs=?,local_date=?,local_time=?,updated=? WHERE id=?',
                       (new_ref, desc, brand, item['quantity'], item['unit'], grams, json.dumps(nut), basis, status, needs, date, tm, self.h.now(), eid))
            db.execute('INSERT INTO food_log_history VALUES(?,?,?,?,?)', (eid, r['version'] + 1, self.h.now(), 'edited', json.dumps(self.public_entry(r))))
            out = self.public_entry(db.execute('SELECT * FROM food_log WHERE id=?', (eid,)).fetchone())
        return {'entry': out, 'moved_from': r['local_date'] if r['local_date'] != date else None}

    def history(self, eid):
        with self.h.db() as db:
            rows = db.execute('SELECT version,at,change FROM food_log_history WHERE entry_id=? ORDER BY version', (clean(eid, 'entry', 16),)).fetchall()
        return {'entry': eid, 'history': [dict(r) for r in rows]}

    def mark_day(self, a):
        d = clean(a.get('date') or self.today(), 'date', 10)
        if not DATE.match(d):
            raise ValueError('date is YYYY-MM-DD.')
        with self.h.db() as db:
            db.execute('INSERT OR REPLACE INTO day_status VALUES(?,?,?)', (d, 1 if a.get('complete', True) else 0, self.h.now()))
        return self.day({'date': d})

    def day(self, a):
        d = clean(a.get('date') or self.today(), 'date', 10)
        if not DATE.match(d):
            raise ValueError('date is YYYY-MM-DD.')
        with self.h.db() as db:
            rows = db.execute('SELECT * FROM food_log WHERE local_date=? AND deleted=0 ORDER BY local_time,created', (d,)).fetchall()
            st = db.execute('SELECT complete FROM day_status WHERE local_date=?', (d,)).fetchone()
        tot = {'confirmed': {k: 0.0 for k in NKEYS}, 'draft': {k: 0.0 for k in NKEYS}}
        unknown = {k: 0 for k in NKEYS}
        for r in rows:
            n = json.loads(r['nutrients'])
            for k in NKEYS:
                if n.get(k) is None:
                    unknown[k] += 1
                else:
                    tot['confirmed' if r['status'] == 'confirmed' else 'draft'][k] += n[k]
        for b in tot.values():
            for k in b:
                b[k] = round(b[k], 1)
        goal = self.active_goal()
        complete = bool(st and st['complete'])
        out = {'date': d, 'entries': [self.public_entry(r) for r in rows], 'totals': tot, 'unknown_counts': {k: v for k, v in unknown.items() if v},
               'completeness': 'complete (The owner marked the day complete)' if complete else 'partial — not marked complete; totals are what is logged so far',
               'complete': complete, 'goal': goal}
        if goal:
            kc = tot['confirmed']['energy_kcal']
            out['vs_goal'] = {'goal_kcal': goal['kcal'], 'logged_kcal': round(kc), 'difference_kcal': round(kc - goal['kcal']),
                              'note': None if complete else 'partial day: the difference is not a final figure'}
        if unknown['energy_kcal']:
            out['caution'] = '%d entr%s have no calorie value; the energy total is a minimum.' % (unknown['energy_kcal'], 'y' if unknown['energy_kcal'] == 1 else 'ies')
        return out

    def trends(self, a):
        days = min(max(int(a.get('days') or 28), 7), 180)
        end = dt.date.fromisoformat(clean(a.get('to') or self.today(), 'to', 10))
        start = end - dt.timedelta(days=days - 1)
        with self.h.db() as db:
            per_day = {r['local_date']: r['kcal'] for r in db.execute(
                "SELECT local_date, SUM(json_extract(nutrients,'$.energy_kcal')) AS kcal FROM food_log WHERE deleted=0 AND status='confirmed' AND local_date BETWEEN ? AND ? GROUP BY local_date",
                (start.isoformat(), end.isoformat()))}
            complete = {r['local_date'] for r in db.execute('SELECT local_date FROM day_status WHERE complete=1 AND local_date BETWEEN ? AND ?', (start.isoformat(), end.isoformat()))}
            weights = [dict(r) for r in db.execute("SELECT value_si,value,unit,measured_at,source FROM measurements WHERE kind='weight' AND deleted=0 AND substr(measured_at,1,10) BETWEEN ? AND ? ORDER BY measured_at",
                                                    (start.isoformat(), end.isoformat()))]
        full = [per_day[d] for d in sorted(complete) if d in per_day]
        out = {'from': start.isoformat(), 'to': end.isoformat(), 'days_logged': len(per_day), 'days_complete': len(complete),
               'average_kcal_complete_days': round(sum(full) / len(full)) if full else None,
               'note': 'Averages use only days the owner marked complete; partial days are excluded.'}
        if len(weights) >= 2:
            first, last = weights[0], weights[-1]
            span = max((dt.date.fromisoformat(last['measured_at'][:10]) - dt.date.fromisoformat(first['measured_at'][:10])).days, 1)
            out['weight'] = {'first': '%s %s on %s' % (first['value'], first['unit'], first['measured_at'][:10]), 'last': '%s %s on %s' % (last['value'], last['unit'], last['measured_at'][:10]),
                             'change_kg': round(last['value_si'] - first['value_si'], 2), 'change_lb': round((last['value_si'] - first['value_si']) / LB, 1),
                             'per_week_kg': round((last['value_si'] - first['value_si']) / span * 7, 2), 'measurements': len(weights),
                             'sources': sorted({w['source'] for w in weights})}
        return out

    # ---------- dispatch ----------
    def call(self, action, a, privileged=False):
        try:
            if action == 'health_nutrition_profile':
                return self.set_profile(a) if any(k in a for k in ('birth_date', 'sex_for_equation', 'gender_identity', 'activity', 'pregnant', 'breastfeeding')) else self.public_profile()
            if action == 'health_nutrition_measure':
                if a.get('delete'):
                    return self.delete_measurement(a.get('measurement'))
                return self.add_measurement(a, source='manual')
            if action == 'health_nutrition_estimate':
                return self.estimate_now(a)
            if action == 'health_nutrition_goal':
                if a.get('confirm'):
                    if not privileged:
                        raise ValueError('Only the owner confirms a calorie goal (/healthgoal ID CODE).')
                    return self.confirm_goal(a)
                if 'kcal' in a:
                    return self.propose_goal(a)
                return {'goal': self.active_goal()}
            if action == 'health_nutrition_food_search':
                return self.fdc.search(a.get('query'), a.get('page_size') or 8)
            if action == 'health_nutrition_food':
                return self.fdc.details(a.get('fdc_id'))
            if action == 'health_nutrition_custom_food':
                return self.add_custom_food(a)
            if action == 'health_nutrition_log':
                return self.log(a)
            if action == 'health_nutrition_edit':
                return self.edit(a)
            if action == 'health_nutrition_history':
                return self.history(a.get('entry'))
            if action == 'health_nutrition_day':
                return self.mark_day(a) if 'complete' in a else self.day(a)
            if action == 'health_nutrition_trends':
                return self.trends(a)
        except NutritionError as e:
            return {'error': e.kind, 'detail': e.detail,
                    'next': {'not_configured': 'Food search is not set up on this server; log the food manually (name + label numbers) instead.',
                             'rate_limited': 'Food search is busy; log manually now or try the search later.',
                             'unavailable': 'Food search is unreachable; log manually now (it keeps working offline).',
                             'corrupt': 'Food search returned bad data; nothing was saved from it. Log manually.',
                             'not_found': 'No such food id; search again.'}.get(e.kind, 'Log manually instead.')}
        raise ValueError('Unknown nutrition action.')
