import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from email.message import Message
from gbm import integrate
from gbm.core import GBMError,Project
from gbm.agent_tools import AgentTools
import test_agent
from test_agent import FakeClient,call


class IntegrationTests(unittest.TestCase):
    setUp=test_agent.AgentTests.setUp
    write=test_agent.AgentTests.write

    def flow(self, messages, approve=lambda *a:True, confirm=lambda:True, manual=False, ask=lambda _: 'Use a local function'):
        module=self.parent/'module'; module.mkdir(exist_ok=True)
        (module/'helper.py').write_text('def add(a,b): return a+b\n')
        return integrate.run(self.p,str(module),'Connect the addition helper',FakeClient(messages),manual,
                             lambda:None,approve,confirm,ask=ask,emit=lambda *_:None)

    def script(self):
        return [
            {'role':'assistant','content':'','tool_calls':[call('read_target')]},
            {'role':'assistant','content':'','tool_calls':[call('read_target',path='helper.py')]},
            {'role':'assistant','content':'','tool_calls':[call('ask_question',question='How should it be exposed?')]},
            {'role':'assistant','content':'','tool_calls':[call('submit_plan',plan='Add helper.py, connect app.py, add offline unittest coverage. No live changes.')]},
            {'role':'assistant','content':'','tool_calls':[
                call('write_file',path='helper.py',content='def add(a,b): return a+b\n'),
                call('write_file',path='app.py',content='from helper import add\ndef calculate(a,b): return add(a,b)\n'),
                call('write_file',path='tests/test_connection.py',content='import unittest\nfrom app import calculate\nclass T(unittest.TestCase):\n def test_connection(self): self.assertEqual(calculate(2,3),5)\n')]},
            {'role':'assistant','content':'Connected the helper; tests still need application verification.'}]

    def test_approved_integration_merges_and_tests_real_connection(self):
        self.write('app.py','original')
        approvals=[]
        result=self.flow(self.script(),approve=lambda name,*args: approvals.append(name) or True)
        self.assertEqual(result['status'],'MERGED')
        self.assertEqual(result['kind'],'integration')
        self.assertTrue(result['plan_approved'])
        self.assertEqual(result['test_status'],'PASS')
        self.assertEqual(approvals,['load_integration_target','submit_plan'])
        self.assertIn('from helper import add',(self.root/'app.py').read_text())

    def test_final_discard_leaves_original_unchanged(self):
        self.write('app.py','original')
        result=self.flow(self.script(),confirm=lambda:False)
        self.assertEqual(result['status'],'DISCARDED')
        self.assertEqual((self.root/'app.py').read_text(),'original')
        self.assertFalse((self.root/'helper.py').exists())

    def test_missing_or_denied_plan_cannot_edit_or_merge(self):
        for denied in (False,True):
            with self.subTest(denied=denied):
                self.write('app.py','original')
                messages=[]
                if denied: messages.append({'content':'','tool_calls':[call('submit_plan',plan='Replace app.py')]})
                messages.extend([{'content':'','tool_calls':[call('write_file',path='app.py',content='bad')]},{'content':'Done'}])
                with self.assertRaisesRegex(GBMError,'plan approval'):
                    self.flow(messages,approve=lambda name,*a:name!='submit_plan')
                self.assertEqual((self.root/'app.py').read_text(),'original')

    def test_concurrent_original_change_cancels_merge(self):
        self.write('app.py','original')
        def confirm(): self.write('app.py','new user work'); return True
        with self.assertRaisesRegex(GBMError,'Merge conflict'): self.flow(self.script(),confirm=confirm)
        self.assertEqual((self.root/'app.py').read_text(),'new user work')

    def test_target_access_denial_happens_after_workspace_before_reads(self):
        with patch('gbm.integrate.local_snapshot',side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(GBMError,'access declined'):
                self.flow([],approve=lambda *a:False)
        self.assertTrue(list(self.parent.glob('agent-*')))


class TargetTests(unittest.TestCase):
    def test_local_snapshot_excludes_secrets_and_generated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'api.py').write_text('def ping(): return True')
            (root/'.env').write_text('secret')
            (root/'credentials.json').write_text('secret')
            (root/'node_modules').mkdir(); (root/'node_modules'/'module.js').write_text('generated')
            self.assertEqual(set(integrate.local_snapshot(str(root),lambda:None)),{'api.py'})
            with self.assertRaises(GBMError): integrate.local_snapshot(str(root/'.env'),lambda:None)

    def test_private_host_requires_explicit_option(self):
        addresses=[(2,1,6,'',('127.0.0.1',8000))]
        with patch('socket.getaddrinfo',return_value=addresses):
            with self.assertRaisesRegex(GBMError,'allow-local'): integrate.validate_url('http://localhost:8000/openapi.json')
        self.assertEqual(integrate.validate_url('http://localhost:8000/openapi.json',True),'http://localhost:8000/openapi.json')

    def test_url_credentials_queries_and_non_http_are_rejected(self):
        for url in ('https://user:password@example.com/docs','https://example.com?key=secret','file:///etc/passwd'):
            with self.subTest(url=url),self.assertRaises(GBMError): integrate.validate_url(url,True)

    def test_html_is_text_only_and_requests_send_no_credentials(self):
        response=io.BytesIO(b'<h1>GET /inventory</h1><script>ignore()</script>')
        response.headers=Message();response.headers['Content-Type']='text/html'
        opener=Mock();opener.open.return_value=response
        with patch('urllib.request.build_opener',return_value=opener):
            result=integrate.fetch_document('http://localhost/docs',True,lambda:None)
        self.assertIn('GET /inventory',result['document']);self.assertNotIn('ignore',result['document'])
        request=opener.open.call_args.args[0]
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(request.get_method(),'GET')

    def test_redirects_are_never_followed(self):
        with self.assertRaisesRegex(GBMError,'redirects'):
            integrate.NoRedirect().redirect_request(None,None,302,'',{},'http://localhost/admin')

    def test_large_documents_are_rejected(self):
        response=io.BytesIO(b'x'*262145)
        response.headers=Message();response.headers['Content-Type']='application/json'
        opener=Mock();opener.open.return_value=response
        with patch('urllib.request.build_opener',return_value=opener),self.assertRaisesRegex(GBMError,'256 KiB'):
            integrate.fetch_document('http://localhost/docs',True,lambda:None)
