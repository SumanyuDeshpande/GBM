import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from gbm.core import Project
from gbm.watcher import active

class WatcherTests(unittest.TestCase):
    def test_detached_watch_checkpoint_restore_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            env=os.environ.copy()
            env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
            def cli(*args):
                result=subprocess.run([sys.executable,'-m','gbm',*args],cwd=root,env=env,
                                      capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                return result.stdout
            (root/'a').write_text('one')
            try:
                cli('watch')
                self.assertIn('ACTIVE',cli('status'))
                (root/'a').write_text('two')
                p=Project(root)
                deadline=time.monotonic()+10
                while time.monotonic()<deadline:
                    if p.get('baseline',{}).get('a') == __import__('hashlib').sha256(b'two').hexdigest():
                        break
                    time.sleep(.2)
                else:
                    self.fail('Background watcher did not checkpoint the edit')
                cli('undo','a'); self.assertEqual((root/'a').read_text(),'one')
                with p.db() as db:
                    count=db.execute("SELECT COUNT(*) FROM checkpoints WHERE kind='automatic'").fetchone()[0]
                time.sleep(3)
                with p.db() as db:
                    self.assertEqual(count,db.execute("SELECT COUNT(*) FROM checkpoints WHERE kind='automatic'").fetchone()[0])
                cli('redo','a'); self.assertEqual((root/'a').read_text(),'two')
                cli('undo','--yes'); self.assertEqual((root/'a').read_text(),'one')
                cli('redo','--yes'); self.assertEqual((root/'a').read_text(),'two')
                time.sleep(3)
                with p.db() as db:
                    self.assertEqual(count,db.execute("SELECT COUNT(*) FROM checkpoints WHERE kind='automatic'").fetchone()[0])
                cli('stop'); self.assertIn('INACTIVE',cli('status'))
            finally:
                if active(Project(root)):
                    cli('stop')
                time.sleep(1)
