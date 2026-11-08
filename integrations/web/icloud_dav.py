"""Minimal CalDAV / CardDAV client for iCloud (and any RFC 4791 / RFC 6352 server), standard library only (web 0.29.0).

Apple publishes no REST API for Calendar or Contacts. iCloud speaks CalDAV at https://caldav.icloud.com and CardDAV at
https://contacts.icloud.com, authenticated with the Apple Account email plus an **app-specific password** (never the account
password; Apple revokes every app-specific password when the account password changes, so a 401 after months of working
means "ask the owner to reconnect", not "retry").

Discovery is the two-step dance every client does:
  1. PROPFIND /            Depth 0   -> DAV:current-user-principal
  2. PROPFIND <principal>  Depth 0   -> calendar-home-set (CalDAV) or addressbook-home-set (CardDAV); iCloud answers with an
                                        absolute URL on a numbered host (pNN-caldav.icloud.com), which must be followed as given
  3. PROPFIND <home>       Depth 1   -> the calendars / address books

Events come back from a REPORT calendar-query with a time range; contacts from a REPORT addressbook-query. The iCalendar and vCard
parsers here are deliberately small: SUMMARY, DTSTART, DTEND, LOCATION, UID, DESCRIPTION for events; FN, TEL, EMAIL, ORG for
contacts. Anything the owner's assistant needs beyond that is a later, tested extension, not an ad-hoc regex.

Nothing in this module logs, prints or returns the password. The password lives in the Accounts secret file and reaches this
module only as a constructor argument.
"""
import base64
import datetime as dt
import http.client
import re
import ssl
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

NS = {'d': 'DAV:', 'c': 'urn:ietf:params:xml:ns:caldav', 'card': 'urn:ietf:params:xml:ns:carddav', 'cs': 'http://calendarserver.org/ns/',
      'ical': 'http://apple.com/ns/ical/'}
ICLOUD_CALDAV = 'https://caldav.icloud.com'
ICLOUD_CARDDAV = 'https://contacts.icloud.com'
TIMEOUT = 20
MAX_BODY = 8 * 1024 * 1024


class DAVError(Exception):
    """A human-readable failure. The message never contains credentials."""


