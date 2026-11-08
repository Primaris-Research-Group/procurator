"""Nutrition v1 (web 0.23.0; build-plans/health/06 acceptance tests). Reference vectors are hand-calculated from the published
Mifflin–St Jeor equation (NUTR-SPEC-0.1, pending physician review). Food data is a synthetic FoodData Central fake."""
import json
import tempfile
import unittest

from health import Health
from health_nutrition import FDC, Nutrition, age_on, estimate, to_cm, to_kg, KJ

NOW = 1_794_200_000.0          # 2026-11-08 (America/New_York)



def ready(h):
    """0.25.0: the owner confirmed 18+ (/healthadult) and allowed stored-data reads (/healthread) — the tests below exercise
    the health features themselves; test_health_gates.py covers the gates."""
    h.call('health_adult_confirm', {}, True)
    for c in ('activity', 'nutrition', 'appointments'):
        h.call('health_read_allow', {'category': c, 'days': 365}, True)
    return h

class Quiet:
    def send_with_buttons(self, *a):
        pass


def food(fid, desc, per100, portions=(), dtype='SR Legacy', **extra):
    ids = {'energy_kcal': 1008, 'protein_g': 1003, 'fat_g': 1004, 'carbs_g': 1005, 'fiber_g': 1079, 'sugars_g': 2000, 'sodium_mg': 1093}
    return {'fdcId': fid, 'description': desc, 'dataType': dtype, 'publicationDate': '2019-04-01',
            'foodNutrients': [{'nutrient': {'id': ids[k], 'number': '', 'unitName': 'KCAL' if k == 'energy_kcal' else 'MG' if k == 'sodium_mg' else 'G'}, 'amount': v} for k, v in per100.items()],
            'foodPortions': [{'amount': 1, 'measureUnit': {'name': u}, 'modifier': m, 'gramWeight': g} for u, m, g in portions], **extra}


FOODS = {
    171287: food(171287, 'Egg, whole, cooked, hard-boiled', {'energy_kcal': 155, 'protein_g': 12.6, 'fat_g': 10.6, 'carbs_g': 1.12, 'sodium_mg': 124},
                 portions=[('undetermined', 'large', 50), ('cup', 'chopped', 136)]),
    169704: food(169704, 'Rice, white, long-grain, cooked', {'energy_kcal': 130, 'protein_g': 2.69, 'carbs_g': 28.2}, portions=[('cup', '', 158)]),
    999001: food(999001, 'Greek yogurt, plain', {'protein_g': 10}, dtype='Branded', brandOwner='Synthetic Dairy Co', servingSize=170, servingSizeUnit='g'),   # energy missing
    999002: {'fdcId': 999002, 'description': 'Lentil soup', 'dataType': 'Foundation', 'foodNutrients': [{'nutrientId': 1062, 'value': 418.4, 'unitName': 'kJ'}]},
}


class FakeFDC:
    def __init__(self):
        self.calls, self.mode = [], None
    def __call__(self, path, params):
        self.calls.append(path)
        if self.mode == '429':
            return 429, b'{}'
        if self.mode == 'garbage':
            return 200, b'<html>'
        if self.mode == 'down':
            from health_nutrition import NutritionError
            raise NutritionError('unavailable', 'unreachable')
        if path == 'foods/search':
            q = params['query'].lower()
            return 200, json.dumps({'foods': [f for f in FOODS.values() if all(w in f['description'].lower() for w in q.split())]}).encode()
        fid = int(path.split('/')[1])
        return (200, json.dumps(FOODS[fid]).encode()) if fid in FOODS else (404, b'{}')


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = [NOW]
        self.h = Health(Quiet(), self.tmp.name, now=lambda: self.clock[0])
        ready(self.h)
        self.fake = FakeFDC()
        self.n = self.h.attach('health_nutrition_', Nutrition(self.h, FDC(None, key='synthetic-key', fetch=self.fake)))

    def tearDown(self):
        self.tmp.cleanup()

    def c(self, action, a=None, privileged=False):
        return self.h.call('health_nutrition_' + action, a or {}, privileged)

    def adult(self, sex='male', birth='1996-03-01', kg=80, cm=180, activity='moderate'):
        self.c('profile', {'birth_date': birth, 'sex_for_equation': sex, 'activity': activity, **({'pregnant': False, 'breastfeeding': False} if sex == 'female' else {})})
        self.c('measure', {'kind': 'weight', 'value': kg, 'unit': 'kg', 'measured_at': '2026-11-07'})
        self.c('measure', {'kind': 'height', 'value': cm, 'unit': 'cm', 'measured_at': '2026-01-10'})


