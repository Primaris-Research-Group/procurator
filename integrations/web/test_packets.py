"""Task packet format v1 (Nov 6 2026): front matter, kinds, steps, journals, validation, credential scan."""
import os
import pathlib
import unittest
import packets

HERE = os.path.dirname(os.path.abspath(__file__))
BG = os.path.join(HERE, '..', '..', 'task-packets', 'legal-matter')
BUILDERS = [{'name': 'windows', 'online': True, 'repos': ['example-org/procurator-sandbox']}, {'name': 'mac', 'online': False, 'repos': []}]


def packet(extra='', body='', kind='administrative', steps='1. type: browse: fill the form; stop for my final submit.\n'):
    return {'00_START_HERE.md': '# Test packet\n**Revision:** T-2026-11-06-r1\n**Kind:** %s\n**Goal:** Do the thing.\n**Done when:**\n- The thing shows a confirmation\n%s\n## Facts\n- Date 11/10/2026\n\n## Steps\n%s%s' % (kind, extra, steps, body)}


class Tests(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir(BG), 'the legal-matter example packet is only on the Mac')
    def test_legal_matter_packet_is_accepted_and_understood(self):
        files = {n: pathlib.Path(BG, n).read_text(encoding='utf-8') for n in os.listdir(BG) if n.endswith('.md')}
        r = packets.validate(files)
        self.assertTrue(r['ok'], r['errors'])
        p = r['packet']
        self.assertEqual(p['revision'], 'BG-2026-11-05-r1')
        self.assertEqual(p['start'], '00_START_HERE.md')
        self.assertEqual(p['kind'], 'legal')
        self.assertEqual(p['kind_source'][:8], 'inferred')
        self.assertIn('recover', p['goal'].lower())
        self.assertEqual(len(p['done_when']), 4)
        self.assertEqual([t['thread'] for t in p['threads']], ['BG-CORE-r1', 'BG-NJ-r1', 'BG-RECOVERY-r1', 'BG-CONDUCT-r1'])
        self.assertEqual(len(p['threads'][1]['steps']), 8)
        self.assertEqual(p['steps'], [])  # "## Step 3 — …" prose sections are not typed steps
        self.assertEqual(p['cadence'], 240)  # inferred kind: default timing until the owner sets Kind or Cadence
        self.assertTrue(any('Kind' in w for w in r['warnings']))
        self.assertIn('rules', p)

    def test_short_packet_with_typed_steps(self):
        r = packets.validate(packet('**Sites:** leaveco\n**Budget:** $3\n', steps='1. type: browse: open the form.\n2. type: ask: ask me on a duplicate warning.\n'), saved_logins=['leaveco'])
        self.assertTrue(r['ok'], r['errors'])
        p = r['packet']
        self.assertEqual([s['type'] for s in p['steps']], ['browse', 'ask'])
        self.assertEqual(p['done_when'], ['The thing shows a confirmation'])
        self.assertEqual(p['budget_usd'], 3.0)
        self.assertEqual(p['sites'], ['leaveco'])
        self.assertEqual(p['kind_source'], 'stated')
        self.assertEqual(r['warnings'], [])

    def test_every_template_validates(self):
        for kind, text in packets.TEMPLATES.items():
            r = packets.validate({'00_START_HERE.md': text}, saved_logins=['leaveco'], builders=BUILDERS)
            self.assertTrue(r['ok'], (kind, r['errors']))
            self.assertEqual(r['packet']['kind'], kind)

    def test_refusals(self):
        def errors(files, **kw):
            r = packets.validate(files, **kw)
            self.assertFalse(r['ok'])
            self.assertIsNone(r['packet'])
            return ' '.join(r['errors'])
        self.assertIn('Kind must be', errors(packet(kind='magic')))
        self.assertIn('unknown type', errors(packet(steps='1. type: teleport: go.\n')))
        self.assertIn('Budget must be', errors(packet('**Budget:** $900\n')))
        self.assertIn('Cadence', errors(packet('**Cadence:** 10\n')))
        self.assertIn('Hours', errors(packet('**Hours:** 9-5\n')))
        self.assertIn('Model must be', errors(packet('**Model:** gpt-9\n')))
        self.assertIn('not saved', errors(packet('**Sites:** leaveco, chase\n'), saved_logins=['leaveco']))
        self.assertIn('needs **Repo:**', errors(packet(kind='coding', steps='1. type: build: do it.\n')))
        self.assertIn('type: build', errors(packet('**Repo:** a/b\n**Agent:** codex\n', kind='coding', steps='1. type: research: look.\n')))
        self.assertIn('not on the allowed list', errors(packet('**Repo:** a/b\n**Agent:** codex\n', kind='coding', steps='1. type: build: do it.\n'), builders=BUILDERS))
        self.assertIn('.md or .txt', errors({'x.exe': 'hi'}))
        self.assertIn('empty', errors({'x.md': '  '}))
        self.assertIn('1–25', errors({}))

    def test_offline_builder_is_a_warning_not_an_error(self):
        r = packets.validate(packet('**Repo:** a/b\n**Agent:** claude\n', kind='coding', steps='1. type: build: do it.\n'),
                             builders=[{'name': 'mac', 'online': False, 'repos': ['a/b']}])
        self.assertTrue(r['ok'], r['errors'])
        self.assertTrue(any('offline' in w for w in r['warnings']))

    def test_credential_scan_refuses_and_names_line_only(self):
        for kind, line in [('password', 'The password: hunter2222 goes here'), ('API key', 'key sk-abcdefghijklmnopqrstuvwxyz0123'),
                           ('API key', 'token ghp_abcdefghijklmnopqrstuvwxyz0123456789'), ('private key', '-----BEGIN RSA PRIVATE KEY-----'),
                           ('Social Security number', 'SSN 123-45-6789'), ('card number', 'Visa 4111 1111 1111 1111'), ('one-time code', 'verification code: 482913'),
                           ('bearer token', 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789')]:
            r = packets.validate(packet(body='\n## Notes\n' + line + '\n'))
            self.assertFalse(r['ok'], line)
            self.assertIn(kind, r['errors'][0])
            self.assertIn('line', r['errors'][0])
            for secret in ('hunter2222', 'sk-abc', 'ghp_', '123-45-6789', '4111', '482913', 'abcdefghijklmnop'):
                self.assertNotIn(secret, r['errors'][0])

    def test_credential_scan_ignores_phone_numbers_ids_and_fingerprints(self):
        text = ('Call 609-403-7800 ext. 34135 or 866-779-1054. Drive ID 1FDOEoJ1Ynuu_b1jVwaQXK9-qpqiMFBYN.\n'
                'Fingerprint dc8ddfe974258edcaf4a81c2dc3dc911b4bbb935c455ad2e5b68431453e919b4. Message 1793972419862.\n'
                'Passwords and one-time codes never go to the model. Card numbers and Social Security numbers are refused.\n')
        self.assertEqual(packets.credential_scan({'a.md': text}), [])

    def test_kind_inference_and_defaults(self):
        r = packets.validate({'a.md': '# Fix the parser\n**Revision:** P-1\n**Goal:** Fix it.\n\n## Context\nOpen a draft PR in the repo after the unit tests pass; use Codex.\n'})
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['packet']['kind'], 'coding')  # inferred kinds never fail the coding-only checks
        self.assertEqual(r['packet']['cadence'], 240)
        r = packets.validate({'a.md': '# Plain\n**Revision:** P-2\n\nBook it.\n'})
        self.assertEqual(r['packet']['kind'], 'administrative')
        self.assertEqual(r['packet']['goal'], 'Plain')
        self.assertTrue(any('using the title' in w for w in r['warnings']))
        self.assertTrue(any('plan its own threads' in w for w in r['warnings']))

    def test_front_matter_shapes(self):
        fm = packets.front_matter('# T\n\n**Revision:** R-1  \n**Done-when:** a; b\n**Owner:** me\n\nText\n\n## Section\n**Kind:** legal\n')
        self.assertEqual(fm, {'revision': 'R-1', 'done_when': 'a; b', 'owner': 'me'})
        self.assertEqual(packets.parse_hours('08:00-20:00 ET'), (8, 20))
        self.assertEqual(packets.parse_budget('$15'), 15.0)
        self.assertEqual(packets.parse_budget('15.50'), 15.5)


if __name__ == '__main__':
    unittest.main()
