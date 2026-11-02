import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_core
from gbm.core import GBMError
from gbm import experiments

class ExperimentTests(unittest.TestCase):
    setUp=test_core.HistoryTests.setUp
    write=test_core.HistoryTests.write

    def execute(self,plan):
        report,path=experiments.execute(self.p,plan,lambda: None)
        self.assertTrue(path.exists())
        return report

    def test_discovery_never_imports(self):
        self.write('sample.py', "raise RuntimeError('must not import')\ndef divide(x: int):\n return 10/x\n")
        plan=experiments.build_plan(self.p,cases=3)
        self.assertEqual(len(plan['probes']),3)
        self.assertEqual(plan['probes'][1]['inputs'],{'x':0})
        self.assertFalse((self.p.home/'experiments').exists())

    def test_findings_and_replay_source_preservation(self):
        self.write('sample.py', 'def divide(x: int):\n return 10/x\n')
        plan=experiments.build_plan(self.p,str(self.root/'sample.py')+':divide',cases=3,timeout=3)
        report=self.execute(plan)
        self.assertEqual(report['results'][0]['status'],'COMPLETED')
        self.assertEqual(report['results'][1]['status'],'FINDING')
        self.assertEqual(report['results'][1]['exception'],'ZeroDivisionError')
        self.write('sample.py','def divide(x):\n return 42\n')
        replay=experiments.load_plan(self.p,report['id'])
        self.assertEqual(replay['manifest'],plan['manifest'])
        self.assertEqual(self.execute(replay)['results'][1]['status'],'FINDING')
        self.assertIn('42',(self.root/'sample.py').read_text())

    def test_copy_is_reset_between_cases(self):
        self.write('sample.py', "from pathlib import Path\ndef touch(x: int):\n p=Path('counter'); assert not p.exists(); p.write_text('x'); return x\n")
        report=self.execute(experiments.build_plan(self.p,cases=2,timeout=3))
        self.assertEqual([r['status'] for r in report['results']],['COMPLETED','COMPLETED'])
        self.assertFalse((self.root/'counter').exists())

    def test_timeout(self):
        self.write('sample.py','def hang(x):\n while True: pass\n')
        report=self.execute(experiments.build_plan(self.p,cases=1,timeout=.5,budget=2))
        self.assertEqual(report['results'][0]['status'],'TIMEOUT')

    def test_missing_dependency_is_inconclusive(self):
        self.write('sample.py','import gbm_nonexistent_dependency_123\ndef run(x): return x\n')
        report=self.execute(experiments.build_plan(self.p,cases=1,timeout=3))
        self.assertEqual(report['results'][0]['status'],'INCONCLUSIVE')
        self.assertEqual(report['results'][0]['phase'],'import')

    def test_rejected_input_not_called_a_bug(self):
        self.write('sample.py',"def reject(x):\n raise ValueError('invalid input')\n")
        report=self.execute(experiments.build_plan(self.p,cases=1,timeout=3))
        self.assertEqual(report['results'][0]['status'],'REJECTED_INPUT')

    def test_unsupported_and_limits(self):
        self.write('sample.py','async def run(x): return x\n')
        with self.assertRaises(GBMError): experiments.build_plan(self.p)
        for cases,timeout,budget in [(0,2,30),(101,2,30),(1,0,30),(1,float('nan'),30),(1,2,0)]:
            with self.assertRaises(GBMError): experiments.build_plan(self.p,cases=cases,timeout=timeout,budget=budget)
        for value in ['../outside','abc',None]:
            with self.assertRaises(GBMError): experiments.load_plan(self.p,value)

    def test_stop_cancels_running_worker_and_saves_report(self):
        self.write('sample.py','def hang(x):\n while True: pass\n')
        plan=experiments.build_plan(self.p,cases=1,timeout=3)
        calls=[]
        def active():
            calls.append(1)
            if len(calls)>3: raise GBMError('Monitoring is inactive')
        with self.assertRaisesRegex(GBMError,'inactive'):
            experiments.execute(self.p,plan,active)
        reports=list((self.p.home/'experiments').glob('*/report.json'))
        self.assertEqual(json.loads(reports[0].read_text())['status'],'CANCELLED')

    def test_cli_review_run_and_watch_gate(self):
        import os,subprocess,sys,time
        self.write('sample.py', "from pathlib import Path\nPath('import-marker').write_text('imported')\ndef divide(x: int):\n return 10/x\n")
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        def cli(*args,input=None):
            return subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,env=env,
                                  input=input,text=True,capture_output=True,timeout=20)
        self.assertIn('Monitoring is inactive',cli('experiment','--plan').stderr)
        try:
            result=cli('watch'); self.assertEqual(result.returncode,0,result.stderr)
            result=cli('experiment','sample.py:divide','--cases','2','--plan')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('Plan only',result.stdout)
            self.assertFalse((self.root/'import-marker').exists())
            result=cli('experiment','--cases','2',input='n\n')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertFalse((self.p.home/'experiments').exists())
            result=cli('experiment','sample.py:divide','--cases','2','--yes')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('FINDING=1',result.stdout)
            self.assertFalse((self.root/'import-marker').exists())
            self.assertEqual(cli('stop').returncode,0)
            self.assertIn('Monitoring is inactive',cli('experiment','--plan').stderr)
        finally:
            cli('stop'); time.sleep(1)

    def test_process_exit_is_crash(self):
        self.write('sample.py','import os\ndef quit_now(x):\n os._exit(3)\n')
        report=self.execute(experiments.build_plan(self.p,cases=1,timeout=3))
        self.assertEqual(report['results'][0]['status'],'CRASH')
