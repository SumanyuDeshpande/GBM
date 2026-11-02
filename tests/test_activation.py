import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
import test_core
from gbm.cli import main
from gbm import sessions

class ActivationTests(unittest.TestCase):
    setUp = test_core.HistoryTests.setUp
    write = test_core.HistoryTests.write

    def cli(self,*args,input=None):
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        return subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,env=env,
                              input=input,capture_output=True,text=True,timeout=20)

    def start(self):
        result=self.cli('watch')
        self.assertEqual(result.returncode,0,result.stderr)
        self.addCleanup(self.cleanup_watcher)

    def cleanup_watcher(self):
        self.cli('stop'); time.sleep(1)

    def assert_blocked(self,*args):
        result=self.cli(*args)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('Monitoring is inactive',result.stderr)
        self.assertIn("gbm watch",result.stderr)

    def test_all_project_commands_blocked_before_watch_and_after_stop(self):
        commands=[['history'],['history','a'],['undo'],['undo','a'],['redo'],['redo','a'],
                  ['session','start'],['session','end'],['session','status'],['session','recover'],
                  ['recover','--yes'],['stop']]
        for args in commands:
            with self.subTest(phase='before',args=args): self.assert_blocked(*args)
        self.assertEqual(self.cli('status').returncode,0)
        self.assertEqual(self.cli('--help').returncode,0)
        self.assertEqual(self.cli('session','--help').returncode,0)
        self.write('a','one'); self.start()
        self.assertEqual(self.cli('history').returncode,0)
        self.assertEqual(self.cli('session','start').returncode,0)
        self.assertEqual(self.cli('stop').returncode,0)
        for args in commands:
            with self.subTest(phase='after',args=args): self.assert_blocked(*args)
        self.assertIsNotNone(self.p.get('session'))
        self.assertEqual(self.cli('watch').returncode,0)
        self.assertEqual(self.cli('session','end',input='y\n').returncode,0)

    def test_stale_heartbeat_does_not_activate(self):
        self.p.set('watcher',{'running':True,'pid':os.getpid(),'heartbeat':time.time()-60})
        self.assert_blocked('history')

    def test_stop_while_session_prompt_open_blocks_completion(self):
        self.write('a','one'); self.start()
        self.assertEqual(self.cli('session','start').returncode,0)
        self.write('a','two')
        def stop_at_prompt(_):
            self.assertEqual(self.cli('stop').returncode,0)
            return 'n'
        errors=io.StringIO()
        with patch('gbm.cli.Path.cwd',return_value=self.root), patch('builtins.input',side_effect=stop_at_prompt), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            code=main(['session','end'])
        self.assertEqual(code,1)
        self.assertIn('Monitoring is inactive',errors.getvalue())
        self.assertEqual((self.root/'a').read_text(),'two')
        self.assertIsNotNone(self.p.get('session'))

    def test_watch_can_restart_with_pending_recovery(self):
        self.write('a','one'); self.start()
        self.assertEqual(self.cli('session','start').returncode,0)
        self.write('a','two')
        def fail(*args): raise OSError('simulated interruption')
        with self.p.lock():
            plan=sessions.preview(self.p)
            with patch('gbm.sessions.write',side_effect=fail), self.assertRaises(OSError):
                sessions.finish(self.p,plan,False)
        self.assertEqual(self.cli('stop').returncode,0)
        self.assert_blocked('recover','--yes')
        result=self.cli('watch')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('gbm recover',result.stdout)
        recovered=self.cli('recover','--yes')
        self.assertEqual(recovered.returncode,0,recovered.stderr)
        self.assertFalse(self.p.pending())
        self.assertIsNotNone(self.p.get('session'))
