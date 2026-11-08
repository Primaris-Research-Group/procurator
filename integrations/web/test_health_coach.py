"""Health and wellness coach (0.26.0): opt-in, questionnaire and screening, the published-guidance calorie plan with its floors,
the energy-balance equation (device vs estimate, never counting exercise twice), metrics, reminders and the safety pauses.
Synthetic data only."""
import datetime as dt
import tempfile
import unittest
from zoneinfo import ZoneInfo

from health import Health
from health_coach import Coach, plan_for, bp_category, PROMPTS
from health_nutrition import Nutrition


class Quiet:
    def __init__(self):
        self.messages = []
    def telegram(self, m):
        self.messages.append(m)
    def send_with_buttons(self, m, rows):
        self.messages.append(m)


def at(y, mo, d, h, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=ZoneInfo('America/New_York')).timestamp()


class Plan(unittest.TestCase):
    def test_standard_and_faster_paces_by_bmi(self):
        p = plan_for(32.0, 'male', 2600, 1800, 100, 'standard')
        self.assertEqual((p['deficit_kcal'], p['target_kcal']), (500, 2100))
        self.assertAlmostEqual(p['rough_weekly_change_kg'], -0.45, places=2)
        self.assertIn('(2600) − 500 = 2100', p['equation'])
        self.assertEqual(plan_for(32.0, 'male', 2600, 1800, 100, 'faster')['target_kcal'], 1850)
        q = plan_for(27.0, 'male', 2600, 1800, 90, 'faster')     # 750 only with BMI >= 30
        self.assertEqual((q['pace'], q['target_kcal']), ('standard', 2100))

    def test_healthy_bmi_gets_at_most_gentle_and_underweight_gets_no_plan(self):
        p = plan_for(23.0, 'female', 2000, 1350, 62, 'standard')
        self.assertEqual((p['pace'], p['deficit_kcal'], p['target_kcal']), ('gentle', 250, 1750))
        self.assertEqual(plan_for(17.9, 'female', 1900, 1250, 48)['no_plan'], 'underweight')

    def test_floors_resting_energy_and_sex_specific_minimum(self):
        p = plan_for(31.0, 'female', 1650, 1250, 80, 'faster')   # 1650 − 750 = 900 → floor max(1200, 1250)
        self.assertEqual((p['target_kcal'], p['deficit_kcal']), (1250, 400))
        self.assertTrue(any('floor' in n for n in p['notes']))
        m = plan_for(31.0, 'male', 1900, 1450, 95, 'faster')     # men: 1,500 minimum
        self.assertEqual(m['target_kcal'], 1500)
        self.assertEqual(plan_for(31.0, 'female', 1250, 1250, 80)['no_plan'], 'no_room')

    def test_pace_never_exceeds_one_percent_a_week(self):
        p = plan_for(31.0, 'female', 2700, 1400, 45, 'faster')   # 45 kg: 1% = 0.45 kg/week → ≤ 495 kcal/day
        self.assertLessEqual(-p['rough_weekly_change_kg'], 0.45 + 1e-9)
        self.assertTrue(any('1%' in n for n in p['notes']))

    def test_screening_stops_the_plan(self):
        self.assertEqual(plan_for(32, 'male', 2600, 1800, 100, screening={'eating_disorder': 'yes'})['no_plan'], 'eating_disorder')
        r = plan_for(32, 'male', 2600, 1800, 100, screening={'supervised': ['diabetes_meds', 'weight_meds']})
        self.assertEqual(r['no_plan'], 'supervised')
        self.assertIn('insulin', r['why'])

    def test_target_weight_below_bmi_18_5_is_refused(self):
        p = plan_for(27.0, 'female', 2100, 1400, 75, 'standard', target_kg=48, height_cm=170)
        self.assertTrue(p['target_weight']['refused'])
        ok = plan_for(27.0, 'female', 2100, 1400, 75, 'standard', target_kg=68, height_cm=170)['target_weight']
        self.assertEqual(ok['to_lose_kg'], 7.0)
        self.assertGreater(ok['rough_weeks_static'], 10)

    def test_bp_categories(self):
        self.assertEqual(bp_category(118, 76), 'normal')
        self.assertEqual(bp_category(124, 78), 'elevated')
        self.assertEqual(bp_category(134, 82), 'stage 1 hypertension range')
        self.assertEqual(bp_category(150, 70), 'stage 2 hypertension range')
        self.assertTrue(bp_category(186, 100).startswith('severe'))


class CoachFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [at(2026, 11, 9, 10)]   # Monday 10:00 Eastern
        self.p = Quiet()
        self.h = Health(self.p, self.tmp.name, now=lambda: self.clock[0])
        self.h.call('health_adult_confirm', {}, True)
        self.n = self.h.attach('health_nutrition_', Nutrition(self.h))
        self.c = self.h.attach('health_coach_', Coach(self.h, self.n))
        for cat in ('nutrition', 'wellness'):
            self.h.call('health_read_allow', {'category': cat, 'days': 365}, True)

    def tearDown(self):
        self.tmp.cleanup()

    def on(self):
        self.h.call('health_coach_settings', {'set': 'on'}, True)

    def profile(self, kg=100, sex='male'):
        self.n.set_profile({'birth_date': '1980-05-01', 'sex_for_equation': sex, 'activity': 'light'})
        self.n.add_measurement({'kind': 'weight', 'value': kg, 'unit': 'kg', 'measured_at': '2026-11-08'})
        self.n.add_measurement({'kind': 'height', 'value': 177, 'unit': 'cm', 'measured_at': '2026-11-01'})

    def eat(self, day, kcal, key):
        self.n.log({'entry_key': key, 'date': day, 'items': [{'name': 'synthetic meal', 'quantity': 1, 'unit': 'serving', 'nutrients': {'energy_kcal': kcal}}]})
        self.n.mark_day({'date': day})

    def test_off_until_the_owner_turns_it_on(self):
        with self.assertRaisesRegex(ValueError, '/coach on'):
            self.h.call('health_coach_questionnaire', {})
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.h.call('health_coach_settings', {'set': 'on'}, False)
        self.assertFalse(self.h.call('health_coach_status', {})['enabled'])
        self.on()
        q = self.h.call('health_coach_questionnaire', {})
        self.assertIn('eating_disorder', [x['id'] for x in q['questions']])

    def test_answers_set_goal_reminders_and_eating_disorder_limits(self):
        self.on()
        r = self.h.call('health_coach_answers', {'goals': ['weight_loss', 'stress', 'heart_health'], 'eating_disorder': 'no', 'supervised': []})
        self.assertEqual(set(r['reminders']), {'meals', 'weigh_in', 'stress', 'exercise', 'blood_pressure'})
        r = self.h.call('health_coach_answers', {'goals': ['weight_loss', 'track_calories'], 'eating_disorder': 'yes', 'supervised': [], 'reminders': {'meals': '19:00'}})
        self.assertNotIn('meals', r['reminders'])
        self.assertIn('National Alliance for Eating Disorders', r['say'])
        self.assertTrue(any('eating-disorder' in x for x in r['limits']))
        self.assertEqual(self.h.call('health_coach_plan', {})['no_plan'], 'eating_disorder')
        self.assertIsNone(self.h.call('health_coach_balance', {})['balance'])
        with self.assertRaisesRegex(ValueError, 'stay off'):
            self.h.call('health_coach_settings', {'set': 'reminder', 'kind': 'meals', 'time': '19:00'}, True)
        with self.assertRaises(ValueError):
            self.h.call('health_coach_answers', {'goals': ['get_shredded']})

    def test_plan_from_profile_proposes_a_goal_he_must_confirm(self):
        self.on()
        self.profile()
        self.h.call('health_coach_answers', {'goals': ['weight_loss'], 'eating_disorder': 'no', 'supervised': [], 'pace': 'standard', 'target_weight': 200, 'target_weight_unit': 'lb'})
        p = self.h.call('health_coach_plan', {'propose': True})
        self.assertEqual(p['bmi']['category'], 'obesity')
        self.assertEqual(p['deficit_kcal'], 500)
        self.assertIn('/healthgoal', p['goal_proposal']['next'])
        self.assertIsNone(self.n.active_goal())     # nothing changes until the owner confirms
        g = p['goal_proposal']
        self.h.call('health_nutrition_goal', {'confirm': True, 'proposal': g['proposal'], 'hash': g['hash']}, True)
        self.assertEqual(self.n.active_goal()['kcal'], p['target_kcal'])
        self.assertIn('target_weight', p)

    def test_balance_uses_device_energy_without_counting_exercise_twice(self):
        self.on()
        self.profile()
        self.h.call('health_coach_answers', {'goals': ['weight_loss'], 'eating_disorder': 'no', 'supervised': []})
        with self.h.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS daily_stats(date TEXT,type TEXT,device TEXT,value REAL,unit TEXT,tz TEXT,method TEXT,updated REAL,PRIMARY KEY(date,type,device))')
            db.execute("INSERT INTO daily_stats VALUES('2026-11-08','active_energy','d1',500,'kcal','America/New_York','sum',0)")
            db.execute("INSERT INTO daily_stats VALUES('2026-11-08','basal_energy','d1',1900,'kcal','America/New_York','sum',0)")
        self.eat('2026-11-08', 1800, 'meal-2026-11-08')
        self.eat('2026-11-07', 2000, 'meal-2026-11-07')
        b = self.h.call('health_coach_balance', {'days': 3, 'to': '2026-11-08'})
        d8 = [d for d in b['days'] if d['date'] == '2026-11-08'][0]
        self.assertEqual((d8['mode'], d8['burned_kcal'], d8['eaten_kcal'], d8['deficit_kcal']), ('device', 2400, 1800, 600))
        self.assertIn('Apple Health resting 1900 + active 500', d8['burned_from'])
        d7 = [d for d in b['days'] if d['date'] == '2026-11-07'][0]
        est = self.n.estimate_now()['estimate']['maintenance_kcal']
        self.assertEqual((d7['mode'], d7['burned_kcal']), ('estimate', est))   # no device data: the activity-level estimate, nothing added
        self.assertEqual(b['summary']['complete_days'], 2)
        self.assertEqual(b['summary']['total_deficit_kcal'], 600 + est - 2000)
        self.assertIn('never to the activity-level maintenance', ' '.join(b['notes']))

    def test_observed_maintenance_from_his_own_trend(self):
        self.on()
        self.profile(kg=100)
        self.h.call('health_coach_answers', {'goals': ['weight_loss'], 'eating_disorder': 'no', 'supervised': []})
        start = dt.date(2026, 10, 12)
        for i in range(28):
            self.eat((start + dt.timedelta(days=i)).isoformat(), 2200, 'm-%02d-synthetic' % i)
        self.n.add_measurement({'kind': 'weight', 'value': 101.0, 'unit': 'kg', 'measured_at': '2026-10-12'})
        self.n.add_measurement({'kind': 'weight', 'value': 99.6, 'unit': 'kg', 'measured_at': '2026-11-08'})
        obs = self.h.call('health_coach_balance', {'days': 7, 'to': '2026-11-08'})['observed_maintenance']
        # 2200 + 1.4 kg × 7700 / 27 days ≈ 2599 → 2600
        self.assertEqual(obs['kcal'], 2600)

    def test_restriction_pauses_calorie_reminders_and_says_so_once(self):
        self.on()
        self.profile(sex='female', kg=80)
        self.h.call('health_coach_answers', {'goals': ['weight_loss'], 'eating_disorder': 'no', 'supervised': []})
        for i, day in enumerate(('2026-11-05', '2026-11-06', '2026-11-07')):
            self.eat(day, 900, 'low-%d-synthetic' % i)
        self.clock[0] = at(2026, 11, 9, 19, 35)
        out = self.h.call('health_coach_due', {}, True)['send']
        kinds = [x['kind'] for x in out]
        self.assertIn('caution', kinds)
        self.assertNotIn('meals', kinds)
        self.assertIn('/coach resume', out[0]['text'])
        self.assertNotIn('900', out[0]['text'].replace('1200', ''))   # no intake numbers in Telegram
        self.clock[0] += 300
        self.assertNotIn('caution', [x['kind'] for x in self.h.call('health_coach_due', {}, True)['send']])
        b = self.h.call('health_coach_balance', {'days': 1, 'to': '2026-11-08'})
        self.assertIn('below', b['caution'])

    def test_reminders_fixed_text_windows_caps_and_quiet_hours(self):
        self.on()
        self.h.call('health_coach_answers', {'goals': ['weight_loss', 'stress', 'activity', 'sleep', 'heart_health'], 'eating_disorder': 'no', 'supervised': [],
                                             'reminders': {'breathing': '19:40'}})
        self.clock[0] = at(2026, 11, 9, 7, 35)   # Monday: weigh-in
        out = self.h.call('health_coach_due', {}, True)['send']
        self.assertEqual([x['text'] for x in out], [PROMPTS['weigh_in'][2]])
        self.assertEqual(self.h.call('health_coach_due', {}, True)['send'], [])          # once a day
        self.clock[0] = at(2026, 11, 9, 15, 10)
        self.assertEqual([x['kind'] for x in self.h.call('health_coach_due', {}, True)['send']], ['stress'])
        self.clock[0] = at(2026, 11, 9, 19, 45)
        sent = [x['kind'] for x in self.h.call('health_coach_due', {}, True)['send']]
        self.assertEqual(sent, ['meals'])     # cap of 3 a day: breathing (19:40) waits; exercise/sleep later are capped too
        self.clock[0] = at(2026, 11, 9, 22, 5)
        self.assertEqual(self.h.call('health_coach_due', {}, True)['send'], [])          # quiet hours
        self.clock[0] = at(2026, 11, 10, 15, 50)
        self.assertEqual(self.h.call('health_coach_due', {}, True)['send'], [])          # 45-minute window passed
        with self.assertRaisesRegex(ValueError, 'timer'):
            self.h.call('health_coach_due', {}, False)

    def test_pause_and_off(self):
        self.on()
        self.h.call('health_coach_answers', {'goals': ['stress'], 'eating_disorder': 'skip', 'supervised': []})
        self.h.call('health_coach_settings', {'set': 'pause', 'days': 2}, True)
        self.clock[0] = at(2026, 11, 10, 15, 5)
        self.assertEqual(self.h.call('health_coach_due', {}, True)['send'], [])
        self.clock[0] = at(2026, 11, 12, 15, 5)
        self.assertEqual(len(self.h.call('health_coach_due', {}, True)['send']), 1)
        self.h.call('health_coach_settings', {'set': 'off'}, True)
        self.clock[0] = at(2026, 11, 13, 15, 5)
        self.assertEqual(self.h.call('health_coach_due', {}, True)['send'], [])

    def test_metrics_bp_urgent_stress_breathing_and_activity_week(self):
        self.on()
        self.h.call('health_coach_answers', {'goals': ['heart_health', 'stress'], 'eating_disorder': 'no', 'supervised': []})
        r = self.h.call('health_coach_metric', {'kind': 'blood_pressure', 'value': '186/122'})
        self.assertIn('call 911', r['urgent'])
        r = self.h.call('health_coach_metric', {'kind': 'blood_pressure', 'value': '128/79'})
        self.assertTrue(r['category'].startswith('elevated'))
        with self.assertRaises(ValueError):
            self.h.call('health_coach_metric', {'kind': 'blood_pressure', 'value': '80/120'})
        self.assertIn('Box breathing', self.h.call('health_coach_metric', {'kind': 'stress', 'value': 5})['offer'])
        self.assertNotIn('offer', self.h.call('health_coach_metric', {'kind': 'stress', 'value': 2}))
        self.h.call('health_coach_metric', {'kind': 'exercise_minutes', 'value': 40, 'intensity': 'vigorous', 'activity': 'run'})
        self.h.call('health_coach_metric', {'kind': 'exercise_minutes', 'value': 30, 'intensity': 'moderate'})
        self.h.call('health_coach_metric', {'kind': 'waist', 'value': 38, 'unit': 'in'})
        self.h.call('health_coach_metric', {'kind': 'water', 'value': 4, 'unit': 'cups'})
        m = self.h.call('health_coach_metrics', {'days': 7})
        self.assertEqual(m['metrics']['blood_pressure']['readings'], 2)
        self.assertEqual(m['activity_week']['moderate_equivalent_minutes'], 110)
        self.assertFalse(m['activity_week']['met'])
        self.assertIn('96.5 cm', m['metrics']['waist']['latest'])
        self.assertEqual(m['metrics']['water']['total'], 960)
        w = self.h.call('health_coach_metric', {'kind': 'weight', 'value': 210, 'unit': 'lb'})
        self.assertEqual(w['kind'], 'weight')

    def test_reads_of_coach_data_need_his_permission(self):
        self.on()
        self.h.call('health_coach_answers', {'goals': ['stress'], 'eating_disorder': 'no', 'supervised': []})
        self.h.call('health_read_allow', {'category': 'wellness', 'allow': 'off'}, True)
        with self.assertRaisesRegex(ValueError, 'not allowed reading'):
            self.h.call('health_coach_metrics', {})
        self.assertIn('/healthread', self.p.messages[-1])

    def test_forget_deletes_everything(self):
        self.on()
        self.h.call('health_coach_answers', {'goals': ['stress'], 'eating_disorder': 'no', 'supervised': []})
        self.h.call('health_coach_metric', {'kind': 'mood', 'value': 3})
        out = self.h.call('health_coach_settings', {'set': 'forget'}, True)
        self.assertEqual(out['deleted_metrics'], 1)
        self.assertFalse(self.h.call('health_coach_status', {})['enabled'])


if __name__ == '__main__':
    unittest.main()
