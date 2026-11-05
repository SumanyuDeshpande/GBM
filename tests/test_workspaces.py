import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import test_core
from gbm.core import GBMError
from gbm import workspaces

class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        test_core.HistoryTests.setUp(self)
        self.destination_temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.destination_temp.cleanup)
        self.parent=Path(self.destination_temp.name).resolve()
    write=test_core.HistoryTests.write

    def prepare(self,name='new-auth'):
        return workspaces.prepare(self.p,name,self.parent)

    def create(self,plan):
        with patch('gbm.workspaces.bootstrap_pip'):
            return workspaces.create(self.p,plan,lambda:None)

    def test_copy_including_empty_folders_and_fresh_environment(self):
        self.write('main.py','original'); self.write('requirements.txt','example-package==1\n')
        self.write('.env','secret')
        (self.root/'empty').mkdir()
        (self.root/'custom-env').mkdir(); (self.root/'custom-env'/'pyvenv.cfg').write_text('old environment')
        (self.root/'custom-env'/'private-package').write_text('do not copy')
        result=self.create(self.prepare())
        destination=Path(result['destination'])
        self.assertEqual(result['status'],'READY')
        self.assertEqual((destination/'main.py').read_text(),'original')
        self.assertTrue((destination/'empty').is_dir())
        self.assertFalse((destination/'.env').exists())
        self.assertFalse((destination/'custom-env').exists())
        self.assertTrue((destination/'.venv'/'pyvenv.cfg').exists())
        self.assertFalse((destination/'.gbm'/'history.db').exists())
        python=Path(result['interpreter'])
        output=subprocess.check_output([str(python),'-I','-c','import sys; print(sys.prefix); print(sys.base_prefix)'],text=True)
        prefix,base=output.strip().splitlines()
        self.assertNotEqual(prefix,base)
        (destination/'main.py').write_text('experiment')
        self.assertEqual((self.root/'main.py').read_text(),'original')

    def test_snapshot_uses_saved_contents_even_if_source_later_changes(self):
        self.write('a','one'); plan=self.prepare(); self.write('a','two')
        result=self.create(plan)
        self.assertEqual((Path(result['destination'])/'a').read_text(),'one')
        self.assertEqual((self.root/'a').read_text(),'two')

    def test_no_overwrite_and_path_boundaries(self):
        self.write('a','one'); plan=self.prepare()
        destination=Path(plan['destination']); destination.mkdir(); (destination/'precious').write_text('keep')
        with self.assertRaises(GBMError): self.create(plan)
        self.assertEqual((destination/'precious').read_text(),'keep')
        with self.assertRaises(GBMError): workspaces.prepare(self.p,'inside',self.root)
        with self.assertRaises(GBMError): workspaces.prepare(self.p,self.root.name,self.root.parent)
        for name in ['../escape','new auth','','CON','nul','a/b','a\\b','x'*65]:
            with self.subTest(name=name),self.assertRaises(GBMError): self.prepare(name)

    def test_default_destination_is_outside_source(self):
        plan=workspaces.prepare(self.p,'named')
        destination=Path(plan['destination'])
        self.assertFalse(destination.is_relative_to(self.root))
        self.assertEqual(destination.name,'named')
        self.assertIn('.gbm-workspaces',destination.parts)

    def test_setup_failure_is_retained_and_original_untouched(self):
        self.write('a','original'); plan=self.prepare()
        with patch('gbm.workspaces.bootstrap_pip',side_effect=GBMError('setup failed')), self.assertRaises(GBMError):
            workspaces.create(self.p,plan,lambda:None)
        metadata=Path(plan['destination'])/'.gbm'/'workspace.json'
        self.assertEqual(json.loads(metadata.read_text())['status'],'INCOMPLETE')
        self.assertEqual((self.root/'a').read_text(),'original')
        with self.assertRaises(GBMError): self.prepare()

    def test_stop_cancels_copy_and_marks_incomplete(self):
        self.write('a','one'); plan=self.prepare(); calls=[]
        def active():
            calls.append(1)
            if len(calls)>1: raise GBMError('Monitoring is inactive')
        with self.assertRaises(GBMError): workspaces.create(self.p,plan,active)
        data=json.loads((Path(plan['destination'])/'.gbm'/'workspace.json').read_text())
        self.assertEqual(data['status'],'INCOMPLETE')
        self.assertFalse((self.root/'.venv').exists())

    def test_cli_creates_workspace_with_working_pip_and_watch_gate(self):
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        def cli(*args,cwd=None):
            return subprocess.run([sys.executable,'-m','gbm',*args],cwd=cwd or self.root,env=env,
                                  text=True,capture_output=True,timeout=90)
        self.write('main.py','print("original")\n')
        self.assertIn('Monitoring is inactive',cli('try','demo','--path',str(self.parent)).stderr)
        try:
            started=cli('watch'); self.assertEqual(started.returncode,0,started.stderr)
            result=cli('try','demo','--path',str(self.parent))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            destination=self.parent/'demo'
            metadata=json.loads((destination/'.gbm'/'workspace.json').read_text())
            pip=subprocess.run([metadata['interpreter'],'-I','-m','pip','--version'],text=True,capture_output=True,timeout=15)
            self.assertEqual(pip.returncode,0,pip.stderr)
            self.assertIn(str(destination/'.venv').lower(),pip.stdout.lower())
            inactive=cli('history',cwd=destination)
            self.assertIn('Monitoring is inactive',inactive.stderr)
            self.assertEqual(cli('stop').returncode,0)
            self.assertIn('Monitoring is inactive',cli('try','second','--path',str(self.parent)).stderr)
        finally:
            cli('stop'); time.sleep(1)

    def test_stop_during_preparation_does_not_create_destination(self):
        self.write('a','one'); calls=[]
        def active():
            calls.append(1)
            if len(calls)>1: raise GBMError('Monitoring is inactive')
        with self.assertRaises(GBMError):
            workspaces.prepare(self.p,'cancelled',self.parent,active)
        self.assertFalse((self.parent/'cancelled').exists())
