"""One device registry (web 0.28.0, plan v2 B8): everything connected to the box in one list and one API (GET /api/v1/devices).

  {id, kind, name, provider, room, capabilities, safety_tier, status, last_seen}
  kind  builder | browser_lane | companion | home_device | health_device | dashboard_session

Sources: builders (dashboard), browser lanes and the Mac companion (lanes.py machines), Apple Health devices (health_sync),
dashboard sessions, home devices (home bridge). Device and room names are untrusted strings: the dashboard escapes them and the
model only sees them inside <untrusted source="device"> fences.
"""
import time

KINDS = ('builder', 'browser_lane', 'companion', 'home_device', 'health_device', 'dashboard_session')


def _ago(t, now):
    return round(now - t) if isinstance(t, (int, float)) and t else None


class Devices:
    def __init__(self, broker, now=time.time):
        self.b, self.now = broker, now

    def _safe(self, fn):
        try:
            return fn() or []
        except Exception:
            return []

    def list(self):
        now, out = self.now(), []
        dash = getattr(self.b, 'dashboard', None)
        for x in self._safe(lambda: dash.builders() if dash else []):
            out.append({'id': 'builder:' + str(x.get('name') or x.get('label')), 'kind': 'builder', 'name': x.get('label') or x.get('name'), 'provider': x.get('os') or 'builder',
                        'room': None, 'capabilities': x.get('agents') or [], 'safety_tier': None, 'status': 'online' if x.get('online') else 'offline',
                        'last_seen': x.get('last_seen')})
        for m in self._safe(lambda: self.b.lanes.machines()):
            mac = 'mac' in str(m.get('label') or m.get('name') or '').lower() or m.get('os') == 'mac'
            out.append({'id': 'lane:' + str(m.get('name')), 'kind': 'companion' if mac else 'browser_lane', 'name': m.get('label') or m.get('name'),
                        'provider': 'home lane', 'room': None, 'capabilities': ['browser'] + (['builder'] if mac else []), 'safety_tier': None,
                        'status': 'online' if m.get('online') else 'offline', 'last_seen_s': m.get('last_seen_s')})
        out.append({'id': 'lane:cloud', 'kind': 'browser_lane', 'name': 'Cloud browser (Browserbase)', 'provider': 'browserbase', 'room': None,
                    'capabilities': ['browser'], 'safety_tier': None, 'status': 'available', 'last_seen': None})
        hs = getattr(self.b, 'health_sync', None)
        for d in self._safe(lambda: hs.call('health_sync_devices', {}, True).get('devices') if hs else []):
            out.append({'id': 'health:' + str(d.get('id')), 'kind': 'health_device', 'name': d.get('name') or 'Apple Health', 'provider': 'apple-health', 'room': None,
                        'capabilities': d.get('types') or [], 'safety_tier': None, 'status': d.get('status') or 'paired', 'last_seen': d.get('last_sync')})
        for s in self._safe(lambda: dash.sessions.devices() if dash else []):
            out.append({'id': 'session:' + s['id'], 'kind': 'dashboard_session', 'name': s['device'], 'provider': 'dashboard', 'room': None,
                        'capabilities': ['view'], 'safety_tier': None, 'status': 'signed in', 'last_seen': s.get('last_seen')})
        home = getattr(self.b, '_home', None)
        if home and home.adapter:
            from home_policy import KIND_TIER
            for d in self._safe(home.adapter.devices):
                out.append({'id': 'home:' + d['id'], 'kind': 'home_device', 'name': d['name'], 'provider': 'home bridge', 'room': d.get('room'),
                            'capabilities': [d['kind']], 'safety_tier': KIND_TIER.get(d['kind']), 'status': 'reachable', 'last_seen': None})
        return {'devices': out, 'counts': {k: sum(1 for d in out if d['kind'] == k) for k in KINDS}, 'untrusted_fields': ['name', 'room']}
