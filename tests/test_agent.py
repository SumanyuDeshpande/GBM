import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_core
from gbm import agent, sessions
from gbm.agent_tools import AgentTools
from gbm.core import GBMError, Project
from gbm.groq_client import GroqClient


def call(name,**arguments):
    return {'id':'tool-'+name,'type':'function','function':{'name':name,'arguments':json.dumps(arguments)}}


class FakeClient:
    key='gsk-test-only-not-a-real-key'
    model='test-model'
    def __init__(self,messages): self.messages=iter(messages)
    def complete(self,messages,tools): return next(self.messages)


class AgentTests(unittest.TestCase):
    def setUp(self):
        test_core.HistoryTests.setUp(self)
        self.temp_dest=tempfile.TemporaryDirectory(); self.addCleanup(self.temp_dest.cleanup)
        self.parent=Path(self.temp_dest.name)
        # Keep test-created workspaces in a separately owned temporary directory.
        from gbm import workspaces
        original=workspaces.prepare
        self.preparer=patch('gbm.agent.workspaces.prepare',side_effect=lambda p,n,require_active: original(p,n,self.parent,require_active))
        self.preparer.start(); self.addCleanup(self.preparer.stop)
        self.pip=patch('gbm.workspaces.bootstrap_pip'); self.pip.start(); self.addCleanup(self.pip.stop)
    write=test_core.HistoryTests.write

    def run_agent(self,messages,confirm=lambda:True,manual=False,approve=lambda *args:True):
        return agent.run(self.p,'Fix the function',FakeClient(messages),manual,lambda:None,approve,confirm,emit=lambda *_:None)

    def scripted_change(self):
        return [{'role':'assistant','content':'Fixing it.','tool_calls':[
            call('write_file',path='app.py',content='def add(a,b):\n return a+b\n'),
            call('write_file',path='tests/test_app.py',content='import unittest\nfrom app import add\nclass TestAdd(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2,3),5)\n')]},
            {'role':'assistant','content':'Implementation complete.'}]

    def test_auto_edits_tests_and_merges(self):
        self.write('app.py','def add(a,b): return 0\n')
        result=self.run_agent(self.scripted_change())
        self.assertEqual(result['status'],'MERGED')
        self.assertEqual(result['test_status'],'PASS')
        self.assertIn('a+b',(self.root/'app.py').read_text())
        self.assertTrue((self.root/'tests/test_app.py').exists())
        self.assertFalse(Path(result['workspace']).exists())
        self.assertFalse(self.p.pending())

    def test_discard_removes_workspace_without_touching_source(self):
        self.write('app.py','original')
        result=self.run_agent(self.scripted_change(),confirm=lambda:False)
        self.assertEqual(result['status'],'DISCARDED')
        self.assertEqual((self.root/'app.py').read_text(),'original')
        self.assertFalse((self.root/'tests').exists())
        self.assertFalse(Path(result['workspace']).exists())

    def test_manual_requires_approval_for_each_action(self):
        approvals=[]
        def approve(name,args,diff):
            approvals.append(name)
            return name!='write_file'
        self.write('app.py','original')
        messages=[{'role':'assistant','content':'','tool_calls':[call('list_files'),call('read_file',path='app.py'),call('write_file',path='app.py',content='new')]},
                  {'role':'assistant','content':'Writes declined.'}]
        result=self.run_agent(messages,manual=True,approve=approve)
        self.assertEqual(approvals,['list_files','read_file','write_file','run_tests'])
        self.assertEqual((self.root/'app.py').read_text(),'original')
        self.assertEqual(result['status'],'NO_CHANGES')
        self.assertEqual(result['test_status'],'INCONCLUSIVE')

    def test_original_edits_block_merge_and_preserve_workspace(self):
        self.write('app.py','original')
        def confirm():
            self.write('app.py','concurrent work'); return True
        with self.assertRaisesRegex(GBMError,'Merge conflict'):
            self.run_agent(self.scripted_change(),confirm=confirm)
        self.assertEqual((self.root/'app.py').read_text(),'concurrent work')
        self.assertTrue(list(self.parent.glob('agent-*')))

    def test_merge_preserves_existing_session(self):
        self.write('app.py','original'); sessions.start(self.p)
        session=self.p.get('session')
        self.run_agent(self.scripted_change())
        self.assertEqual(self.p.get('session'),session)

    def test_failed_tests_are_reported_before_explicit_merge(self):
        self.write('app.py','original')
        messages=self.scripted_change()
        messages[0]['tool_calls'][1]=call('write_file',path='tests/test_app.py',content='import unittest\nclass T(unittest.TestCase):\n def test_bad(self): self.fail("failure")\n')
        result=self.run_agent(messages,confirm=lambda:False)
        self.assertEqual(result['test_status'],'FAIL')
        self.assertEqual(result['status'],'DISCARDED')

    def test_provider_error_never_merges(self):
        client=FakeClient([])
        client.complete=lambda *args: (_ for _ in ()).throw(GBMError('Groq rate limit reached'))
        self.write('app.py','original')
        with self.assertRaisesRegex(GBMError,'rate limit'):
            agent.run(self.p,'Fix',client,False,lambda:None,lambda *args:True,lambda:True,emit=lambda *_:None)
        self.assertEqual((self.root/'app.py').read_text(),'original')
        report=json.loads(next((self.p.home/'agents').glob('*.json')).read_text())
        self.assertEqual(report['status'],'INCOMPLETE')
        self.assertNotIn(client.key,json.dumps(report))

    def test_interrupted_merge_has_recoverable_journal(self):
        self.write('app.py','original'); original=sessions.write
        def partial(project,before,target):
            (project.root/'app.py').write_text('partial'); raise OSError('interrupted')
        with patch('gbm.agent.sessions.write',side_effect=partial),self.assertRaises(OSError):
            self.run_agent(self.scripted_change())
        self.assertTrue(self.p.pending())
        self.p.recover()
        self.assertEqual((self.root/'app.py').read_text(),'original')
        self.assertFalse(self.p.pending())

    def test_missing_key_rejected_without_network(self):
        with self.assertRaisesRegex(GBMError,'GROQ_API_KEY'): GroqClient('')

    def test_tools_block_external_secret_internal_and_unknown_actions(self):
        other=self.parent/'workspace'; other.mkdir()
        workspace=Project(other); workspace.initialize()
        tools=AgentTools(self.p,workspace,Path(os.sys.executable),lambda:None,lambda *a:True)
        for path in ['../escape','/outside','.gbm/config.json','.venv/secret','.env','x/private.key']:
            with self.subTest(path=path),self.assertRaises(GBMError): tools.invoke('write_file',{'path':path,'content':'bad'})
        with self.assertRaises(GBMError): tools.invoke('shell',{'command':'anything'})
        with self.assertRaises(GBMError): tools.invoke('run_tests',{'runner':'shell'})

    def test_test_subprocess_does_not_receive_groq_key(self):
        messages=[{'role':'assistant','content':'','tool_calls':[call('write_file',path='tests/test_env.py',content='import unittest,os\nclass T(unittest.TestCase):\n def test_env(self): self.assertNotIn("GROQ_API_KEY",os.environ)\n')]},
                  {'role':'assistant','content':'Done'}]
        with patch.dict(os.environ,{'GROQ_API_KEY':'never-send-to-child'}):
            result=self.run_agent(messages,confirm=lambda:False)
        self.assertEqual(result['test_status'],'PASS')
