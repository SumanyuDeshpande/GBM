import io
import json
import unittest
import urllib.error
from unittest.mock import patch
from gbm.core import GBMError
from gbm.groq_client import GroqClient


class GroqClientTests(unittest.TestCase):
    def test_tool_request_keeps_key_out_of_payload(self):
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'Done'}}]}).encode())
        with patch('urllib.request.urlopen',return_value=response) as request:
            result=GroqClient('private-test-key').complete([{'role':'user','content':'Fix it'}],[])
        sent=request.call_args.args[0]
        self.assertEqual(sent.get_header('Authorization'),'Bearer private-test-key')
        self.assertNotIn(b'private-test-key',sent.data)
        self.assertTrue(sent.get_header('User-agent').startswith('GBM/'))
        self.assertEqual(sent.get_header('Accept'),'application/json')
        self.assertEqual(result['content'],'Done')

    def test_json_error_details_are_visible_and_credentials_redacted(self):
        body={'error':{'message':'Blocked private-test-key gsk_otherkey Bearer another-key\nTry again',
                       'type':'permissions_error','code':'model_permission_blocked_project'}}
        error=urllib.error.HTTPError('https://api.groq.com',403,'denied',{},io.BytesIO(json.dumps(body).encode()))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(GBMError) as caught:
                GroqClient('private-test-key').complete([],[])
        message=str(caught.exception)
        self.assertIn('HTTP 403',message)
        self.assertIn('model_permission_blocked_project',message)
        for secret in ('private-test-key','gsk_otherkey','another-key','\n'):
            self.assertNotIn(secret,message)

    def test_gateway_error_code_without_html_body(self):
        error=urllib.error.HTTPError('https://api.groq.com',403,'denied',{},io.BytesIO(b'<html>private markup error code: 1010</html>'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaisesRegex(GBMError,'Gateway error code 1010') as caught:
                GroqClient('private-test-key').complete([],[])
        self.assertNotIn('markup',str(caught.exception))

    def test_error_details_are_bounded(self):
        from gbm.groq_client import error_detail
        self.assertEqual(len(error_detail(json.dumps({'error':{'message':'x'*2000}}).encode(),'key')),1000)

    def test_rate_limit_has_actionable_error_without_body(self):
        error=urllib.error.HTTPError('https://api.groq.com',429,'quota',{},io.BytesIO(b'sensitive body'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaisesRegex(GBMError,'rate limit') as caught:
                GroqClient('private-test-key').complete([],[])
        self.assertNotIn('sensitive',str(caught.exception))

    def test_malformed_response_is_rejected(self):
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{}')):
            with self.assertRaisesRegex(GBMError,'invalid agent response'):
                GroqClient('private-test-key').complete([],[])