class CalculationTests(Base):
    def test_reference_vectors(self):
        m = estimate('male', 30, 80, 180, 'moderate')
        self.assertAlmostEqual(m['ree'], 1780.0); self.assertAlmostEqual(m['maintenance'], 2759.0)
        self.assertEqual((m['ree_kcal'], m['maintenance_kcal']), (1780, 2760))
        f = estimate('female', 45, 65, 165, 'sedentary')
        self.assertAlmostEqual(f['ree'], 1295.25); self.assertAlmostEqual(f['maintenance'], 1554.3)
        self.assertEqual(estimate('male', 30, 80, 180, 'extra')['maintenance_kcal'], 3380)          # 1780 × 1.9 = 3382

    def test_units_are_equivalent(self):
        self.assertAlmostEqual(to_kg(176.37, 'lb'), 80.0, places=2)
        self.assertAlmostEqual(to_cm(70.866, 'in'), 180.0, places=2); self.assertAlmostEqual(to_cm(1.8, 'm'), 180.0)
        self.adult()
        a = self.c('estimate')['estimate']
        self.c('measure', {'kind': 'weight', 'value': 176.37, 'unit': 'lb', 'measured_at': '2026-11-08'})
        self.c('measure', {'kind': 'height', 'value': 1.8, 'unit': 'm', 'measured_at': '2026-11-08'})
        b = self.c('estimate')['estimate']
        self.assertEqual(a['maintenance_kcal'], b['maintenance_kcal']); self.assertEqual(b['maintenance_kj'], int(round(2759 * KJ / 10) * 10))

    def test_birthday_boundary(self):
        self.assertEqual(age_on('1996-11-08', '2026-11-07'), 29); self.assertEqual(age_on('1996-11-08', '2026-11-08'), 30)
        self.assertEqual(age_on('2008-11-09', '2026-11-08'), 17)

    def test_estimate_is_labelled_unreviewed_with_bmi_and_provenance(self):
        self.adult()
        out = self.c('estimate')
        self.assertEqual((out['spec'], out['review']), ('NUTR-SPEC-0.1', 'pending physician review'))
        self.assertEqual(out['estimate']['resting_kcal'], 1780)       # born 1996-03-01 → 30 on 2026-11-08
        self.assertEqual(out['bmi'], {'value': 24.7, 'category': 'healthy range', 'note': out['bmi']['note']})
        self.assertIn('2026-11-07', out['inputs']['weight']); self.assertIn('Do not add exercise calories', out['say'])

    def test_missing_inputs_and_stale_weight_are_asked_for_not_assumed(self):
        out = self.c('estimate')
        self.assertIsNone(out['estimate']); self.assertIn('birth date', ' '.join(out['missing']))
        self.adult()
        self.clock[0] += 40 * 86400
        out = self.c('estimate')
        self.assertIsNone(out['estimate']); self.assertIn('over 30 days old', out['why'])

    def test_population_screening(self):
        self.adult(sex='female', birth='1981-05-01', kg=65, cm=165)
        self.assertEqual(self.c('estimate')['estimate']['resting_kcal'], 1300)        # 1295.25, age 45
        self.c('profile', {'pregnant': True})
        out = self.c('estimate')
        self.assertIsNone(out['estimate']); self.assertIn('pregnant', out['why']); self.assertIn('clinician', out['why'])
        self.c('profile', {'pregnant': None})
        self.assertIn('pregnant or breastfeeding', ' '.join(self.c('estimate')['missing']))
        self.c('profile', {'pregnant': False, 'birth_date': '2010-01-01'})
        self.assertIn('under 18', self.c('estimate')['why'])

    def test_the_equation_input_is_explicit_and_separate_from_gender_identity(self):
        with self.assertRaisesRegex(ValueError, 'never inferred'):
            self.c('profile', {'sex_for_equation': 'man'})
        p = self.c('profile', {'sex_for_equation': 'female', 'gender_identity': 'nonbinary'})
        self.assertEqual((p['sex_for_equation'], p['gender_identity']), ('female', 'nonbinary'))

    def test_implausible_measurements_and_future_dates_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'plausible'):
            self.c('measure', {'kind': 'weight', 'value': 800, 'unit': 'kg'})
        with self.assertRaisesRegex(ValueError, 'plausible'):
            self.c('measure', {'kind': 'height', 'value': 180, 'unit': 'in'})
        with self.assertRaisesRegex(ValueError, 'future'):
            self.c('measure', {'kind': 'weight', 'value': 80, 'unit': 'kg', 'measured_at': '2027-01-01'})


