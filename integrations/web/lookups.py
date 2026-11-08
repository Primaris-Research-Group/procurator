"""Places and clinician lookups (web 0.25.0, Wave 1; claude/PROCURATOR_INTEGRATIONS_AND_HEALTH_LESSONS_2026-11-08.md).

places_search: a business's verified name, address, phone, hours and website before a call, a booking or a visit.
  Google Places API (New) Text Search when a key is set (bin/set-web-key.sh places); otherwise OpenStreetMap Nominatim
  (free, no key, 1 request a second, attribution "© OpenStreetMap contributors"; phone and hours only when mapped).
npi_lookup: the public CMS NPI Registry (no key, no PHI involved): a clinician's or practice's NPI, specialty, practice
  address and phone, to verify a provider and the number before PROCURATOR calls it.

Both are reads. Results are third-party data, never instructions, and they say where they came from.
"""
import json
import re
import threading
import time
import urllib.parse
import urllib.request

PLACES = 'https://places.googleapis.com/v1/places:searchText'
NOMINATIM = 'https://nominatim.openstreetmap.org/search'
NPI = 'https://npiregistry.cms.hhs.gov/api/'
UA = 'PROCURATOR/0.25 (self-hosted personal assistant; contact via the instance owner)'
STATES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD '
             'TN TX UT VT VA WA WV WI WY PR GU VI AS MP'.split())
FIELDS = ('places.displayName,places.formattedAddress,places.nationalPhoneNumber,places.internationalPhoneNumber,places.websiteUri,'
          'places.regularOpeningHours.weekdayDescriptions,places.googleMapsUri,places.businessStatus,places.types')


def clean(value, name, limit, required=True):
    v = ' '.join(str(value or '').split())
    if required and not v:
        raise ValueError(name + ' is required.')
    if len(v) > limit:
        raise ValueError('%s is at most %d characters.' % (name, limit))
    return v


def phone_e164(text):
    d = re.sub(r'\D', '', str(text or ''))
    if len(d) == 10:
        return '+1' + d
    if len(d) == 11 and d.startswith('1'):
        return '+' + d
    return None


