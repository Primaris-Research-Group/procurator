"""Web compatibility battery (0.19.1): the 15 case files are complete and gradeable, and the judge checks the answer too."""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import regression

class CompatSetTests(unittest.TestCase):
    def setUp(self):
        self.cases = regression.errands('compat')

    def test_fifteen_read_only_cases_with_everything_a_run_needs(self):
        self.assertEqual(len(self.cases), 15)
        self.assertEqual([c['id'] for c in self.cases], ['W%02d' % i for i in range(1, 16)])
        self.assertEqual(len({c['name'] for c in self.cases}), 15)
        for c in self.cases:
            self.assertTrue(c['intent'].startswith('Read-only research task, in a fresh browser visit'), c['id'])
            self.assertTrue(c['start_url'].startswith('https://'), c['id'])
            self.assertIn(c['lane_default'], ('cloud', 'home'), c['id'])
            self.assertTrue(c['key_nodes'] and all(n['evidence_any'] for n in c['key_nodes']), c['id'])
            self.assertTrue(c['final_state']['evidence_any'], c['id'])
            for p in c['answer_check']['patterns']:
                re.compile(p)
            self.assertEqual(c['budget'], {'steps': 25, 'minutes': 8, 'on_demand_looks': 2}); self.assertIn('cloud fallback never counts', c['grading'])
            self.assertIn('final/commit/protected', c['live_stops_before'])

    def test_default_lanes_match_the_broker(self):
        from lanes import HOME_DEFAULT
        for c in self.cases:
            self.assertEqual(c['lane_default'], 'home' if HOME_DEFAULT.search(c['site']) else 'cloud', c['id'])

    def test_judge_needs_the_page_and_a_supported_answer(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        snap = Path(tmp.name, 'snap.json'); good = Path(tmp.name, 'good.txt'); bad = Path(tmp.name, 'bad.txt')
        snap.write_text(json.dumps({'text': 'Option 2\nHello World!\nYour content goes here.\nLet\'s have some different text!\n12/25/2026'}))
        good.write_text('Option 2 selected; Hello World!; editor: Your content goes here.; shadow: Let\'s have some different text!; date 12/25/2026')
        w01 = next(c for c in self.cases if c['id'] == 'W01')
        self.assertFalse(regression.answer_ok(w01, 'Population 206,922; area code 706.'))        # QA: both area codes required
        self.assertTrue(regression.answer_ok(w01, 'Population 206,922; area codes 706 and 762.'))
        bad.write_text('Option 2 selected; the dynamic text never appeared.')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(regression.judge('W15', snap, key='', setname='compat', answer_path=good), 0)
            self.assertEqual(regression.judge('W15', snap, key='', setname='compat', answer_path=bad), 1)
            self.assertEqual(regression.judge('w15-widget-gym', snap, key='', setname='compat'), 0)

    def test_the_model_judge_cannot_pass_an_answer_the_patterns_reject(self):
        e = next(c for c in self.cases if c['id'] == 'W11')
        calls = []
        def fake(errand, text, key, model, max_tokens, answer=None):
            calls.append(model)
            return {'nodes': [{'id': n['id'], 'pass': True} for n in errand['key_nodes']], 'final': True,
                    'answer_supported': True and regression.answer_ok(errand, answer), 'judge': model}
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        snap = Path(tmp.name, 's.json'); snap.write_text(json.dumps({'text': 'Complete reservation'}))
        ans = Path(tmp.name, 'a.txt'); ans.write_text('I booked it for you.')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(regression.judge('W11', snap, key='k', setname='compat', answer_path=ans, caller=fake), 1)
        self.assertEqual(calls, ['claude-haiku-5-5'])

    def test_live_lists_the_battery_and_the_set_flag_is_parsed(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(regression.main(['live', '--set', 'compat']), 0)
        self.assertEqual(len(re.findall(r'^W\d\d ', out.getvalue(), re.M)), 15)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(regression.main(['live', '--set', 'nope']), 2)

if __name__ == '__main__':
    unittest.main()
