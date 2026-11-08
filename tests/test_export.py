"""bin/export_release.py: a fixture tree with a fake key, an owner literal, a diagnostics file and a renamed service comes out
clean, patched and renamed, or the export fails on the denylist gate."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / 'bin' / 'export_release.py'


def run(args, cwd):
    return subprocess.run([sys.executable, str(EXPORTER)] + args, cwd=cwd, capture_output=True, text=True)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.src = Path(self.td.name) / 'src'
        (self.src / 'integrations' / 'web').mkdir(parents=True)
        (self.src / 'integrations' / 'web' / 'broker.py').write_text("OWNER='123456789'\nNAME='Jane Owner'\nX = 1\n")
        (self.src / 'integrations' / 'web' / 'acme-web.service').write_text('[Unit]\nDescription=acme-web\n')
        (self.src / 'diag.txt').write_text('root@198.18.0.7 secret-ish\n')
        (self.src / 'vendor').mkdir()
        (self.src / 'vendor' / 'key.env').write_text('API_KEY=abcd1234\n')
        (self.src / 'release' / 'overlay').mkdir(parents=True)
        (self.src / 'release' / 'overlay' / 'README.md').write_text('# Product\n')
        (self.src / 'release' / 'deny.txt').write_text('Jane Owner\n123456789\n/acme-/\n')
        self.rules = {
            'allow': ['integrations/**'], 'exclude': ['**/*.pyc'],
            'rename': [['acme-', 'product-']],
            'patches': [{'file': 'integrations/web/broker.py', 'find': "OWNER='123456789'", 'replace': 'OWNER=owner_id()'}],
            'rewrites': [{'label': 'owner name', 'pattern': 'Jane Owner', 'repl': 'the owner', 'regex': False},
                         {'label': 'acme', 'pattern': 'acme-', 'repl': 'product-'}],
            'overlay': 'release/overlay', 'denylist': 'release/deny.txt', 'post': [],
        }
        (self.src / 'release' / 'rules.json').write_text(json.dumps(self.rules))
        self.out = Path(self.td.name) / 'out'

    def tearDown(self):
        self.td.cleanup()

    def test_clean_export(self):
        r = run(['--src', str(self.src), '--out', str(self.out), '--rules', str(self.src / 'release' / 'rules.json')], self.src)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.out / 'diag.txt').exists())
        self.assertFalse((self.out / 'vendor').exists())
        self.assertFalse((self.out / 'release').exists())
        self.assertTrue((self.out / 'README.md').exists())
        self.assertTrue((self.out / 'integrations' / 'web' / 'product-web.service').exists())
        text = (self.out / 'integrations' / 'web' / 'broker.py').read_text()
        self.assertIn('OWNER=owner_id()', text)
        self.assertIn("NAME='the owner'", text)
        self.assertNotIn('123456789', text)
        self.assertIn('Description=product-web', (self.out / 'integrations' / 'web' / 'product-web.service').read_text())

    def test_denylist_hit_fails(self):
        self.rules['rewrites'] = []   # leave the owner's name in place
        (self.src / 'release' / 'rules.json').write_text(json.dumps(self.rules))
        r = run(['--src', str(self.src), '--out', str(self.out), '--rules', str(self.src / 'release' / 'rules.json')], self.src)
        self.assertEqual(r.returncode, 1)
        self.assertIn('DENYLIST', r.stderr)

    def test_patch_must_match_once(self):
        self.rules['patches'][0]['find'] = 'NOT THERE'
        (self.src / 'release' / 'rules.json').write_text(json.dumps(self.rules))
        r = run(['--src', str(self.src), '--out', str(self.out), '--rules', str(self.src / 'release' / 'rules.json')], self.src)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('expected 1 match', r.stderr)


if __name__ == '__main__':
    unittest.main()
