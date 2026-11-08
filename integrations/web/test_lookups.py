"""places_search and npi_lookup (0.25.0): request shape, parsing, fallbacks and refusals, against canned responses."""
import io
import json
import unittest
import urllib.parse

from lookups import Lookups, phone_e164


class Resp(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, body):
        self.body, self.reqs = body, []
    def __call__(self, req, timeout=None):
        self.reqs.append(req)
        if isinstance(self.body, Exception):
            raise self.body
        return Resp(json.dumps(self.body).encode())


NPI_BODY = {'result_count': 1, 'results': [{'number': '1234567893', 'enumeration_type': 'NPI-1',
            'basic': {'first_name': 'ANA', 'last_name': 'RIVERA', 'credential': 'MD', 'status': 'A'},
            'addresses': [{'address_purpose': 'MAILING', 'address_1': 'PO BOX 1', 'city': 'MIAMI', 'state': 'FL', 'postal_code': '331300000', 'telephone_number': '305-555-0000'},
                          {'address_purpose': 'LOCATION', 'address_1': '100 MAIN ST', 'address_2': 'SUITE 4', 'city': 'MIAMI', 'state': 'FL', 'postal_code': '331310000', 'telephone_number': '305-555-0142'}],
            'taxonomies': [{'desc': 'Internal Medicine', 'primary': False, 'state': 'FL'}, {'desc': 'Dermatology', 'primary': True, 'state': 'FL'}]}]}


class LookupTests(unittest.TestCase):
    def test_npi_parses_location_primary_specialty_and_phone(self):
        o = Opener(NPI_BODY)
        r = Lookups(lambda k: None, opener=o).call('npi_lookup', {'last_name': 'Rivera', 'state': 'fl', 'starts_with': True})
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(o.reqs[0].full_url).query)
        self.assertEqual((q['last_name'], q['state'], q['version']), (['Rivera*'], ['FL'], ['2.1']))
        x = r['results'][0]
        self.assertEqual((x['name'], x['specialty'], x['practice_phone'], x['status']), ('Ana Rivera', 'Dermatology', '+13055550142', 'active'))
        self.assertIn('100 MAIN ST, SUITE 4, MIAMI, FL, 33131', x['practice_address'])
        self.assertIn('NPI Registry', r['source'])

    def test_npi_refuses_vague_or_bad_queries(self):
        L = Lookups(lambda k: None, opener=Opener(NPI_BODY))
        for bad in ({}, {'state': 'FL'}, {'number': '12345'}, {'last_name': 'X', 'state': 'Florida'}):
            with self.assertRaises(ValueError):
                L.call('npi_lookup', bad)
        with self.assertRaisesRegex(ValueError, 'did not answer'):
            Lookups(lambda k: None, opener=Opener(OSError('down'))).call('npi_lookup', {'last_name': 'Rivera'})

    def test_places_uses_google_with_a_key_and_field_mask(self):
        o = Opener({'places': [{'displayName': {'text': 'CVS Pharmacy'}, 'formattedAddress': '1 Main St, Miami, FL', 'nationalPhoneNumber': '(305) 555-0177',
                                'internationalPhoneNumber': '+1 305-555-0177', 'regularOpeningHours': {'weekdayDescriptions': ['Monday: 8 AM–10 PM']}, 'businessStatus': 'OPERATIONAL'}]})
        r = Lookups(lambda k: 'KEY' if k == 'places' else None, opener=o).call('places_search', {'query': 'CVS', 'near': 'Miami, FL', 'limit': 3})
        req = o.reqs[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(req.headers.get('X-goog-api-key'), 'KEY')
        self.assertIn('places.nationalPhoneNumber', req.headers.get('X-goog-fieldmask'))
        self.assertEqual(json.loads(req.data)['textQuery'], 'CVS near Miami, FL')
        p = r['places'][0]
        self.assertEqual((p['name'], p['phone'], p['hours']), ('CVS Pharmacy', '+13055550177', ['Monday: 8 AM–10 PM']))
        self.assertNotIn('KEY', json.dumps(r))

    def test_places_falls_back_to_openstreetmap_without_a_key(self):
        o = Opener([{'name': 'Bay Clinic', 'display_name': 'Bay Clinic, 5 Bay Rd, Miami, FL', 'osm_type': 'node', 'osm_id': 42, 'extratags': {'phone': '+1 305 555 0123', 'opening_hours': 'Mo-Fr 08:00-17:00'}}])
        t = [100.0]
        r = Lookups(lambda k: None, opener=o, now=lambda: t[0]).call('places_search', {'query': 'Bay Clinic'})
        self.assertIn('OpenStreetMap', r['source'])
        self.assertIn('nominatim.openstreetmap.org', o.reqs[0].full_url)
        self.assertIn('PROCURATOR', o.reqs[0].headers.get('User-agent'))
        self.assertEqual(r['places'][0]['phone'], '+13055550123')
        self.assertIn('places', r['note'])

    def test_phone_normalization(self):
        self.assertEqual(phone_e164('(305) 555-0142'), '+13055550142')
        self.assertIsNone(phone_e164('555-0142'))


if __name__ == '__main__':
    unittest.main()
