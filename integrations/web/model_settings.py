"""Owner's model choices outside the main chat (web 0.24.0).

The main chat's model is switched with /ai (models.mjs writes it into the gateway config). Two more choices live here,
switched with /ai composer MODEL and /ai packets MODEL in Telegram or on the dashboard's Settings tab:

  composer  the Packet Composer (default sonnet). A session can also pick its own model on its page.
  packets   packet work sessions for packets WITHOUT their own **Model:** line (default main = whatever /ai chose for the chat).
            A packet's own Model line (or /mission model NAME …) always wins.

Values: sonnet | opus | sol | terra | main. Stored as a small JSON file next to the broker database; no secrets.
"""
import json
import os
import threading
from pathlib import Path

CHOICES = ('sonnet', 'opus', 'sol', 'terra', 'main')
DEFAULTS = {'composer': 'sonnet', 'packets': 'main'}
LABELS = {'sonnet': 'Claude Sonnet 5.5', 'opus': 'Claude Opus 5.5', 'sol': 'GPT-6.1 Sol', 'terra': 'GPT-6.1 Terra', 'main': 'same as the chat (/ai)'}
REFS = {'sonnet': 'anthropic/claude-sonnet-5-5', 'opus': 'anthropic/claude-opus-5-5', 'sol': 'openai/gpt-6.1-sol', 'terra': 'openai/gpt-6.1-terra'}


def name_of(ref):
    return next((k for k, v in REFS.items() if v == ref), None)


class ModelSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def read(self):
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, scope):
        v = self.read().get(scope)
        return v if v in CHOICES else DEFAULTS[scope]

    def all(self):
        return {s: self.get(s) for s in DEFAULTS}

    def set(self, scope, value):
        if scope not in DEFAULTS:
            raise ValueError('Model settings exist for: %s.' % ', '.join(DEFAULTS))
        v = str(value or '').strip().lower()
        if v not in CHOICES:
            raise ValueError('Choose one of: %s.' % ', '.join(CHOICES))
        with self.lock:
            d = self.read()
            d[scope] = v
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(d))
            os.replace(tmp, self.path)
        return self.all()

    def describe(self):
        a = self.all()
        return {'composer': {'model': a['composer'], 'label': LABELS[a['composer']]}, 'packets': {'model': a['packets'], 'label': LABELS[a['packets']]},
                'choices': {k: LABELS[k] for k in CHOICES}}
