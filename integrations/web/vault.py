"""Owner facts vault (web 0.18.0, PROCURATOR browsing plan Phase 4, §7.1).

/var/lib/procurator-web/owner-facts.json (mode 600), edited only on the server with bin/set-owner-fact.sh KEY (typed hidden).
The assistant sees the key names, never the values. A form entry may be "{{owner.phone}}": the broker swaps the value
in just before the driver types it. Reviews show it masked ("your saved owner.phone, ending 0100"), and every
string going back to the assistant has the value replaced by its placeholder again. Traces and page archives are
scrubbed of the values. SSNs, card numbers, CVVs, passwords and one-time codes are refused as keys and as values.

  python3 vault.py set KEY        (reads the value from stdin; used by bin/set-owner-fact.sh)
  python3 vault.py keys           (list key names)
  python3 vault.py remove KEY
"""
import json
import os
import re
import sys
from pathlib import Path

KEY = re.compile(r'^owner\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$')
REFUSED_KEY = re.compile(r'ssn|social|tax_?id|card|cvv|cvc|password|passcode|passwd|(?:^|[._])pin$|otp|one_?time|(?:verification|security|access|auth)_?code|secret|token|routing|account_?number|bank', re.I)
PLACEHOLDER = re.compile(r'\{\{\s*(owner\.[a-z0-9_.]+)\s*\}\}')
SUGGESTED = ['owner.name.first', 'owner.name.last', 'owner.phone', 'owner.email', 'owner.dob', 'owner.address.street', 'owner.address.unit',
             'owner.address.city', 'owner.address.state', 'owner.address.zip', 'owner.employer.name', 'owner.insurance.carrier',
             'owner.insurance.member_id', 'owner.insurance.group', 'owner.leave.case_id']

def value_refused(value):
    from task_policy import SENSITIVE, CARD_LIKE, SSN_LIKE
    v = str(value or '')
    return not v.strip() or len(v) > 200 or bool(SSN_LIKE.search(v) or CARD_LIKE.search(v) or SENSITIVE.search(v)) or any(ord(c) < 32 for c in v)

class Vault:
    def __init__(self, state):
        self.path = Path(state) / 'owner-facts.json'

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
            return {k: v for k, v in data.items() if KEY.match(k) and isinstance(v, str)}
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def keys(self):
        return sorted(self._read())

    def set(self, key, value):
        if not KEY.match(key or ''):
            raise ValueError('Keys look like owner.phone or owner.address.zip (lowercase, dots).')
        if REFUSED_KEY.search(key):
            raise ValueError('That kind of value (SSN, card, CVV, password, PIN, code, bank account) is never stored; the owner types those themselves.')
        if value_refused(value):
            raise ValueError('That value looks like a credential, code, card or SSN number (or is empty/too long); it is not stored.')
        data = self._read()
        data[key] = str(value).strip()
        self._write(data)

    def remove(self, key):
        data = self._read()
        data.pop(key, None)
        self._write(data)

    # ---------- use at fill time ----------
    def placeholders(self, text):
        return PLACEHOLDER.findall(text or '')

    def check(self, text):
        """Unknown keys raise; returns True when the value is only owner-fact placeholders (and spacing/punctuation)."""
        keys = self.placeholders(text)
        if not keys:
            return False
        known = self._read()
        missing = [k for k in keys if k not in known]
        if missing:
            raise ValueError('No saved owner fact named %s. Saved keys: %s. The owner adds one on the server with bin/set-owner-fact.sh KEY.' % (', '.join(missing), ', '.join(sorted(known)) or 'none yet'))
        return not re.sub(r'[\s,./()\-#]', '', PLACEHOLDER.sub('', text))

    def substitute(self, text):
        known = self._read()
        return PLACEHOLDER.sub(lambda m: known[m.group(1)] if m.group(1) in known else m.group(0), text or '')

    def pairs(self):
        """(value, placeholder) for redaction, longest values first; very short values (e.g. '4') are not redacted."""
        return sorted(((v, '{{' + k + '}}') for k, v in self._read().items() if len(v.strip()) >= 4), key=lambda p: -len(p[0]))

    def redact(self, value, depth=0):
        """Every string in a structure with saved values replaced by their placeholders (model-facing output)."""
        pairs = self.pairs()
        if not pairs:
            return value
        def walk(v, d):
            if isinstance(v, str):
                for raw, ph in pairs:
                    if raw in v:
                        v = v.replace(raw, ph)
                return v
            if d > 12:
                return v
            if isinstance(v, dict):
                return {k: walk(x, d + 1) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x, d + 1) for x in v]
            return v
        return walk(value, depth)

    def mask(self, text):
        """For Telegram reviews and the activity log: placeholders and raw values shown as 'saved owner.phone (…0100)'."""
        known = self._read()
        def tail(v):
            v = re.sub(r'\s', '', v)
            return '…' + v[-4:] if len(v) > 6 else '••'
        out = PLACEHOLDER.sub(lambda m: 'saved ' + m.group(1) + (' (' + tail(known[m.group(1)]) + ')' if m.group(1) in known else ''), text or '')
        for raw, ph in self.pairs():
            out = out.replace(raw, 'saved ' + ph[2:-2] + ' (' + tail(raw) + ')')
        return out

    def scrub_bytes(self, data):
        for raw, ph in self.pairs():
            data = data.replace(raw.encode(), ph.encode())
        return data

    def scrub_file(self, path):
        """Traces (zip) and archives (MHTML): remove saved values. Zip entries are rewritten (deflate)."""
        import zipfile
        path = Path(path)
        if not self.pairs() or not path.is_file():
            return
        if path.suffix == '.zip':
            tmp = path.with_suffix('.scrub')
            with zipfile.ZipFile(path) as zin, zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    zout.writestr(item, self.scrub_bytes(zin.read(item.filename)))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        else:
            path.write_bytes(self.scrub_bytes(path.read_bytes()))

def main(argv):
    state = os.environ.get('PROCURATOR_WEB_STATE', '/var/lib/procurator-web')
    v = Vault(state)
    if argv[:1] == ['keys']:
        print('\n'.join(v.keys()) or '(no owner facts saved yet)')
        print('\nSuggested keys: ' + ', '.join(SUGGESTED))
        return 0
    if len(argv) == 2 and argv[0] == 'set':
        value = sys.stdin.readline().rstrip('\n')
        try:
            v.set(argv[1], value)
        except ValueError as e:
            print('Not saved: ' + str(e))
            return 1
        print('Saved ' + argv[1] + ' (value hidden). The assistant sees only the key name.')
        return 0
    if len(argv) == 2 and argv[0] == 'remove':
        v.remove(argv[1])
        print('Removed ' + argv[1])
        return 0
    print(__doc__)
    return 2

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main(sys.argv[1:]))
