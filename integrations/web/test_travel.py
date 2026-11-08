"""0.20.5 (Nov 7, ATL-GYE and hotels): fare calendar, results-page links and rate-limit/robot pages."""
import io
import json
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch
from pathlib import Path
from broker import Broker, site_blocked
from test_broker import Provider


class Opener:
    """Answers like the Travelpayouts API; records every request."""
    def __init__(self, months, days, nonstop_empty=False):
        self.months, self.days, self.nonstop_empty, self.seen = months, days, nonstop_empty, []

    def __call__(self, req, timeout=None):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(req.full_url).query))
        self.seen.append((req.full_url, req.headers.get('X-access-token'), q))
        if self.nonstop_empty and q.get('direct') == 'true':
            data = {}
        elif q.get('group_by') == 'month':
            data = self.months
        else:
            data = {k: v for k, v in self.days.items() if k.startswith(q.get('departure_at', ''))}
        return io.BytesIO(json.dumps({'success': True, 'data': data, 'currency': 'usd'}).encode())


def fare(dep, ret, price, stops=0):
    return {'price': price, 'departure_at': dep + 'T10:00:00-05:00', 'return_at': ret + 'T08:00:00-05:00', 'transfers': stops,
            'return_transfers': stops, 'airline': 'AA', 'flight_number': '1234', 'link': '/search/ATL%sGYE1' % dep[5:]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        dns = patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]); dns.start(); self.addCleanup(dns.stop)
        self.b = Broker(Path(self.tmp.name) / 'b.sqlite3', Provider())
        self.keys = self.b.router.keys; self.keys.mkdir(parents=True, exist_ok=True)

    def key(self, name, value='tok123'):
        (self.keys / (name + '.key')).write_text(value + '\n')


class FareCalendarTests(Base):
    MONTHS = {'2027-02': fare('2027-02-10', '2027-02-20', 612), '2027-05': fare('2027-05-04', '2027-05-14', 455, 1), '2026-12': fare('2026-12-20', '2027-01-03', 980, 1)}
    DAYS = {'2027-05-04': fare('2027-05-04', '2027-05-14', 455, 1), '2027-05-11': fare('2027-05-11', '2027-05-18', 470, 1),
            '2027-02-10': fare('2027-02-10', '2027-02-20', 612), '2027-02-17': fare('2027-02-17', '2027-02-24', 640)}

    def test_cheapest_window_months_then_days(self):
        self.key('travelpayouts'); op = Opener(self.MONTHS, self.DAYS)
        out = self.b.router.flight_fares({'origin': 'atl', 'destination': 'gye'}, opener=op)
        self.assertEqual([m['depart'][:7] for m in out['cheapest_months']], ['2027-05', '2027-02', '2026-12'])
        self.assertEqual(out['cheapest_days_in_best_months'][0], {'depart': '2027-05-04', 'return': '2027-05-14', 'price_usd': 455, 'stops_out': 1, 'stops_back': 1,
                         'airline': 'AA', 'flight': 'AA1234', 'aviasales_link': 'https://www.aviasales.com/search/ATL05-04GYE1'})
        self.assertEqual(out['trip'], 'round trip, 3-30 days'); self.assertIn('cached up to 7 days', out['note'])
        url, token, q = op.seen[0]
        self.assertTrue(url.startswith('https://api.travelpayouts.com/aviasales/v3/grouped_prices?')); self.assertEqual(token, 'tok123')
        self.assertNotIn('tok123', url)                                                     # the token travels in a header, not the URL
        self.assertEqual((q['currency'], q['market'], q['min_trip_duration'], q['max_trip_duration']), ('usd', 'us', '3', '30'))

    def test_nonstop_that_does_not_exist_says_so_and_shows_one_stop(self):
        self.key('travelpayouts')
        out = self.b.router.flight_fares({'origin': 'ATL', 'destination': 'GYE', 'nonstop': True}, opener=Opener(self.MONTHS, self.DAYS, nonstop_empty=True))
        self.assertEqual(out['cheapest_months'], []); self.assertIs(out['nonstop_found'], False)
        self.assertIn('No nonstop fare', out['note']); self.assertEqual(out['cheapest_months_with_stops'][0]['price_usd'], 455)

    def test_one_month_and_one_way_and_bad_input(self):
        self.key('travelpayouts'); op = Opener(self.MONTHS, self.DAYS)
        out = self.b.router.flight_fares({'origin': 'ATL', 'destination': 'GYE', 'month': '2027-02', 'one_way': True}, opener=op)
        self.assertEqual([d['depart'] for d in out['cheapest_days']], ['2027-02-10', '2027-02-17']); self.assertEqual(out['trip'], 'one-way')
        self.assertNotIn('min_trip_duration', op.seen[-1][2])
        for bad in ({'origin': 'Atlanta', 'destination': 'GYE'}, {'origin': 'ATL', 'destination': 'GYE', 'month': 'May'},
                    {'origin': 'ATL', 'destination': 'GYE', 'trip_days_min': 20, 'trip_days_max': 5}):
            with self.assertRaises(ValueError):
                self.b.router.flight_fares(bad, opener=op)

    def test_without_a_token_it_says_how_to_get_one(self):
        with self.assertRaisesRegex(ValueError, 'set-web-key.sh travelpayouts'):
            self.b.router.flight_fares({'origin': 'ATL', 'destination': 'GYE'}, opener=Opener({}, {}))

    def test_routing_sends_flight_questions_to_the_fare_tools(self):
        self.assertIn('travelpayouts', self.b.router.route('', 'cheapest round trip flights ATL to GYE', log=False)['alternatives'][0])
        self.key('travelpayouts')
        out = self.b.router.route('', 'cheapest round trip flights ATL to GYE', log=False)
        self.assertEqual(out['route'], 'api'); self.assertIn('flight_fares', out['reason']); self.assertTrue(out['available']['travelpayouts'])