class GoalTests(Base):
    def test_goals_are_confirmed_by_the_owner_only(self):
        self.adult()
        p = self.c('goal', {'kcal': 2500, 'basis': 'own'})
        self.assertIsNone(self.c('goal')['goal'])
        with self.assertRaisesRegex(ValueError, 'Only the owner'):
            self.c('goal', {'confirm': True, 'proposal': p['proposal'], 'hash': p['hash']})
        with self.assertRaisesRegex(ValueError, 'wrong code'):
            self.c('goal', {'confirm': True, 'proposal': p['proposal'], 'hash': '0000000000'}, True)
        self.c('goal', {'confirm': True, 'proposal': p['proposal'], 'hash': p['hash']}, True)
        self.assertEqual(self.c('goal')['goal']['kcal'], 2500)

    def test_low_targets_need_a_clinician(self):
        self.adult()
        with self.assertRaisesRegex(ValueError, 'below 1780'):
            self.c('goal', {'kcal': 1500})
        self.assertEqual(self.c('goal', {'kcal': 1500, 'basis': 'clinician'})['kcal'], 1500)
        with self.assertRaises(ValueError):
            self.c('goal', {'kcal': 500, 'basis': 'clinician'})


class LogTests(Base):
    def test_fdc_portions_grams_and_unknown_nutrients(self):
        out = self.c('log', {'entry_key': 'breakfast-1108', 'date': '2026-11-08', 'time': '08:10', 'items': [
            {'fdc_id': 171287, 'quantity': 2, 'unit': 'large'},
            {'fdc_id': 169704, 'quantity': 100, 'unit': 'g'},
            {'fdc_id': 999001, 'quantity': 1, 'unit': 'serving'}]})
        eggs, rice, yog = out['entries']
        self.assertEqual((eggs['grams'], eggs['nutrients']['energy_kcal'], eggs['status']), (100.0, 155.0, 'confirmed'))
        self.assertEqual(rice['nutrients']['energy_kcal'], 130.0); self.assertIsNone(rice['nutrients']['fat_g'])      # unknown, not zero
        self.assertEqual((yog['grams'], yog['nutrients']['protein_g'], yog['nutrients']['energy_kcal']), (170.0, 17.0, None))
        d = self.c('day', {'date': '2026-11-08'})
        self.assertEqual(d['totals']['confirmed']['energy_kcal'], 285.0); self.assertIn('minimum', d['caution'])
        self.assertTrue(d['completeness'].startswith('partial'))

    def test_kilojoules_and_serving_versus_100g(self):
        e = self.c('log', {'entry_key': 'lunch-1108-a', 'items': [{'fdc_id': 999002, 'quantity': 250, 'unit': 'g'}]})['entries'][0]
        self.assertAlmostEqual(e['nutrients']['energy_kcal'], 250.0, places=1)       # 418.4 kJ/100 g = 100 kcal/100 g
        cf = self.c('custom_food', {'name': 'Protein bar (synthetic)', 'serving_g': 60, 'per_serving': {'energy_kj': 836.8, 'protein_g': 20}})
        a = self.c('log', {'entry_key': 'snack-1108-a', 'items': [{'custom_food': cf['custom_food'], 'quantity': 1.5, 'unit': 'serving'}]})['entries'][0]
        b = self.c('log', {'entry_key': 'snack-1108-b', 'items': [{'custom_food': cf['custom_food'], 'quantity': 90, 'unit': 'g'}]})['entries'][0]
        self.assertEqual(a['nutrients'], b['nutrients']); self.assertEqual(a['nutrients']['energy_kcal'], 300.0)
        self.assertEqual(a['basis'], 'label')

    def test_ambiguous_portions_stay_drafts_until_clarified(self):
        out = self.c('log', {'entry_key': 'dinner-1108-a', 'items': [{'fdc_id': 169704, 'quantity': 1, 'unit': 'bowl'}]})
        e = out['entries'][0]
        self.assertEqual(e['status'], 'draft'); self.assertIn('ambiguous', e['needs']); self.assertIn('Ask the owner', out['next'])
        self.assertEqual(self.c('day')['totals']['confirmed']['energy_kcal'], 0)
        fixed = self.c('edit', {'entry': e['entry'], 'quantity': 1.5, 'unit': 'cup'})['entry']
        self.assertEqual((fixed['status'], fixed['grams'], fixed['nutrients']['energy_kcal']), ('confirmed', 237.0, 308.1))
        self.assertEqual([h['change'] for h in self.c('history', {'entry': e['entry']})['history']], ['created', 'edited'])

    def test_model_estimates_are_drafts_until_he_confirms(self):
        e = self.c('log', {'entry_key': 'dinner-1108-b', 'items': [{'name': 'Chicken burrito', 'quantity': 1, 'unit': 'serving', 'basis': 'estimate', 'nutrients': {'energy_kcal': 900}}]})['entries'][0]
        self.assertEqual(e['status'], 'draft')
        self.assertEqual(self.c('day')['totals']['draft']['energy_kcal'], 900)
        ok = self.c('edit', {'entry': e['entry'], 'confirm': True})['entry']
        self.assertEqual((ok['status'], ok['basis']), ('confirmed', 'estimate'))

    def test_duplicate_submissions_and_reused_keys(self):
        a = {'entry_key': 'coffee-1108', 'items': [{'name': 'Latte', 'quantity': 12, 'unit': 'fl oz', 'basis': 'label', 'nutrients': {'energy_kcal': 190}}]}
        self.c('log', a)
        again = self.c('log', a)
        self.assertTrue(again['duplicate']); self.assertEqual(len(self.c('day')['entries']), 1)
        with self.assertRaisesRegex(ValueError, 'different food'):
            self.c('log', {**a, 'items': [{'name': 'Latte', 'quantity': 16, 'unit': 'fl oz', 'basis': 'label', 'nutrients': {'energy_kcal': 250}}]})
        self.c('log', {'entry_key': 'coffee_1108', 'items': a['items']})       # '_' is not a wildcard
        self.assertEqual(len(self.c('day')['entries']), 2)

    def test_deletes_and_moves_recompute_the_days(self):
        e = self.c('log', {'entry_key': 'late-snack-01', 'date': '2026-11-07', 'time': '23:50', 'items': [{'name': 'Toast', 'quantity': 1, 'unit': 'slice', 'basis': 'label', 'nutrients': {'energy_kcal': 80}}]})['entries'][0]
        self.assertEqual(self.c('day', {'date': '2026-11-07'})['totals']['confirmed']['energy_kcal'], 80)
        moved = self.c('edit', {'entry': e['entry'], 'date': '2026-11-08', 'time': '00:10'})
        self.assertEqual(moved['moved_from'], '2026-11-07')
        self.assertEqual(self.c('day', {'date': '2026-11-07'})['totals']['confirmed']['energy_kcal'], 0)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            self.c('edit', {'entry': e['entry'], 'version': 1, 'delete': True})
        self.c('edit', {'entry': e['entry'], 'delete': True})
        self.assertEqual(self.c('day', {'date': '2026-11-08'})['entries'], [])

    def test_partial_days_stay_partial_even_at_zero_and_only_complete_days_average(self):
        self.assertTrue(self.c('day', {'date': '2026-11-06'})['completeness'].startswith('partial'))
        for d, kcal in (('2026-11-05', 2000), ('2026-11-06', 2400), ('2026-11-07', 600)):
            self.c('log', {'entry_key': 'day-total-' + d, 'date': d, 'items': [{'name': 'Meals', 'quantity': 1, 'unit': 'serving', 'basis': 'manual', 'nutrients': {'energy_kcal': kcal}}]})
        self.c('day', {'date': '2026-11-05', 'complete': True}); self.c('day', {'date': '2026-11-06', 'complete': True})
        t = self.c('trends', {'days': 7, 'to': '2026-11-08'})
        self.assertEqual((t['days_logged'], t['days_complete'], t['average_kcal_complete_days']), (3, 2, 2200))

    def test_goal_comparison_says_partial(self):
        self.adult()
        p = self.c('goal', {'kcal': 2600}); self.c('goal', {'confirm': True, **p}, True)
        self.c('log', {'entry_key': 'meal-cmp-0001', 'items': [{'name': 'Lunch', 'quantity': 1, 'unit': 'serving', 'basis': 'manual', 'nutrients': {'energy_kcal': 700}}]})
        v = self.c('day')['vs_goal']
        self.assertEqual((v['goal_kcal'], v['logged_kcal'], v['difference_kcal']), (2600, 700, -1900)); self.assertIn('partial', v['note'])

    def test_weight_trend_and_imported_measurements_are_upserted_by_source_id(self):
        for d, lb in (('2026-10-12', 182.0), ('2026-10-26', 180.5), ('2026-11-08', 179.0)):
            self.n.add_measurement({'kind': 'weight', 'value': lb, 'unit': 'lb', 'measured_at': d, 'source_id': 'hk:' + d}, source='apple_health')
        self.n.add_measurement({'kind': 'weight', 'value': 179.2, 'unit': 'lb', 'measured_at': '2026-11-08', 'source_id': 'hk:2026-11-08'}, source='apple_health')
        t = self.c('trends', {'days': 30, 'to': '2026-11-08'})['weight']
        self.assertEqual((t['measurements'], t['change_lb'], t['sources']), (3, -2.8, ['apple_health']))


