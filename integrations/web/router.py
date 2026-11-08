"""Capability router and read-only adapters (web 0.18.0, PROCURATOR browsing plan Phase 6).

capability_route(site, intent) -> api | mcp | playbook | browse:cloud | browse:home | takeover, with the reason.
Prefer a structured source over a browser whenever one exists:
- flights: Duffel offer search (read-only; booking stays with the owner via the airline/agency site),
- account balances: SimpleFIN Bridge (read-only, only if the owner connected it; banks are never browsed),
- Shopify stores: the Shop MCP connector,
- sites marked never: the owner does it themselves (takeover);
- everything else: the cloud browser. The home lane (Phase 5) and playbooks (Phase 7) are not built yet and are
  never chosen; the reason says when a site would prefer the home lane.

Keys live in /var/lib/procurator-web/keys/ (mode 600), set on the server with bin/set-web-key.sh duffel|simplefin.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FLIGHT = re.compile(r'\b(flights?|fly|airfare|plane tickets?|one[- ]way|round[- ]trip|nonstop)\b', re.I)
BALANCE = re.compile(r'\b(balance|balances|how much (?:is|do i have) in|account summary|checking|savings)\b', re.I)
SHOP = re.compile(r'\b(buy|order|cart|shop|purchase|product)\b', re.I)
HOME_PREFERRED = re.compile(r'(?:\.gov$|(?:^|\.)leaveco\.example$|(?:^|\.)cvs\.com$|(?:^|\.)walmart\.com$|(?:^|\.)target\.com$|power|energy|electric|water|gas)', re.I)
IATA = re.compile(r'^[A-Z]{3}$')
HOTEL = re.compile(r'\b(hotels?|motels?|rooms?|stays?|lodging|resorts?|inn|airbnb|accommodations?)\b', re.I)
TP = 'https://api.travelpayouts.com/aviasales/v3/'
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')

class Router:
    def __init__(self, broker, state):
        self.b = broker
        self.keys = Path(state) / 'keys'
        with broker.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS route_log(ts REAL,site TEXT,intent TEXT,route TEXT,reason TEXT,task_id TEXT)')

    def key(self, name):
        try:
            v = (self.keys / (name + '.key')).read_text().strip()
            return v or None
        except OSError:
            return None

    def route(self, site, intent, task_id=None, log=True):
        site = (site or '').lower().strip()
        host = urllib.parse.urlsplit(site if '://' in site else 'https://' + site).hostname or site
        host = host[4:] if host.startswith('www.') else host
        intent = (intent or '')[:300]
        alternatives = []
        rule = self.b.site_rule(host) if host else {'mode': 'browser', 'reason': ''}
        shopify = host and host in self.shopify_sites()
        if FLIGHT.search(intent) and (self.key('duffel') or self.key('travelpayouts')):
            # 0.20.5 (Nov 7, ATL-GYE): structured fare data first; travel sites throttle and bot-check automated browsers.
            route, reason = 'api', ('cheapest dates/months and nonstop availability: flight_fares (fare calendar)' if self.key('travelpayouts') else '') + \
                ('; ' if self.key('travelpayouts') and self.key('duffel') else '') + ('live offers for exact dates: flight_search (Duffel)' if self.key('duffel') else '') + \
                '; to show results on a travel site use travel_search_links and open the link (read-only research); booking stays with the owner'
            alternatives.append('travel_search_links + browser (prefer the PC for Expedia-group sites)')
        elif BALANCE.search(intent) and (rule['mode'] == 'never' or not host):
            if self.key('simplefin'):
                route, reason = 'api', 'balances come read-only from SimpleFIN Bridge (account_balances); bank sites are never browsed'
            else:
                route, reason = 'takeover', 'banks are never browsed and SimpleFIN is not connected; the owner checks it themselves'
        elif rule['mode'] == 'never':
            route, reason = 'takeover', host + ' is never browsed by the assistant: ' + rule.get('reason', '')
        elif shopify and SHOP.search(intent):
            route, reason = 'mcp', host + ' is a Shopify store: use the Shop connector (search, cart, checkout link for them to pay)'
            alternatives.append('browse:cloud')
        else:
            # 0.19.2 (Nov 6 compat run, finding 4): the browser lane the broker would pick now (rules, PC online), labelled as a
            # prediction. Where a browser actually ran is the session's own record (its lane), never this route event.
            try:
                lane, _, why = self.b.lanes.choose('https://' + host) if host else ('cloud', None, 'no site given')
            except ValueError as e:
                lane, why = 'home', str(e)
            except Exception:
                lane, why = 'cloud', 'lane check unavailable'
            route = 'browse:' + lane
            reason = 'no structured source for this; the %s browser with every gate on (predicted: %s)' % ('home PC' if lane == 'home' else 'cloud', why)
            if rule['mode'] == 'read_only':
                reason += ' (read-only site: pages and links only)'
            if rule['mode'] == 'owner_final':
                reason += ' (the owner presses the final order button themselves)'
            if FLIGHT.search(intent):
                alternatives.append('api (flight_fares calendar once a free Travelpayouts token is set: bin/set-web-key.sh travelpayouts; flight_search once a Duffel key is set)')
            if FLIGHT.search(intent) or HOTEL.search(intent):
                reason += '. Start from travel_search_links: open the results page directly instead of typing into the search form'
        out = {'route': route, 'reason': reason, 'site': host or None, 'alternatives': alternatives,
               'available': {'duffel': bool(self.key('duffel')), 'travelpayouts': bool(self.key('travelpayouts')), 'simplefin': bool(self.key('simplefin')), 'shop_mcp': True, 'home_lane': self._home_online(), 'playbooks': True},
               'lane_note': 'browse:home/cloud is a prediction; the browser session records where it actually ran'}
        if log:
            with self.b.db() as db:
                db.execute('INSERT INTO route_log VALUES(?,?,?,?,?,?)', (time.time(), host, intent[:160], route, reason[:200], task_id))
            if task_id:
                self.b.event(task_id, 'route', {'route': route, 'site': host})
        return out

    def _home_online(self):
        try:
            return bool(self.b.lanes.online_machine())
        except Exception:
            return False

    def shopify_sites(self):
        with self.b.db() as db:
            r = db.execute("SELECT value FROM owner_settings WHERE key='shopify_sites'").fetchone()
        return set(json.loads(r['value'])) if r else set()

    def note_platform(self, host, platform):
        """Snapshots report page platforms (WebMCP); Shopify stores are remembered for routing."""
        if platform != 'shopify' or not host:
            return
        sites = self.shopify_sites()
        if host in sites:
            return
        sites.add(host)
        with self.b.db() as db:
            db.execute("INSERT OR REPLACE INTO owner_settings VALUES('shopify_sites',?)", (json.dumps(sorted(sites)[-200:]),))

    def share(self, since):
        with self.b.db() as db:
            rows = db.execute('SELECT route FROM route_log WHERE ts>=?', (since,)).fetchall()
        n = len(rows)
        browser = sum(1 for r in rows if r['route'].startswith('browse:'))
        return {'routed': n, 'needed_browser': browser, 'share_browser': round(browser / n, 3) if n else None,
                'by_route': {k: sum(1 for r in rows if r['route'] == k) for k in sorted({r['route'] for r in rows})}}

    # ---------- adapters ----------
    def flights(self, args, opener=urllib.request.urlopen):
        token = self.key('duffel')
        if not token:
            raise ValueError('Flight search needs a Duffel access token on the server (bin/set-web-key.sh duffel). Until then, search on a travel site in the browser.')
        o, d = str(args.get('origin', '')).upper(), str(args.get('destination', '')).upper()
        if not IATA.match(o) or not IATA.match(d):
            raise ValueError('Give 3-letter airport codes (e.g. ATL, MIA).')
        dates = [args.get('departure_date')] + ([args['return_date']] if args.get('return_date') else [])
        for x in dates:
            if not isinstance(x, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', x):
                raise ValueError('Dates are YYYY-MM-DD.')
        adults = args.get('adults', 1)
        if type(adults) is not int or not 1 <= adults <= 6:
            raise ValueError('1 to 6 adults.')
        cabin = args.get('cabin', 'economy')
        if cabin not in ('economy', 'premium_economy', 'business', 'first'):
            raise ValueError('cabin is economy, premium_economy, business or first.')
        slices = [{'origin': o, 'destination': d, 'departure_date': dates[0]}] + ([{'origin': d, 'destination': o, 'departure_date': dates[1]}] if len(dates) > 1 else [])
        body = {'data': {'slices': slices, 'passengers': [{'type': 'adult'}] * adults, 'cabin_class': cabin, **({'max_connections': 0} if args.get('nonstop') else {})}}
        req = urllib.request.Request('https://api.duffel.com/air/offer_requests?return_offers=true&supplier_timeout=20000', data=json.dumps(body).encode(),
            headers={'Authorization': 'Bearer ' + token, 'Duffel-Version': 'v2', 'Content-Type': 'application/json', 'Accept': 'application/json'})
        with opener(req, timeout=45) as r:
            data = json.load(r).get('data') or {}
        offers = sorted(data.get('offers') or [], key=lambda x: float(x.get('total_amount') or 1e9))[:8]
        def seg(s):
            c = s.get('marketing_carrier') or {}
            return '%s%s %s→%s %s–%s' % (c.get('iata_code', ''), s.get('marketing_carrier_flight_number', ''), (s.get('origin') or {}).get('iata_code', ''),
                                          (s.get('destination') or {}).get('iata_code', ''), (s.get('departing_at') or '')[5:16].replace('T', ' '), (s.get('arriving_at') or '')[11:16])
        return {'source': 'Duffel (live airline offers; read-only)', 'searched': slices, 'adults': adults, 'cabin': cabin,
                'offers': [{'price': x.get('total_amount') + ' ' + x.get('total_currency', ''), 'airline': (x.get('owner') or {}).get('name'),
                            'slices': [{'duration': sl.get('duration'), 'stops': max(0, len(sl.get('segments') or []) - 1), 'flights': [seg(s) for s in sl.get('segments') or []]} for sl in x.get('slices') or []],
                            'refundable': ((x.get('conditions') or {}).get('refund_before_departure') or {}).get('allowed'),
                            'expires_at': x.get('expires_at')} for x in offers],
                'booking': 'Not booked. Offer prices can change; the owner books on the airline or travel site (the assistant can open it and fill everything up to payment).'}

    def _tp(self, path, params, token, opener):
        q = urllib.parse.urlencode({**params, 'currency': 'usd', 'market': 'us'})
        req = urllib.request.Request(TP + path + '?' + q, headers={'X-Access-Token': token, 'Accept': 'application/json'})
        try:
            with opener(req, timeout=30) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            raise ValueError('The fare calendar service answered %s; try again later or use travel_search_links.' % e.code)
        if not body.get('success', True):
            raise ValueError('The fare calendar service refused the request: %s' % str(body.get('error') or '')[:200])
        return body.get('data') or {}

    @staticmethod
    def _fare(x):
        link = x.get('link') or ''
        return {'depart': (x.get('departure_at') or '')[:10], 'return': (x.get('return_at') or '')[:10] or None, 'price_usd': x.get('price'),
                'stops_out': x.get('transfers'), 'stops_back': x.get('return_transfers'), 'airline': x.get('airline'),
                'flight': (x.get('airline') or '') + str(x.get('flight_number') or ''), 'aviasales_link': ('https://www.aviasales.com' + link) if link.startswith('/') else None}

    def flight_fares(self, args, opener=urllib.request.urlopen):
        """0.20.5 (Nov 7, ATL-GYE "cheapest window anytime of the year"): a fare calendar from the Travelpayouts (Aviasales)
        Data API: the cheapest price other travellers found for each month or day (cached up to 7 days). It answers "when is it
        cheapest" and "is there a nonstop" in one or two calls; exact live prices still need flight_search or a travel site."""
        token = self.key('travelpayouts')
        if not token:
            raise ValueError('The fare calendar needs a free Travelpayouts token on the server (bin/set-web-key.sh travelpayouts). Until then use travel_search_links and read the results in the browser.')
        o, d = str(args.get('origin', '')).upper(), str(args.get('destination', '')).upper()
        if not IATA.match(o) or not IATA.match(d):
            raise ValueError('Give 3-letter airport or city codes (e.g. ATL, GYE).')
        one_way = bool(args.get('one_way'))
        lo, hi = args.get('trip_days_min'), args.get('trip_days_max')
        for v in (lo, hi):
            if v is not None and (type(v) is not int or not 1 <= v <= 60):
                raise ValueError('Trip length is 1 to 60 days.')
        if not one_way and lo is None and hi is None:
            lo, hi = 3, 30
        lo = lo if lo is not None else 1
        hi = hi if hi is not None else max(lo, 30)
        if lo > hi:
            raise ValueError('trip_days_min is more than trip_days_max.')
        direct = bool(args.get('nonstop'))
        trip = {} if one_way else {'min_trip_duration': lo, 'max_trip_duration': hi}
        note = ('Prices other travellers found recently (cached up to 7 days, USD, per adult, economy). They show WHEN it is cheapest '
                'and whether nonstops exist; the live price can differ. Confirm exact dates with flight_search or a travel_search_links page before telling them a price is bookable.')
        month = args.get('month')
        if month is not None and not re.fullmatch(r'\d{4}-\d{2}', str(month)):
            raise ValueError('month is YYYY-MM.')
        def grouped(by, extra, nonstop):
            data = self._tp('grouped_prices', {'origin': o, 'destination': d, 'group_by': by, 'direct': str(nonstop).lower(), **trip, **extra}, token, opener)
            rows = data.values() if isinstance(data, dict) else data
            return sorted((self._fare(x) for x in rows if isinstance(x, dict) and x.get('price')), key=lambda f: f['price_usd'])
        out = {'source': 'Travelpayouts / Aviasales fare calendar (cached, read-only)', 'route': o + '-' + d,
               'trip': 'one-way' if one_way else 'round trip, %d-%d days' % (lo, hi), 'nonstop_only': direct, 'note': note}
        if month:
            days = grouped('departure_at', {'departure_at': month}, direct)
            out.update(month=month, cheapest_days=days[:12])
            if direct and not days:
                out['with_stops'] = grouped('departure_at', {'departure_at': month}, False)[:6]
            return out
        months = grouped('month', {}, direct)
        out['cheapest_months'] = months[:12]
        if direct and not months:
            out['nonstop_found'] = False
            out['note'] += ' No nonstop fare is in the calendar for this route, which usually means no nonstop is sold (or it is seasonal and not yet on sale); cheapest with stops are listed.'
            out['cheapest_months_with_stops'] = grouped('month', {}, False)[:12]
            months = out['cheapest_months_with_stops']
        best = sorted({m['depart'][:7] for m in months[:3] if m['depart']})
        if best:
            days = []
            for m in best:
                days += grouped('departure_at', {'departure_at': m}, direct and bool(out['cheapest_months']))
            out['cheapest_days_in_best_months'] = sorted(days, key=lambda f: f['price_usd'])[:10]
        return out

    def travel_links(self, args):
        """0.20.5: results-page addresses for the main travel sites, so the browser opens the search results directly instead
        of typing into a search form (fewer steps, fewer bot checks). 0.20.7: Google Flights/Hotels (google.com/travel) may be
        browsed too (read-only); the rest of google.com stays off-limits."""
        q = urllib.parse.quote
        kind = args.get('kind')
        adults = args.get('adults', 1)
        if type(adults) is not int or not 1 <= adults <= 9:
            raise ValueError('1 to 9 adults.')
        if kind == 'flights':
            o, d = str(args.get('origin', '')).upper(), str(args.get('destination', '')).upper()
            if not IATA.match(o) or not IATA.match(d):
                raise ValueError('Give 3-letter airport codes.')
            dep, ret = args.get('depart'), args.get('return')
            for x in [dep] + ([ret] if ret else []):
                if not isinstance(x, str) or not DATE.match(x):
                    raise ValueError('Dates are YYYY-MM-DD.')
            nonstop = bool(args.get('nonstop'))
            us = lambda x: '%d/%d/%s' % (int(x[5:7]), int(x[8:10]), x[:4])
            yymmdd = lambda x: x[2:4] + x[5:7] + x[8:10]
            kayak = 'https://www.kayak.com/flights/%s-%s/%s%s%s?sort=price_a%s' % (o, d, dep, ('/' + ret) if ret else '', ('/%dadults' % adults) if adults > 1 else '', '&fs=stops=0' if nonstop else '')
            exp = 'https://www.expedia.com/Flights-Search?trip=%s&leg1=from:%s,to:%s,departure:%sTANYT%s&passengers=adults:%d&options=cabinclass:economy&mode=search' % (
                'roundtrip' if ret else 'oneway', o, d, us(dep), ('&leg2=from:%s,to:%s,departure:%sTANYT' % (d, o, us(ret))) if ret else '', adults)
            sky = 'https://www.skyscanner.com/transport/flights/%s/%s/%s/%s?adultsv2=%d&cabinclass=economy%s' % (o.lower(), d.lower(), yymmdd(dep), (yymmdd(ret) + '/') if ret else '', adults, '&preferdirects=true' if nonstop else '')
            gf = 'https://www.google.com/travel/flights?q=' + q('Flights from %s to %s on %s%s%s' % (o, d, dep, (' through ' + ret) if ret else '', ' nonstop' if nonstop else ''))
            links = [{'site': 'kayak.com', 'url': kayak}, {'site': 'google.com/travel/flights', 'url': gf}, {'site': 'expedia.com', 'url': exp}, {'site': 'skyscanner.com', 'url': sky}]
            their = {'site': 'Google Flights (also for them to open)', 'url': gf}
        elif kind == 'hotels':
            dest = ' '.join(str(args.get('destination') or '').split())[:80]
            if not dest:
                raise ValueError('Give the destination (city or neighbourhood).')
            ci, co = args.get('checkin'), args.get('checkout')
            for x in (ci, co):
                if not isinstance(x, str) or not DATE.match(x):
                    raise ValueError('Dates are YYYY-MM-DD.')
            rooms = args.get('rooms', 1)
            if type(rooms) is not int or not 1 <= rooms <= 8:
                raise ValueError('1 to 8 rooms.')
            links = [{'site': 'hotels.com', 'url': 'https://www.hotels.com/Hotel-Search?destination=%s&startDate=%s&endDate=%s&adults=%d&rooms=%d&sort=PRICE_LOW_TO_HIGH' % (q(dest), ci, co, adults, rooms)},
                     {'site': 'booking.com', 'url': 'https://www.booking.com/searchresults.html?ss=%s&checkin=%s&checkout=%s&group_adults=%d&no_rooms=%d&group_children=0&order=price' % (q(dest), ci, co, adults, rooms)},
                     {'site': 'expedia.com', 'url': 'https://www.expedia.com/Hotel-Search?destination=%s&startDate=%s&endDate=%s&adults=%d&rooms=%d&sort=PRICE_LOW_TO_HIGH' % (q(dest), ci, co, adults, rooms)},
                     {'site': 'kayak.com', 'url': 'https://www.kayak.com/hotels/%s/%s/%s/%dadults?sort=price_a' % (q(dest.replace(' ', '-'), safe=',-'), ci, co, adults)}]
            gh = 'https://www.google.com/travel/hotels?q=' + q('hotels in %s %s to %s' % (dest, ci, co))
            links.append({'site': 'google.com/travel/hotels', 'url': gh})
            their = {'site': 'Google Hotels (also for them to open)', 'url': gh}
        else:
            raise ValueError('kind is flights or hotels.')
        return {'links': links, 'for_him': their,
                'how': 'Open one link with web_browser_open (a research task, read-only) and read the results; that skips typing into the search form. '
                       'If the page shows the site\'s home page or empty results, the site changed its address format: use its search form instead (typing the approved facts). '
                       'If a site shows "Too Many Requests", "Access Denied" or a robot check, do not reload it: use the next link or the fare tools. '
                       'Expedia, hotels.com and Kayak usually work better from their PC (browser=home) than from the cloud.'}

    def balances(self, opener=urllib.request.urlopen):
        access = self.key('simplefin')
        if not access:
            raise ValueError('Account balances need SimpleFIN Bridge connected on the server (bin/set-web-key.sh simplefin). Banks are never browsed.')
        p = urllib.parse.urlsplit(access)
        if p.scheme != 'https' or not p.username:
            raise ValueError('The saved SimpleFIN access URL is not valid; set it again with bin/set-web-key.sh simplefin.')
        url = urllib.parse.urlunsplit(('https', p.hostname + (':%d' % p.port if p.port else ''), p.path.rstrip('/') + '/accounts', 'balances-only=1', ''))
        auth = base64.b64encode((urllib.parse.unquote(p.username) + ':' + urllib.parse.unquote(p.password or '')).encode()).decode()
        req = urllib.request.Request(url, headers={'Authorization': 'Basic ' + auth, 'Accept': 'application/json'})
        with opener(req, timeout=30) as r:
            data = json.load(r)
        accounts = []
        for a in (data.get('accounts') or [])[:20]:
            when = a.get('balance-date')
            accounts.append({'institution': (a.get('org') or {}).get('name'), 'account': (a.get('name') or '')[:60], 'currency': a.get('currency'),
                             'balance': a.get('balance'), 'available': a.get('available-balance'),
                             'as_of': time.strftime('%Y-%m-%d %H:%M', time.localtime(int(when))) if isinstance(when, (int, float)) or str(when).isdigit() else None})
        return {'source': 'SimpleFIN Bridge (read-only)', 'accounts': accounts, 'errors': [str(e)[:160] for e in (data.get('errors') or [])][:5],
                'note': 'Read-only balances. Nothing can be moved or paid from here.'}
