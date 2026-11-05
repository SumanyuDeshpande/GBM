import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from gbm import setup
from gbm.cli import main
from gbm.core import GBMError
from gbm.groq_client import GroqClient


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'settings.json'
        patcher=patch('gbm.setup.settings_path',return_value=self.path)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher=patch.dict(os.environ,{},clear=True)
        patcher.start(); self.addCleanup(patcher.stop)

    def flow(self, answers=('1','y'), previous=None, failure=None):
        self.store=Mock(); self.store.get.return_value=previous
        self.client=Mock(); self.client.list_models.return_value=['chosen-model']
        self.client.complete.return_value={'tool_calls':[{'function':{'name':'gbm_setup_check','arguments':'{"ok":true}'}}]}
        if failure: self.client.complete.side_effect=failure
        self.output=[]
        with patch('gbm.setup.CredentialStore',return_value=self.store),patch('gbm.setup.hidden_key',return_value='test-key'),patch('gbm.setup.GroqClient',return_value=self.client):
            setup.run(ask=Mock(side_effect=answers),emit=self.output.append)

    def test_saves_only_nonsecret_settings_after_verified_tool_call(self):
        self.flow()
        self.store.set.assert_called_once_with('test-key')
        self.assertEqual(json.loads(self.path.read_text()),{'provider':'groq','model':'chosen-model'})
        self.assertNotIn('test-key',self.path.read_text()+''.join(self.output))
        self.assertEqual(self.client.complete.call_args.kwargs['tool_choice']['function']['name'],'gbm_setup_check')

    def test_cancel_preserves_saved_key_and_model(self):
        setup.save('old-model'); self.flow(('1','n'),previous='old-key')
        self.store.set.assert_not_called()
        self.assertEqual(setup.load()['model'],'old-model')

    def test_failed_connection_never_saves(self):
        with self.assertRaisesRegex(GBMError,'blocked'): self.flow(failure=GBMError('blocked'))
        self.store.set.assert_not_called(); self.assertFalse(self.path.exists())

    def test_failed_settings_write_restores_previous_credential(self):
        with patch('gbm.setup.save',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.flow(previous='old-key')
        self.assertEqual(self.store.set.call_args_list[-1].args,('old-key',))

    def test_precedence_and_saved_key(self):
        setup.save('saved-model')
        with patch('gbm.setup.CredentialStore') as store:
            store.return_value.get.return_value='saved-key'
            self.assertEqual(setup.resolve(),('saved-key','saved-model'))
            with patch.dict(os.environ,{'GROQ_API_KEY':'env-key','GROQ_MODEL':'env-model'}):
                self.assertEqual(setup.resolve('explicit'),('env-key','explicit'))
                self.assertEqual(setup.resolve(),('env-key','env-model'))

    def test_setup_bypasses_project_discovery_and_watch(self):
        with patch('gbm.setup.run') as run,patch('gbm.cli.Project.discover',side_effect=AssertionError('must not discover')):
            self.assertEqual(main(['setup']),0)
        run.assert_called_once()

    def test_no_tool_call_cannot_pass_verification(self):
        client=Mock(); client.complete.return_value={'content':'Looks fine'}
        with self.assertRaisesRegex(GBMError,'expected tool call'): setup.verify(client)

    def test_hidden_input_refuses_visible_fallback(self):
        import getpass
        with patch('getpass.getpass',side_effect=getpass.GetPassWarning('no terminal')):
            with self.assertRaisesRegex(GBMError,'Hidden input'): setup.hidden_key()

    def test_list_models_fetches_authenticated_endpoint_and_filters_inactive(self):
        raw={'data':[{'id':'b','active':True},{'id':'a'},{'id':'blocked','active':False}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(raw).encode())) as request:
            self.assertEqual(GroqClient('test-key').list_models(),['a','b'])
        sent=request.call_args.args[0]
        self.assertEqual(sent.full_url,'https://api.groq.com/openai/v1/models')
        self.assertEqual(sent.get_method(),'GET')
        self.assertEqual(sent.get_header('Authorization'),'Bearer test-key')
