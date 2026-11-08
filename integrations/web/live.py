"""Live frames (web 0.28.0, plan v2 B11): view-only, lane-independent, memory only.

  wanted(task)          the lane asks once a second whether anyone is watching; frames are captured ONLY while a viewer was seen in
                        the last 5 seconds (the last viewer leaving stops capture within 5 s)
  push(task, ...)       the lane sends the latest frame: JPEG bytes captured with the mask selectors (site_policy.MASK_SELECTORS)
                        for show/mask views, or a placeholder {domain, title, reason} for hide views (banks, health portals)
  frame(task, viewer)   the dashboard reads the latest frame (ETag); reading marks the viewer as present
  reveal                a masked or hidden view is revealed for one task and one dashboard session only after a Telegram
                        confirmation (/livereveal CODE); it is logged and ends with the task

Frames are never written to disk and never decoded here (the broker stays stdlib, no image code). Only the latest frame per task
is held; a finished task's frame is dropped.
"""
import hashlib
import secrets
import threading
import time

VIEWER_TTL = 5
MAX_FRAME = 400 * 1024


class Live:
    def __init__(self, now=time.time, telegram=None, log=None):
        self.now = now
        self.telegram = telegram or (lambda text: None)
        self.log = log or (lambda text: None)
        self.lock = threading.Lock()
        self.frames, self.viewers, self.reveals, self.codes = {}, {}, {}, {}

    def wanted(self, task):
        with self.lock:
            seen = {v: t for v, t in self.viewers.get(task, {}).items() if self.now() - t <= VIEWER_TTL}
            self.viewers[task] = seen
            return {'capture': bool(seen), 'viewers': len(seen), 'reveal': bool(self.reveals.get(task)), 'max_fps': 1}

    def push(self, task, jpeg=None, placeholder=None, url_host='', view='show'):
        if not self.wanted(task)['capture']:
            return {'stored': False, 'reason': 'no viewer'}
        if jpeg is not None:
            if not isinstance(jpeg, (bytes, bytearray)) or len(jpeg) > MAX_FRAME or jpeg[:3] != b'\xff\xd8\xff':
                raise ValueError('A frame is one JPEG under 400 KB.')
            if view == 'hide' and not self.reveals.get(task):
                raise ValueError('This page is hidden from Live (bank, payment or health portal): send a placeholder, not an image.')
        elif not isinstance(placeholder, dict):
            raise ValueError('Send a frame or a placeholder.')
        with self.lock:
            etag = hashlib.sha256(bytes(jpeg) if jpeg is not None else repr(sorted(placeholder.items())).encode()).hexdigest()[:16]
            self.frames[task] = {'jpeg': bytes(jpeg) if jpeg is not None else None,
                                 'placeholder': {k: str(placeholder.get(k, ''))[:120] for k in ('domain', 'title', 'reason')} if placeholder else None,
                                 'etag': etag, 'ts': self.now(), 'host': str(url_host)[:80], 'view': view}
        return {'stored': True, 'etag': etag}

    def frame(self, task, viewer):
        with self.lock:
            self.viewers.setdefault(task, {})[viewer] = self.now()
            return self.frames.get(task)

    def end(self, task):
        with self.lock:
            self.frames.pop(task, None)
            self.viewers.pop(task, None)
            if self.reveals.pop(task, None):
                self.log('Live reveal ended with task %s' % task)

    # ---------- reveal (Telegram-confirmed, one task, one session) ----------
    def reveal_request(self, task, session):
        code = secrets.token_hex(3)
        with self.lock:
            self.codes[code] = {'task': task, 'session': session, 'at': self.now()}
        self.telegram('LIVE REVEAL — task %s\nSomeone signed in to your dashboard asked to see the masked or hidden parts of this task\'s screen '
                      '(password fields, card fields, bank or health pages).\nAllow for this task only by sending:\n/livereveal %s\nIgnore it to keep them hidden.' % (task, code))
        return {'requested': True, 'note': 'Confirm in Telegram with the /livereveal line.'}

    def reveal_confirm(self, code):
        with self.lock:
            c = self.codes.pop(str(code), None)
            if not c or self.now() - c['at'] > 300:
                raise ValueError('Unknown or expired reveal code.')
            self.reveals[c['task']] = c['session']
        self.log('Live reveal allowed for task %s (one dashboard session)' % c['task'])
        return {'revealed': c['task']}

    def revealed_for(self, task, session):
        return self.reveals.get(task) == session