class DAVClient:
    def __init__(self, base_url, username, password, timeout=TIMEOUT, context=None):
        self.base = base_url.rstrip('/')
        self.auth = 'Basic ' + base64.b64encode(('%s:%s' % (username, password)).encode()).decode()
        self.timeout = timeout
        self.context = context or ssl.create_default_context()

    # ---------- transport ----------
    def request(self, method, url, body=None, depth=None, headers=None, ok=(200, 201, 204, 207)):
        """One HTTP request. Absolute URLs (iCloud's numbered hosts) are followed; relative ones hang off the base."""
        if not url.startswith('http'):
            url = urllib.parse.urljoin(self.base + '/', url)
        u = urllib.parse.urlsplit(url)
        conn = (http.client.HTTPSConnection(u.netloc, timeout=self.timeout, context=self.context) if u.scheme == 'https'
                else http.client.HTTPConnection(u.netloc, timeout=self.timeout))
        h = {'Authorization': self.auth, 'User-Agent': 'PROCURATOR-DAV/1.0', 'Accept': '*/*'}
        if depth is not None:
            h['Depth'] = str(depth)
        if body is not None:
            h['Content-Type'] = 'application/xml; charset=utf-8' if method in ('PROPFIND', 'REPORT') else 'text/calendar; charset=utf-8'
            body = body.encode() if isinstance(body, str) else body
        h.update(headers or {})
        try:
            conn.request(method, u.path + (('?' + u.query) if u.query else ''), body=body, headers=h)
            r = conn.getresponse()
            data = r.read(MAX_BODY)
        except (OSError, http.client.HTTPException) as e:
            raise DAVError('Could not reach %s (%s).' % (u.netloc, e.__class__.__name__)) from None
        finally:
            conn.close()
        if r.status == 401:
            raise DAVError('%s did not accept the sign-in. Use an app-specific password, not the account password.' % u.netloc)
        if r.status in (301, 302, 307, 308) and r.getheader('Location'):
            return self.request(method, r.getheader('Location'), body, depth, headers, ok)
        if r.status not in ok:
            raise DAVError('%s answered HTTP %d to %s.' % (u.netloc, r.status, method))
        return r.status, dict(r.getheaders()), data, url

    def propfind(self, url, props_xml, depth=0):
        body = '<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" ' \
               'xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/" xmlns:ical="http://apple.com/ns/ical/">' \
               '<d:prop>%s</d:prop></d:propfind>' % props_xml
        status, headers, data, final = self.request('PROPFIND', url, body, depth=depth)
        return parse_multistatus(data), final

    # ---------- discovery ----------
    def principal(self):
        responses, final = self.propfind('/', '<d:current-user-principal/>')
        for r in responses:
            href = r['props'].get('current-user-principal')
            if href:
                return urllib.parse.urljoin(final, href)
        raise DAVError('The server did not name a principal for this account.')

    def home(self, kind):
        """kind: 'calendar' or 'addressbook'. Returns the absolute home URL."""
        prop = '<c:calendar-home-set/>' if kind == 'calendar' else '<card:addressbook-home-set/>'
        key = 'calendar-home-set' if kind == 'calendar' else 'addressbook-home-set'
        principal = self.principal()
        responses, final = self.propfind(principal, prop)
        for r in responses:
            href = r['props'].get(key)
            if href:
                return urllib.parse.urljoin(final, href)
        raise DAVError('The server did not report a %s home for this account.' % kind)

    # ---------- CalDAV ----------
    def calendars(self):
        home = self.home('calendar')
        responses, final = self.propfind(home, '<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/><ical:calendar-color/><cs:getctag/>', depth=1)
        out = []
        for r in responses:
            if not r['props'].get('is_calendar'):
                continue
            comps = r['props'].get('components') or []
            if comps and 'VEVENT' not in comps:
                continue  # Reminders lists (VTODO) are not calendars for us; Apple's Reminders no longer speaks CalDAV anyway.
            out.append({'url': urllib.parse.urljoin(final, r['href']), 'name': r['props'].get('displayname') or r['href'].rstrip('/').rsplit('/', 1)[-1],
                        'color': r['props'].get('color'), 'ctag': r['props'].get('ctag')})
        return out

    def events(self, calendar_url, start, end, limit=200):
        """VEVENTs overlapping [start, end) (aware datetimes). Returns parsed dicts, soonest first."""
        body = ('<?xml version="1.0" encoding="utf-8"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                '<d:prop><d:getetag/><c:calendar-data/></d:prop><c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                '<c:time-range start="%s" end="%s"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>') % (utc_stamp(start), utc_stamp(end))
        status, headers, data, final = self.request('REPORT', calendar_url, body, depth=1)
        found = []
        for r in parse_multistatus(data):
            ics = r['props'].get('calendar-data')
            if not ics:
                continue
            for ev in parse_vevents(ics):
                ev['url'] = urllib.parse.urljoin(final, r['href'])
                ev['etag'] = r['props'].get('getetag')
                found.append(ev)
        found.sort(key=lambda e: e.get('start') or '')
        return found[:limit]

    def create_event(self, calendar_url, summary, start, end, location=None, description=None, uid=None, all_day=False):
        """PUT one new VEVENT. If-None-Match: * guarantees we never overwrite an existing object."""
        uid = uid or (uuid.uuid4().hex + '@procurator')
        ics = build_vevent(uid, summary, start, end, location, description, all_day)
        url = calendar_url.rstrip('/') + '/' + uid + '.ics'
        status, headers, data, final = self.request('PUT', url, ics, headers={'If-None-Match': '*'}, ok=(201, 204))
        return {'uid': uid, 'url': final, 'etag': headers.get('ETag') or headers.get('etag')}

    def delete(self, object_url, etag=None):
        h = {'If-Match': etag} if etag else {}
        self.request('DELETE', object_url, headers=h, ok=(200, 204))
        return True

    # ---------- CardDAV ----------
    def addressbooks(self):
        home = self.home('addressbook')
        responses, final = self.propfind(home, '<d:displayname/><d:resourcetype/><cs:getctag/>', depth=1)
        return [{'url': urllib.parse.urljoin(final, r['href']), 'name': r['props'].get('displayname') or 'Contacts', 'ctag': r['props'].get('ctag')}
                for r in responses if r['props'].get('is_addressbook')]

    def contacts(self, addressbook_url, limit=2000):
        body = ('<?xml version="1.0" encoding="utf-8"?><card:addressbook-query xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
                '<d:prop><d:getetag/><card:address-data/></d:prop><card:filter/></card:addressbook-query>')
        status, headers, data, final = self.request('REPORT', addressbook_url, body, depth=1)
        out = []
        for r in parse_multistatus(data):
            vcf = r['props'].get('address-data')
            if vcf:
                for card in parse_vcards(vcf):
                    card['url'] = urllib.parse.urljoin(final, r['href'])
                    out.append(card)
        out.sort(key=lambda c: (c.get('name') or '').lower())
        return out[:limit]


