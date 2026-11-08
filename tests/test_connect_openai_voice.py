import importlib.util
import unittest
import urllib.error
from pathlib import Path
spec=importlib.util.spec_from_file_location('openai_voice',Path(__file__).parents[1]/'bin/connect-openai-voice.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
KEY='sk-proj-'+('x'*40)
class Reply:
    def __init__(self,body):self.body=body
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def read(self,n):return self.body[:n]
class API:
    def __init__(self,body=None,error=None):self.body=body;self.error=error;self.req=None
    def open(self,req,timeout):
        self.req=req
        if self.error:raise self.error
        return Reply(self.body)
class OpenAIVoiceTests(unittest.TestCase):
    def test_key_rejects_wrong_ids_and_env_injection(self):
        for key in ['project_abc','sk-x','sk-'+('x'*30)+'\nOTHER=secret','sk-'+('x'*30)+';cmd']:
            with self.assertRaises(m.SetupError):m.validate_key(key)
    def test_model_check_is_read_only_and_exact(self):
        api=API(b'{"id":"gpt-realtime-2.1"}')
        m.verify_key(KEY,api)
        self.assertEqual(api.req.get_method(),'GET')
        self.assertEqual(api.req.full_url,'https://api.openai.com/v1/models/gpt-realtime-2.1')
        self.assertEqual(api.req.get_header('Authorization'),'Bearer '+KEY)
    def test_other_model_and_oversized_response_fail(self):
        for body in [b'{"id":"other"}',b'x'*65537]:
            with self.assertRaises(m.SetupError):m.verify_key(KEY,API(body))
    def test_http_errors_do_not_echo_key_or_provider_body(self):
        for code in [401,403,404,429,500]:
            error=urllib.error.HTTPError('https://api.openai.com',code,KEY,None,None)
            with self.assertRaises(m.SetupError) as caught:m.verify_key(KEY,API(error=error))
            self.assertNotIn(KEY,str(caught.exception))
    def test_redirects_are_not_followed(self):
        self.assertIsNone(m.NoRedirects().redirect_request(None,None,302,'',{},'https://other.example'))