class FoodSourceTests(Base):
    def test_search_cache_and_outages(self):
        r = self.c('food_search', {'query': 'rice cooked'})
        self.assertEqual([f['fdc_id'] for f in r['foods']], [169704])
        self.c('food', {'fdc_id': 169704}); n = len(self.fake.calls)
        self.assertEqual(self.c('food', {'fdc_id': 169704})['source'], 'cache'); self.assertEqual(len(self.fake.calls), n)
        self.fake.mode = '429'
        self.assertTrue(self.c('food_search', {'query': 'rice cooked'})['source'].startswith('cache'))
        self.assertEqual(self.c('food_search', {'query': 'tofu'})['error'], 'rate_limited')
        self.clock[0] += 40 * 86400
        self.assertIn('stale', self.c('food', {'fdc_id': 169704})['source'])
        self.fake.mode = 'garbage'
        self.assertEqual(self.c('food', {'fdc_id': 171287})['error'], 'corrupt')
        self.fake.mode = 'down'
        self.assertEqual(self.c('food_search', {'query': 'tofu'})['error'], 'unavailable')
        # Manual logging keeps working offline.
        self.assertEqual(self.c('log', {'entry_key': 'offline-0001', 'items': [{'name': 'Apple', 'quantity': 1, 'unit': 'serving', 'basis': 'manual', 'nutrients': {'energy_kcal': 95}}]})['entries'][0]['status'], 'confirmed')

    def test_missing_key_is_reported_and_unknown_portion_asks(self):
        n = Nutrition(self.h, FDC(None, key='', fetch=self.fake))
        self.assertEqual(n.call('health_nutrition_food_search', {'query': 'rice'})['error'], 'not_configured')
        e = self.c('log', {'entry_key': 'eggs-unknown-1', 'items': [{'fdc_id': 171287, 'quantity': 1, 'unit': 'tbsp'}]})['entries'][0]
        self.assertEqual(e['status'], 'draft'); self.assertIn('large', e['needs'])

    def test_the_api_key_never_appears_in_results(self):
        blob = json.dumps(self.c('food_search', {'query': 'egg'})) + json.dumps(self.c('food', {'fdc_id': 171287}))
        self.assertNotIn('synthetic-key', blob)


if __name__ == '__main__':
    unittest.main()