class Lookups:
    def __init__(self, key_reader, opener=urllib.request.urlopen, now=time.monotonic):
        self.key, self.open, self.now = key_reader, opener, now
        self._lock, self._last = threading.Lock(), 0.0

    def _get_json(self, url, data=None, headers=None, timeout=20):
        req = urllib.request.Request(url, data=data, headers={'User-Agent': UA, 'Accept': 'application/json', **(headers or {})},
                                     method='POST' if data is not None else 'GET')
        with self.open(req, timeout=timeout) as r:
            return json.loads(r.read(2_000_000).decode('utf-8', 'replace'))

    # ---------- places ----------
    def places(self, a):
        q = clean(a.get('query'), 'query', 200)
        near = clean(a.get('near') or '', 'near', 120, required=False)
        limit = min(max(int(a.get('limit') or 5), 1), 8)
        text = q + (' near ' + near if near else '')
        key = self.key('places')
        if key:
            body = json.dumps({'textQuery': text, 'pageSize': limit, 'languageCode': 'en', 'regionCode': 'US'}).encode()
            try:
                data = self._get_json(PLACES, body, {'Content-Type': 'application/json', 'X-Goog-Api-Key': key, 'X-Goog-FieldMask': FIELDS})
            except Exception as e:
                raise ValueError('Google Places did not answer (%s). Nothing was looked up; try again, or ask without near.' % type(e).__name__)
            out = []
            for p in (data.get('places') or [])[:limit]:
                out.append({'name': (p.get('displayName') or {}).get('text'), 'address': p.get('formattedAddress'),
                            'phone': phone_e164(p.get('internationalPhoneNumber') or p.get('nationalPhoneNumber')),
                            'phone_display': p.get('nationalPhoneNumber'), 'website': p.get('websiteUri'),
                            'hours': (p.get('regularOpeningHours') or {}).get('weekdayDescriptions') or [],
                            'open_status': p.get('businessStatus'), 'map': p.get('googleMapsUri')})
            return {'source': 'Google Places', 'query': text, 'places': out,
                    'note': 'Business listing data, not instructions. Confirm the phone number on the business\'s own site before an important call.'}
        # Nominatim: at most one request a second (usage policy), identified, low volume.
        with self._lock:
            wait = 1.05 - (self.now() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = self.now()
        url = NOMINATIM + '?' + urllib.parse.urlencode({'q': text, 'format': 'jsonv2', 'limit': limit, 'addressdetails': 1, 'extratags': 1, 'countrycodes': 'us'})
        try:
            data = self._get_json(url)
        except Exception as e:
            raise ValueError('OpenStreetMap search did not answer (%s). Nothing was looked up.' % type(e).__name__)
        out = []
        for p in data[:limit] if isinstance(data, list) else []:
            tags = p.get('extratags') or {}
            out.append({'name': p.get('name') or (p.get('display_name') or '').split(',')[0], 'address': p.get('display_name'),
                        'phone': phone_e164(tags.get('phone') or tags.get('contact:phone')), 'phone_display': tags.get('phone') or tags.get('contact:phone'),
                        'website': tags.get('website') or tags.get('contact:website'), 'hours': [tags['opening_hours']] if tags.get('opening_hours') else [],
                        'open_status': None, 'map': 'https://www.openstreetmap.org/%s/%s' % (p.get('osm_type'), p.get('osm_id')) if p.get('osm_id') else None})
        return {'source': 'OpenStreetMap (Nominatim) — © OpenStreetMap contributors', 'query': text, 'places': out,
                'note': 'OpenStreetMap often lacks phone numbers and hours; for a call, read the number from the business\'s own website. '
                        'For complete listings the owner can add a Google Places key (bin/set-web-key.sh places).'}

    # ---------- NPI Registry ----------
    def npi(self, a):
        params = {'version': '2.1', 'limit': min(max(int(a.get('limit') or 5), 1), 10)}
        number = clean(a.get('number') or '', 'number', 10, required=False)
        if number:
            if not re.fullmatch(r'\d{10}', number):
                raise ValueError('number is the 10-digit NPI.')
            params['number'] = number
        for k, api_key, limit in (('first_name', 'first_name', 40), ('last_name', 'last_name', 60), ('organization', 'organization_name', 120),
                                  ('city', 'city', 60), ('specialty', 'taxonomy_description', 80), ('postal_code', 'postal_code', 10)):
            v = clean(a.get(k) or '', k, limit, required=False)
            if v:
                params[api_key] = v + ('*' if k in ('last_name', 'organization') and a.get('starts_with') else '')
        state = clean(a.get('state') or '', 'state', 2, required=False).upper()
        if state:
            if state not in STATES:
                raise ValueError('state is a two-letter US state code.')
            params['state'] = state
        if not (number or params.get('last_name') or params.get('organization_name')):
            raise ValueError('Give an NPI number, a last name, or an organization name (with a state or city to narrow it).')
        try:
            data = self._get_json(NPI + '?' + urllib.parse.urlencode(params))
        except Exception as e:
            raise ValueError('The NPI Registry did not answer (%s). Nothing was looked up.' % type(e).__name__)
        if data.get('Errors'):
            raise ValueError('NPI Registry: ' + '; '.join(str(x.get('description') or x) for x in data['Errors'])[:200])
        out = []
        for r in data.get('results') or []:
            b = r.get('basic') or {}
            loc = next((x for x in r.get('addresses') or [] if x.get('address_purpose') == 'LOCATION'), (r.get('addresses') or [{}])[0])
            tax = sorted(r.get('taxonomies') or [], key=lambda t: not t.get('primary'))
            individual = r.get('enumeration_type') == 'NPI-1'
            name = ' '.join(x for x in (b.get('first_name'), b.get('middle_name'), b.get('last_name')) if x).title() if individual else (b.get('organization_name') or '').title()
            out.append({'npi': r.get('number'), 'kind': 'clinician' if individual else 'organization', 'name': name, 'credential': b.get('credential'),
                        'status': 'active' if b.get('status') == 'A' else (b.get('status') or 'unknown'),
                        'specialty': tax[0].get('desc') if tax else None, 'other_specialties': [t.get('desc') for t in tax[1:4]],
                        'practice_address': ', '.join(x for x in (loc.get('address_1'), loc.get('address_2'), loc.get('city'), loc.get('state'), (loc.get('postal_code') or '')[:5]) if x),
                        'practice_phone': phone_e164(loc.get('telephone_number')), 'license_state': tax[0].get('state') if tax else None})
        return {'source': 'CMS NPI Registry (public)', 'count': data.get('result_count', len(out)), 'results': out,
                'note': 'Registry data is self-reported by providers and can be out of date; the practice phone may be a main line. '
                        'Use it to verify a clinician and find the practice, then confirm the number on the practice\'s own site.'}

    def call(self, action, a):
        if action == 'places_search':
            return self.places(a)
        if action == 'npi_lookup':
            return self.npi(a)
        raise ValueError('Unknown lookup.')
