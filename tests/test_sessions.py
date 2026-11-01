import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import test_core
from gbm import sessions
from gbm.core import Project, GBMError

class SessionTests(unittest.TestCase):
    setUp = test_core.HistoryTests.setUp
    write = test_core.HistoryTests.write

    def start(self):
        sessions.start(self.p)
    def end(self, keep=False):
        sessions.finish(self.p, sessions.preview(self.p), keep)

    def test_discard_files_folders_and_uncommitted_work(self):
        self.write('a','uncommitted'); (self.root/'empty').mkdir()
        self.write('deleted','original'); self.start()
        original=sessions.snapshot(self.p)
        self.write('a','changed'); (self.root/'deleted').unlink()
        (self.root/'empty').rmdir(); (self.root/'new'/'nested').mkdir(parents=True)
        (self.root/'new'/'nested'/'binary').write_bytes(b'\x00\xff')
        self.end(); self.assertEqual(sessions.snapshot(self.p),original)
        self.assertIsNone(self.p.get('session'))

    def test_keep_and_persistence(self):
        self.write('a','one'); self.start()
        self.p=Project(self.root)
        self.assertIsNotNone(self.p.get('session'))
        self.write('a','two'); (self.root/'empty').mkdir()
        self.end(keep=True)
        self.assertEqual((self.root/'a').read_text(),'two')
        self.assertTrue((self.root/'empty').is_dir())
        self.assertIsNone(self.p.get('session'))

    def test_no_nested_sessions_and_missing_session(self):
        with self.assertRaises(GBMError): sessions.preview(self.p)
        self.start()
        with self.assertRaises(GBMError): self.start()

    def test_snapshot_does_not_move_when_watcher_captures(self):
        self.write('a','one'); self.start()
        self.write('a','two'); self.p.capture()
        self.write('a','three'); self.p.capture()
        self.end(); self.assertEqual((self.root/'a').read_text(),'one')
        self.assertIsNone(self.p.capture())

    def test_discard_recovery_including_empty_folders(self):
        self.write('a','one'); self.start()
        self.write('a','two'); (self.root/'newempty').mkdir()
        experiment=sessions.snapshot(self.p); self.end()
        plan=sessions.preview(self.p,recover=True)
        sessions.finish(self.p,plan,False)
        self.assertEqual(sessions.snapshot(self.p),experiment)

    def test_file_directory_type_changes(self):
        self.write('file','original'); (self.root/'folder').mkdir()
        (self.root/'folder'/'child').write_text('child'); self.start()
        (self.root/'file').unlink(); (self.root/'file').mkdir()
        (self.root/'file'/'child').write_text('new')
        (self.root/'folder'/'child').unlink(); (self.root/'folder').rmdir()
        self.write('folder','became file'); self.end()
        self.assertEqual((self.root/'file').read_text(),'original')
        self.assertEqual((self.root/'folder'/'child').read_text(),'child')

    def test_conflict_changes_nothing(self):
        self.write('a','one'); self.start(); self.write('a','two')
        plan=sessions.preview(self.p); self.write('a','concurrent')
        with self.assertRaises(GBMError): sessions.finish(self.p,plan,False)
        self.assertEqual((self.root/'a').read_text(),'concurrent')
        self.assertIsNotNone(self.p.get('session'))

    def test_excluded_contents_not_deleted(self):
        self.start(); (self.root/'new').mkdir(); (self.root/'new'/'.env').write_text('secret')
        with self.assertRaises(GBMError): self.end()
        self.assertEqual((self.root/'new'/'.env').read_text(),'secret')
        self.assertFalse(self.p.pending())

    def test_exclusion_policy_change_rejected(self):
        self.start()
        config=self.p.config(); config['exclude'].append('a')
        (self.p.home/'config.json').write_text(json.dumps(config))
        with self.assertRaises(GBMError): self.end()

    def test_interruption_recovery_keeps_session(self):
        self.write('a','one'); self.start(); self.write('a','two')
        (self.root/'new').mkdir(); before=sessions.snapshot(self.p)
        def interrupt(*args):
            self.write('a','partial'); raise OSError('interrupted')
        with patch('gbm.sessions.write',side_effect=interrupt), self.assertRaises(OSError): self.end()
        self.assertTrue(self.p.pending())
        with self.assertRaises(GBMError): self.p.capture()
        self.p.recover()
        self.assertEqual(sessions.snapshot(self.p),before)
        self.assertIsNotNone(self.p.get('session'))
        self.assertFalse(self.p.pending()); self.end()
        self.assertEqual((self.root/'a').read_text(),'one')

    def test_cli_start_end_and_cancel(self):
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        def cli(*args,input=None):
            return subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,env=env,
                                  input=input,text=True,capture_output=True,timeout=15)
        self.write('a','one')
        self.assertEqual(cli('session','start').returncode,0)
        self.write('a','two')
        cancelled=cli('session','end',input='')
        self.assertEqual(cancelled.returncode,130)
        self.assertIsNotNone(self.p.get('session'))
        result=cli('session','end',input='bad\nn\n')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('Would you like to maintain',result.stdout)
        self.assertEqual((self.root/'a').read_text(),'one')
        self.assertEqual(cli('session','start').returncode,0)
        self.write('a','kept')
        result=cli('session','end',input='y\n')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.root/'a').read_text(),'kept')

    def test_live_watcher_session_discard(self):
        import time
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        def cli(*args,input=None):
            result=subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,env=env,
                                  input=input,text=True,capture_output=True,timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return result
        self.write('a','one')
        try:
            cli('watch'); cli('session','start')
            self.write('a','two'); (self.root/'empty').mkdir()
            time.sleep(3)
            cli('session','end',input='n\n')
            time.sleep(3)
            self.assertEqual((self.root/'a').read_text(),'one')
            self.assertFalse((self.root/'empty').exists())
            self.assertEqual(self.p.scan(),self.p.get('baseline'))
            self.assertIn('ACTIVE',cli('status').stdout)
        finally:
            cli('stop'); time.sleep(1)
