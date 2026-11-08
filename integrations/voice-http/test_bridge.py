import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from bridge import PATH, ORIGIN, process

SETTINGS = {'testNumber': '+15555550123', 'fromNumber': '+15550100100'}
PAUSE = b'<?xml version="1.0"?><Response><Pause length="30"/></Response>'


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.fields = {'From': SETTINGS['testNumber'], 'To': SETTINGS['fromNumber'],
                       'Direction': 'inbound', 'CallStatus': 'ringing', 'CallSid': 'CA'+'a'*32}
        self.sent = []

    def send(self, target, body, signature):
        self.sent.append((target, body, signature))
        return 200, 'application/xml', PAUSE

    def run_form(self, fields=None, target=PATH, send=None):
        raw = urllib.parse.urlencode(self.fields if fields is None else fields).encode()
        return process(target, raw, 'valid-test-signature', SETTINGS, send or self.send)

    def test_answers_only_authorized_initial_pause_and_redirects_same_webhook(self):
        status, _, body = self.run_form()
        xml = ET.fromstring(body)
        self.assertEqual(status, 200)
        self.assertEqual([n.tag for n in xml], ['Say', 'Redirect'])
        self.assertIn('AI assistant', xml[0].text)
        self.assertEqual(xml[1].attrib, {'method': 'POST'})
        self.assertEqual(xml[1].text, ORIGIN+PATH+'?classic_answered=1')
        self.assertEqual(self.sent[0][2], 'valid-test-signature')
        self.assertEqual(self.sent[0][1], urllib.parse.urlencode(self.fields).encode())

    def test_failed_signature_never_generates_an_answer(self):
        def rejected(*_):
            return 401, 'text/plain', b'Unauthorized'
        self.assertEqual(self.run_form(send=rejected), (401, 'text/plain', b'Unauthorized'))

    def test_other_caller_or_number_and_outbound_calls_are_unchanged(self):
        for key, value in [('From', '+15555550124'), ('To', '+15555550125'),
                           ('Direction', 'outbound-api'), ('CallSid', 'invalid'), ('CallStatus', 'in-progress')]:
            with self.subTest(key=key):
                self.assertEqual(self.run_form({**self.fields, key: value})[2], PAUSE)

    def test_redirect_marker_status_and_turn_callbacks_cannot_loop(self):
        for query in ['classic_answered=1', 'type=status', 'type=amd', 'callId=anything', 'turnToken=anything']:
            with self.subTest(query=query):
                self.assertEqual(self.run_form(target=PATH+'?'+query)[2], PAUSE)

    def test_speech_and_digits_are_forwarded_without_bootstrap(self):
        for key in ['SpeechResult', 'Digits']:
            self.assertEqual(self.run_form({**self.fields, key: 'test'})[2], PAUSE)

    def test_gather_stream_reject_and_other_pause_responses_are_untouched(self):
        for body in [b'<Response><Gather input="speech"/></Response>',
                     b'<Response><Connect><Stream url="wss://example.test"/></Connect></Response>',
                     b'<Response><Reject/></Response>', b'<Response><Pause length="5"/></Response>', b'invalid']:
            self.assertEqual(self.run_form(send=lambda *_, body=body: (200, 'application/xml', body))[2], body)

    def test_query_is_preserved_and_xml_escaped(self):
        _, _, body = self.run_form(target=PATH+'?example=one%20two&key=value')
        xml = ET.fromstring(body)
        self.assertEqual(xml[1].text, ORIGIN+PATH+'?example=one%20two&key=value&classic_answered=1')
        self.assertIn(b'&amp;', body)

    def test_ambiguous_identity_fields_are_rejected_before_forwarding(self):
        raw = urllib.parse.urlencode(self.fields).encode()+b'&From=%2B15555550124'
        self.assertEqual(process(PATH, raw, 'sig', SETTINGS, self.send)[0], 400)
        self.assertEqual(self.sent, [])

    def test_wrong_paths_absolute_urls_oversized_and_invalid_forms_are_rejected(self):
        for target in ['/other', 'https://evil.example'+PATH, PATH+'#fragment', PATH+'?'+'x'*2049]:
            self.assertEqual(process(target, b'', 'sig', SETTINGS, self.send)[0], 404)
        self.assertEqual(process(PATH, b'x'*16385, 'sig', SETTINGS, self.send)[0], 413)
        self.assertEqual(process(PATH, b'\xff', 'sig', SETTINGS, self.send)[0], 400)
        self.assertEqual(self.sent, [])

    def test_upstream_errors_are_never_replaced_with_successful_audio(self):
        for status in [400, 401, 403, 429, 500, 502]:
            self.assertEqual(self.run_form(send=lambda *_, status=status: (status, 'text/plain', b'failure')),
                             (status, 'text/plain', b'failure'))


if __name__ == '__main__':
    unittest.main()