class TravelLinkTests(Base):
    def test_flight_links_open_results_directly(self):
        out = self.b.call('travel_links', {'kind': 'flights', 'origin': 'ATL', 'destination': 'GYE', 'depart': '2027-05-04', 'return': '2027-05-14', 'adults': 2, 'nonstop': True})
        urls = {l['site']: l['url'] for l in out['links']}
        self.assertEqual(urls['kayak.com'], 'https://www.kayak.com/flights/ATL-GYE/2027-05-04/2027-05-14/2adults?sort=price_a&fs=stops=0')
        self.assertIn('leg1=from:ATL,to:GYE,departure:5/4/2027TANYT&leg2=from:GYE,to:ATL,departure:5/14/2027TANYT', urls['expedia.com'])
        self.assertTrue(urls['skyscanner.com'].startswith('https://www.skyscanner.com/transport/flights/atl/gye/270504/270514/?adultsv2=2'))
        self.assertTrue(out['for_him']['url'].startswith('https://www.google.com/travel/flights?q=')); self.assertIn('also for them', out['for_him']['site'])
        self.assertEqual(urls['google.com/travel/flights'], out['for_him']['url'])                  # 0.20.7: Google Flights is browsable too
        self.assertEqual(self.b.site_rule('https://www.google.com/search?q=x')['mode'], 'never')
        for l in out['links']:
            self.assertNotEqual(self.b.site_rule(l['url'])['mode'], 'never', l['site'])          # every link is a site the browser may open

    def test_hotel_links(self):
        out = self.b.call('travel_links', {'kind': 'hotels', 'destination': 'Miami, FL', 'checkin': '2026-12-25', 'checkout': '2026-12-26', 'adults': 2})
        urls = {l['site']: l['url'] for l in out['links']}
        self.assertEqual(urls['hotels.com'], 'https://www.hotels.com/Hotel-Search?destination=Miami%2C%20FL&startDate=2026-12-25&endDate=2026-12-26&adults=2&rooms=1&sort=PRICE_LOW_TO_HIGH')
        self.assertIn('checkin=2026-12-25&checkout=2026-12-26&group_adults=2', urls['booking.com'])
        with self.assertRaises(ValueError):
            self.b.call('travel_links', {'kind': 'hotels', 'destination': 'Miami', 'checkin': 'Dec 25', 'checkout': '2026-12-26'})
        with self.assertRaises(ValueError):
            self.b.call('travel_links', {'kind': 'cars'})


class BlockedPageTests(unittest.TestCase):
    def test_rate_limit_and_robot_pages_are_named(self):
        self.assertEqual(site_blocked({'title': 'Too Many Requests', 'text': ''}), 'Too Many Requests')
        self.assertEqual(site_blocked({'title': 'Expedia', 'text': 'Access Denied\nReference #18.2f'}), 'Access Denied')
        self.assertEqual(site_blocked({'title': 'skyscanner', 'text': 'Are you a person or a robot? Press & Hold to confirm'}), 'Press & Hold')
        self.assertIsNone(site_blocked({'title': 'Hotels in Miami', 'text': 'Rooms from $429 per night ' * 200}))   # a long results page
        self.assertIsNone(site_blocked({'title': 'Miami deals', 'text': 'Book now: $429'}))


class BlockedSnapshotTests(Base):
    def test_the_snapshot_tells_the_assistant_to_switch_source(self):
        sid = self.b.call('open', {'request_id': 'exp-blocked-01', 'url': 'https://www.expedia.com/'})['id']
        self.b.provider.snap = {'url': 'https://www.expedia.com/', 'title': 'Too Many Requests', 'text': 'Too Many Requests', 'controls': [], 'frame_count': 0}
        snap = self.b.call('snapshot', {'id': sid})['snapshot']
        self.assertEqual(snap['site_blocked'], 'Too Many Requests'); self.assertIn('Do not reload', snap['site_blocked_guidance'])
        self.b.provider.snap = {'url': 'https://www.expedia.com/', 'title': 'Expedia', 'text': 'Flights to Lima', 'controls': [], 'frame_count': 0}
        self.assertNotIn('site_blocked', self.b.call('snapshot', {'id': sid})['snapshot'])


if __name__ == '__main__':
    unittest.main()
