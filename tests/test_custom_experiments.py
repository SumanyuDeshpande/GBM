import json
from pathlib import Path
import unittest
from unittest.mock import patch
import test_core
from gbm import custom_experiments as custom
from gbm import experiments
from gbm.core import GBMError

class CustomExperimentTests(unittest.TestCase):
    setUp=test_core.HistoryTests.setUp
    write=test_core.HistoryTests.write

    def plan(self,cases,code='def add(a,b):\n return a+b\n'):
        self.write('main.py',code)
        self.write('test.py','CASES = '+repr(cases)+'\n')
        return custom.build_plan(self.p,str(self.root/'test.py'),str(self.root/'main.py'),timeout=3)

    def run_cases(self,cases,code='def add(a,b):\n return a+b\n'):
        plan=self.plan(cases,code)
        report,_=experiments.execute(self.p,plan,lambda:None)
        return report

    def test_only_supplied_function_cases_and_expected_values(self):
        with patch('gbm.experiments.samples',side_effect=AssertionError('must not generate')):
            report=self.run_cases([
                {'function':'add','inputs':{'a':2,'b':3},'expected':5},
                {'function':'add','args':[-1,1],'expected':7}])
        self.assertEqual([r['status'] for r in report['results']],['PASS','FAIL'])
        self.assertEqual(len(report['results']),2)
        self.assertEqual(report['mode'],'custom')

    def test_none_expectation_observation_and_expected_exception(self):
        report=self.run_cases([
            {'function':'run','inputs':{'x':0},'expected':None},
            {'function':'run','inputs':{'x':0}},
            {'function':'run','inputs':{'x':1},'expected_exception':'ValueError'},
            {'function':'run','inputs':{'x':0},'expected_exception':'ValueError'}],
            'def run(x):\n if x: raise ValueError("invalid")\n return None\n')
        self.assertEqual([r['status'] for r in report['results']],['PASS','COMPLETED','PASS','FAIL'])

    def test_script_input_and_exact_output_including_prompts(self):
        report=self.run_cases([
            {'stdin':'2\n3\n','expected_stdout':'a: b: 5\n'},
            {'stdin':'-1\n1\n','expected_stdout':'wrong\n'},
            {'stdin':'4\n5\n'}],
            'a=int(input("a: ")); b=int(input("b: ")); print(a+b)\n')
        self.assertEqual([r['status'] for r in report['results']],['PASS','FAIL','COMPLETED'])
        self.assertEqual(report['results'][2]['stdout'],'a: b: 9\n')

    def test_script_exit_and_finite_input(self):
        report=self.run_cases([{'stdin':'','expected_exit':7}], 'raise SystemExit(7)\n')
        self.assertEqual(report['results'][0]['status'],'PASS')
        report=self.run_cases([{'stdin':'','expected_exit':1}], 'input()\n')
        self.assertEqual(report['results'][0]['status'],'PASS')
        self.assertEqual(report['results'][0]['exception'],'EOFError')

    def test_stored_replay_preserves_cases_and_source(self):
        plan=self.plan([{'function':'add','inputs':{'a':2,'b':3},'expected':5}])
        report,_=experiments.execute(self.p,plan,lambda:None)
        self.write('main.py','raise RuntimeError("changed")\n')
        self.write('test.py','CASES=[]\n')
        replay=experiments.load_plan(self.p,report['id'])
        result,_=experiments.execute(self.p,replay,lambda:None)
        self.assertEqual(result['results'][0]['status'],'PASS')
        self.assertEqual(replay['mode'],'custom')

    def test_case_file_not_executed_and_expressions_rejected(self):
        self.write('main.py','def add(a,b): return a+b\n')
        for content in ["raise RuntimeError('do not execute')\nCASES=[]\n",
                        "CASES = __import__('os').getcwd()\n",'CASES=[]\nCASES=[]\n']:
            self.write('test.py',content)
            with self.assertRaises(GBMError):
                custom.build_plan(self.p,str(self.root/'test.py'),str(self.root/'main.py'))
        self.assertFalse((self.p.home/'experiments').exists())

    def test_invalid_schema_is_rejected_before_execution(self):
        invalid=[[],[1],[{'stdin':[]}],[{'stdin':'','function':'add'}],
                 [{'function':'missing'}],[{'function':'add','inputs':[]}],
                 [{'function':'add','args':{}}],[{'function':'add','expected':(1,2)}],
                 [{'function':'add','expected_exception':'not_an_exception'}],
                 [{'function':'add','expected':0,'expected_exception':'ValueError'}],
                 [{'stdin':'','expected_exit':True}],[{'stdin':'','expected_stdout':5}],
                 [{'stdin':'','unexpected':0}]]
        for cases in invalid:
            with self.subTest(cases=cases),self.assertRaises(GBMError): self.plan(cases)

    def test_full_output_comparison_before_display_truncation(self):
        report=self.run_cases([{'stdin':'','expected_stdout':'x'*3000+'\n'}],'print("x"*3000)\n')
        self.assertEqual(report['results'][0]['status'],'PASS')
        self.assertTrue(report['results'][0]['output_truncated'])
        self.assertEqual(len(report['results'][0]['stdout']),2000)

    def test_relative_writes_stay_in_copy(self):
        report=self.run_cases([{'stdin':'','expected_stdout':'done\n'}],
                             'from pathlib import Path\nPath("created.txt").write_text("copy")\nprint("done")\n')
        self.assertEqual(report['results'][0]['status'],'PASS')
        self.assertFalse((self.root/'created.txt').exists())

    def test_more_than_default_generated_case_count_is_not_truncated(self):
        cases=[{'function':'add','inputs':{'a':i,'b':1},'expected':i+1} for i in range(25)]
        self.assertEqual(len(self.plan(cases)['probes']),25)

    def test_cli_custom_mode_and_argument_contract(self):
        import os,subprocess,sys,time
        self.plan([{'stdin':'2\n3\n','expected_stdout':'5\n'},
                   {'stdin':'-1\n1\n','expected_stdout':'0\n'}],
                  'a=int(input()); b=int(input()); print(a+b)\n')
        env=os.environ.copy(); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
        def cli(*args,input=None):
            return subprocess.run([sys.executable,'-m','gbm',*args],cwd=self.root,env=env,
                                  input=input,text=True,capture_output=True,timeout=20)
        try:
            started=cli('watch'); self.assertEqual(started.returncode,0,started.stderr)
            for args in [('--use','test.py'),('--for','main.py'),
                         ('--use','test.py','--for','main.py','--cases','1')]:
                result=cli('experiment',*args,'--plan')
                self.assertEqual(result.returncode,1,result.stdout+result.stderr)
            result=cli('experiment','--use','test.py','--for','main.py','--plan')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('2 supplied cases',result.stdout)
            self.assertFalse((self.p.home/'experiments').exists())
            result=cli('experiment','--use','test.py','--for','main.py',input='y\n')
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('PASS=2',result.stdout)
            reports=list((self.p.home/'experiments').glob('*/report.json'))
            report=json.loads(reports[0].read_text())
            replay=cli('experiment','replay',report['id'],'--yes')
            self.assertEqual(replay.returncode,0,replay.stdout+replay.stderr)
            self.assertIn('2 supplied cases',replay.stdout)
            self.write('test.py','CASES=[{"stdin":"1\\n2\\n","expected_stdout":"wrong"}]\n')
            failed=cli('experiment','--use','test.py','--for','main.py','--yes')
            self.assertEqual(failed.returncode,1,failed.stdout+failed.stderr)
            self.assertIn('FAIL=1',failed.stdout)
            cli('stop')
            self.assertIn('Monitoring is inactive',cli('experiment','--use','test.py','--for','main.py').stderr)
        finally:
            cli('stop'); time.sleep(1)

    def test_script_can_import_sibling_module(self):
        (self.root/'tools').mkdir()
        (self.root/'tools'/'helper.py').write_text('value=7\n')
        (self.root/'tools'/'main.py').write_text('import helper\nprint(int(input())+helper.value)\n')
        self.write('test.py','CASES=[{"stdin":"2\\n","expected_stdout":"9\\n"}]\n')
        plan=custom.build_plan(self.p,str(self.root/'test.py'),str(self.root/'tools'/'main.py'),timeout=3)
        report,_=experiments.execute(self.p,plan,lambda:None)
        self.assertEqual(report['results'][0]['status'],'PASS')
