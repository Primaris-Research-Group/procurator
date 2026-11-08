"""Dropdowns (Nov 4 CVS patient-info State field): label or value accepted, custom dropdowns allowed."""
import json
import tempfile
import unittest
from pathlib import Path
from broker import Broker
from task_policy import autonomous_decision
from test_broker import Provider, control

ORIGIN = 'https://www.cvs.com'
INFO = ORIGIN + '/scheduling/patient-info'
OPTIONS = [{'value': '', 'label': 'Select a state'}, {'value': 'GA', 'label': 'Georgia'}, {'value': 'WY', 'label': 'Wyoming'}]


def state_select(**extra):
    return control('e4', 'select-one', 'State') | {'tag': 'select', 'name': 'state', 'id': 'state', 'options': OPTIONS, 'hidden_native': True} | extra


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Provider(); self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', self.p)
        self.s = self.b.call('open', {'request_id': 'sel-open-1', 'url': 'practice'})
        with self.b.db() as db: db.execute('UPDATE sessions SET origin=?,url=?,practice=0 WHERE id=?', (ORIGIN, INFO, self.s['id']))
        self.p.snap.update({'url': INFO, 'title': 'Patient info'})

    def prepare(self, rid, ref, value):
        return self.b.call('prepare', {'id': self.s['id'], 'request_id': rid, 'steps': [{'kind': 'select', 'ref': ref, 'value': value}]})

    def test_native_select_accepts_label_or_value_and_stores_the_value(self):
        self.p.snap['controls'] = [state_select()]
        for rid, value in [('sel-label-1', 'Georgia'), ('sel-value-1', 'GA'), ('sel-case-1', '  georgia ')]:
            p = json.loads(self.b.plan(self.prepare(rid, 'e4', value)['id'])['plan'])
            self.assertEqual(p['steps'][0]['value'], 'GA')
        with self.assertRaisesRegex(ValueError, 'exact label'):
            self.prepare('sel-bad-1', 'e4', 'Atlantis')

    def test_custom_dropdowns_are_choosable_plain_buttons_are_not(self):
        combo = control('e5', '', 'State') | {'tag': 'div', 'role': 'combobox', 'haspopup': 'listbox', 'name': '', 'id': 'cb'}
        comp = control('e6', '', 'State') | {'tag': 'ps-select', 'custom_field': True, 'name': 'state', 'id': 'ws'}
        plain = control('e7', 'button', 'Continue') | {'tag': 'button', 'name': '', 'id': ''}
        self.p.snap['controls'] = [combo, comp, plain]
        for rid, ref in [('sel-combo-1', 'e5'), ('sel-comp-1', 'e6')]:
            p = json.loads(self.b.plan(self.prepare(rid, ref, 'Georgia')['id'])['plan'])
            self.assertEqual(p['steps'][0]['value'], 'Georgia')
        with self.assertRaisesRegex(ValueError, 'not a selection field'):
            self.prepare('sel-plain-1', 'e7', 'Georgia')

    def test_autonomous_state_choice_is_routine_and_owner_fact_matches_label(self):
        spec = {'mode': 'autonomous', 'origin': ORIGIN, 'facts': ['Georgia'], 'fields': [], 'options': []}
        plan = {'url': INFO, 'steps': [{'kind': 'select', 'control': state_select(), 'value': 'GA'}], 'page_errors': ['Select a state']}
        self.assertEqual(autonomous_decision(spec, plan), (True, 'routine'))


if __name__ == '__main__':
    unittest.main()
