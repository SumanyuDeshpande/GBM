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
        self.assertEqual(result['content'],'Done')

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
