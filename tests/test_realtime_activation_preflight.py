import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('activation',Path(__file__).parents[1]/'bin/activate-realtime-voice.py')
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
KEY='sk-proj-'+('x'*40)


class AudioPreflightTests(unittest.TestCase):
    def check(self,receipt,returncode=0):
        return subprocess.CompletedProcess([],returncode,json.dumps(receipt),'')

    def test_exhausted_credit_blocks_activation_without_exposing_credentials(self):
        with patch.object(m.subprocess,'run',return_value=self.check({'passed':False,'error':'credit_balance_exhausted'},1)) as run:
            with self.assertRaises(m.AudioPreflightError) as caught:
                m.verify_audio({'api_key':KEY,'model':'gpt-realtime-2.1'})
        self.assertIn('credit balance exhausted',str(caught.exception))
        self.assertNotIn(KEY,str(caught.exception))
        args,kwargs=run.call_args
        self.assertNotIn(KEY,' '.join(args[0]))
        self.assertEqual(json.loads(kwargs['input'])['api_key'],KEY)
        self.assertEqual(args[0][:3],['docker','exec','-i'])

    def test_model_visibility_or_empty_audio_cannot_pass(self):
        for receipt in [{'passed':True,'audioBytes':0},{'passed':False,'audioBytes':1600}]:
            with patch.object(m.subprocess,'run',return_value=self.check(receipt)):
                with self.assertRaises(m.AudioPreflightError):
                    m.verify_audio({'api_key':KEY,'model':'gpt-realtime-2.1'})

    def test_real_audio_receipt_passes(self):
        with patch.object(m.subprocess,'run',return_value=self.check({'passed':True,'audioBytes':1600})):
            m.verify_audio({'api_key':KEY,'model':'gpt-realtime-2.1'})

    def test_raw_errors_are_not_exposed(self):
        with patch.object(m.subprocess,'run',side_effect=RuntimeError(KEY)):
            with self.assertRaises(m.AudioPreflightError) as caught:
                m.verify_audio({'api_key':KEY,'model':'gpt-realtime-2.1'})
        self.assertNotIn(KEY,str(caught.exception))
