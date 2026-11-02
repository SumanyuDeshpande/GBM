import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import test_core
from gbm.core import GBMError
from gbm.history import history

class BrowseTests(unittest.TestCase):
    setUp = test_core.HistoryTests.setUp
    write = test_core.HistoryTests.write
    capture = test_core.HistoryTests.capture
    restore = test_core.HistoryTests.restore

    def test_file_changes_delete_recreate_and_unrelated_edits(self):
        self.capture()
        self.write('a','one'); self.capture()
        self.write('other','unrelated'); self.capture()
        self.write('a','two'); self.capture()
        (self.root/'a').unlink(); self.capture()
        self.write('a','two'); self.capture()
        entries=history(self.p,'a')
        self.assertEqual([e['change'] for e in entries],['ADDED','DELETED','MODIFIED','ADDED'])
        self.assertEqual([e['id'] for e in entries],[6,5,4,2])

    def test_project_counts_and_limit(self):
        self.write('a','one'); self.write('b','one'); self.capture()
        self.write('a','two'); (self.root/'b').unlink(); self.write('c','new'); self.capture()
        entry=history(self.p,limit=1)[0]
        self.assertEqual(entry['change'],'+1 ~1 -1')
        self.assertEqual(entry['detail'],'2 files')
        self.assertTrue(entry['head'])
        self.assertEqual(len(history(self.p)),2)

    def test_safety_does_not_change_automatic_comparisons(self):
        self.write('a','one'); self.capture()
        self.write('a','two'); self.capture()
        self.restore('a')
        default=history(self.p,'a')
        all_entries=history(self.p,'a',include_safety=True)
        self.assertEqual(len(default),2)
        self.assertEqual(all_entries[0]['kind'],'safety')
        self.assertEqual(all_entries[0]['change'],'SNAPSHOT')
        self.assertEqual([e for e in all_entries if e['kind']=='automatic'],default)

    def test_readonly_and_redo_survives_browsing(self):
        self.write('a','one'); self.capture(); self.write('a','two'); self.capture()
        self.restore(); baseline=self.p.get('baseline'); redo=self.p.get('project_redo')
        with self.p.db() as db: before=db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]
        self.write('a','pending edit')
        history(self.p); history(self.p,'a',include_safety=True)
        self.assertEqual((self.root/'a').read_text(),'pending edit')
        self.assertEqual(self.p.get('baseline'),baseline)
        self.assertEqual(self.p.get('project_redo'),redo)
        with self.p.db() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0],before)

    def test_empty_unknown_and_invalid_limit(self):
        self.assertEqual(history(self.p),[])
        self.write('a','one'); self.capture()
        self.assertEqual(history(self.p,'unknown'),[])
        for limit in [0,-1]:
            with self.assertRaises(GBMError): history(self.p,limit=limit)
        with self.assertRaises(GBMError): history(self.p,'../outside')

    def test_cli_deleted_file_and_help(self):
        self.write('a','one'); self.capture(); (self.root/'a').unlink(); self.capture()
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        import time
        def cli(*args):
            return subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,
                                  env=env,text=True,capture_output=True,timeout=15)
        try:
            started=cli('watch')
            self.assertEqual(started.returncode,0,started.stderr)
            result=cli('history','a','--limit','1')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('DELETED',result.stdout)
            self.assertNotIn('ADDED',result.stdout)
        finally:
            cli('stop'); time.sleep(1)