# ---------- parsing ----------
def parse_multistatus(data):
    """-> [{href, status, props:{...}}]. Props are flattened to the handful of keys this module uses."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise DAVError('The server returned something that is not a DAV response.') from None
    out = []
    for resp in root.findall('d:response', NS):
        href = (resp.findtext('d:href', default='', namespaces=NS) or '').strip()
        props = {}
        for ps in resp.findall('d:propstat', NS):
            st = ps.findtext('d:status', default='', namespaces=NS)
            if '200' not in st:
                continue
            p = ps.find('d:prop', NS)
            if p is None:
                continue
            for el in p:
                tag = el.tag.split('}', 1)[-1]
                if tag in ('current-user-principal', 'calendar-home-set', 'addressbook-home-set'):
                    h = el.find('d:href', NS)
                    props[tag] = (h.text or '').strip() if h is not None else None
                elif tag == 'resourcetype':
                    props['is_calendar'] = el.find('c:calendar', NS) is not None
                    props['is_addressbook'] = el.find('card:addressbook', NS) is not None
                elif tag == 'supported-calendar-component-set':
                    props['components'] = [c.get('name') for c in el.findall('c:comp', NS)]
                elif tag == 'displayname':
                    props['displayname'] = (el.text or '').strip()
                elif tag == 'calendar-color':
                    props['color'] = (el.text or '').strip()[:9]
                elif tag == 'getctag':
                    props['ctag'] = (el.text or '').strip()
                elif tag == 'getetag':
                    props['getetag'] = (el.text or '').strip()
                elif tag in ('calendar-data', 'address-data'):
                    props[tag] = el.text or ''
        out.append({'href': href, 'props': props})
    return out


def unfold(text):
    """RFC 5545 / 6350 line unfolding: a line starting with a space or tab continues the previous one."""
    return re.sub(r'\r?\n[ \t]', '', text.replace('\r\n', '\n'))


def unescape(value):
    return value.replace('\\n', '\n').replace('\\N', '\n').replace('\\,', ',').replace('\\;', ';').replace('\\\\', '\\')


def parse_properties(block):
    """'NAME;PARAM=V:value' lines -> [(name, params, value)]."""
    out = []
    for line in unfold(block).split('\n'):
        if ':' not in line:
            continue
        head, value = line.split(':', 1)
        parts = head.split(';')
        params = {}
        for p in parts[1:]:
            if '=' in p:
                k, v = p.split('=', 1)
                params[k.upper()] = v.strip('"')
        out.append((parts[0].upper(), params, value))
    return out


def parse_ical_time(value, params):
    """-> ISO 8601 string. Date-only values stay dates; floating times are returned as given, zoned ones as UTC when 'Z'."""
    value = value.strip()
    if params.get('VALUE') == 'DATE' or re.fullmatch(r'\d{8}', value):
        return '%s-%s-%s' % (value[:4], value[4:6], value[6:8])
    m = re.fullmatch(r'(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)', value)
    if not m:
        return value
    iso = '%s-%s-%sT%s:%s:%s' % m.groups()[:6]
    if m.group(7):
        return iso + 'Z'
    if params.get('TZID'):
        return iso + ' ' + params['TZID']
    return iso


def parse_vevents(ics):
    events = []
    for block in re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', ics.replace('\r\n', '\n'), flags=re.S):
        ev = {'summary': '', 'start': None, 'end': None, 'all_day': False}
        for name, params, value in parse_properties(block):
            if name == 'SUMMARY':
                ev['summary'] = unescape(value)[:300]
            elif name == 'DTSTART':
                ev['start'] = parse_ical_time(value, params)
                ev['all_day'] = params.get('VALUE') == 'DATE' or bool(re.fullmatch(r'\d{8}', value.strip()))
            elif name == 'DTEND':
                ev['end'] = parse_ical_time(value, params)
            elif name == 'LOCATION':
                ev['location'] = unescape(value)[:300]
            elif name == 'DESCRIPTION':
                ev['description'] = unescape(value)[:2000]
            elif name == 'UID':
                ev['uid'] = value.strip()[:200]
            elif name == 'RRULE':
                ev['recurring'] = True
            elif name == 'STATUS':
                ev['status'] = value.strip().lower()
        events.append(ev)
    return events


def parse_vcards(vcf):
    cards = []
    for block in re.findall(r'BEGIN:VCARD(.*?)END:VCARD', vcf.replace('\r\n', '\n'), flags=re.S):
        card = {'name': '', 'phones': [], 'emails': []}
        for name, params, value in parse_properties(block):
            if name == 'FN':
                card['name'] = unescape(value)[:200]
            elif name == 'N' and not card['name']:
                parts = [unescape(p) for p in value.split(';')]
                card['name'] = ' '.join(p for p in (parts[1:2] + parts[0:1]) if p)[:200]
            elif name == 'TEL':
                card['phones'].append({'type': (params.get('TYPE') or '').lower(), 'number': value.strip()[:40]})
            elif name == 'EMAIL':
                card['emails'].append({'type': (params.get('TYPE') or '').lower(), 'address': value.strip()[:200]})
            elif name == 'ORG':
                card['org'] = unescape(value.split(';')[0])[:200]
            elif name == 'UID':
                card['uid'] = value.strip()[:200]
            elif name == 'BDAY':
                card['birthday'] = parse_ical_time(value, {'VALUE': 'DATE'}) if re.fullmatch(r'\d{8}', value.strip()) else value.strip()[:20]
        if card['name'] or card['phones'] or card['emails']:
            cards.append(card)
    return cards


# ---------- building ----------
def utc_stamp(t):
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def escape(value):
    return str(value).replace('\\', '\\\\').replace(';', '\\;').replace(',', '\\,').replace('\n', '\\n')


def fold(line):
    out, enc = [], line.encode()
    while len(enc) > 73:
        cut = 73
        while cut > 0 and (enc[cut] & 0xC0) == 0x80:  # never split a UTF-8 sequence
            cut -= 1
        out.append(enc[:cut].decode()); enc = b' ' + enc[cut:]
    out.append(enc.decode())
    return '\r\n'.join(out)


def build_vevent(uid, summary, start, end, location=None, description=None, all_day=False):
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    if all_day:
        s = 'DTSTART;VALUE=DATE:' + start.strftime('%Y%m%d')
        e = 'DTEND;VALUE=DATE:' + end.strftime('%Y%m%d')
    else:
        s = 'DTSTART:' + utc_stamp(start)
        e = 'DTEND:' + utc_stamp(end)
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Primaris Health Institute//PROCURATOR//EN', 'BEGIN:VEVENT', 'UID:' + uid,
             'DTSTAMP:' + now, s, e, fold('SUMMARY:' + escape(summary)[:300])]
    if location:
        lines.append(fold('LOCATION:' + escape(location)[:300]))
    if description:
        lines.append(fold('DESCRIPTION:' + escape(description)[:2000]))
    lines += ['END:VEVENT', 'END:VCALENDAR', '']
    return '\r\n'.join(lines)


def icloud_calendar(username, password, **kw):
    return DAVClient(ICLOUD_CALDAV, username, password, **kw)


def icloud_contacts(username, password, **kw):
    return DAVClient(ICLOUD_CARDDAV, username, password, **kw)
