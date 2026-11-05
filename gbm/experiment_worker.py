"""Worker for bounded Python probes. Invoked with an isolated interpreter."""
import builtins
import contextlib
import io
import runpy
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import time
import traceback


def configure_limits(timeout, platform=None, resource_module=None):
    platform = platform or sys.platform
    if os.name == 'nt' and resource_module is None:
        return  # Parent applies Windows Job limits before opening the gate.
    if resource_module is None:
        import resource as resource_module
    resource = resource_module
    def limit(kind, soft, hard):
        _, inherited_hard = resource.getrlimit(kind)
        if inherited_hard != resource.RLIM_INFINITY:
            hard = min(hard, inherited_hard)
        resource.setrlimit(kind, (min(soft, hard), hard))
    # Darwin's data-segment limit is not a portable cap on Python's total memory.
    # Do not lower it during interpreter startup; disclose the lack of a memory cap.
    if platform != 'darwin':
        limit(resource.RLIMIT_AS, 256*1024*1024, 256*1024*1024)
    limit(resource.RLIMIT_FSIZE, 1024*1024, 1024*1024)
    limit(resource.RLIMIT_CPU, int(timeout)+1, int(timeout)+2)


class OutputLimitError(RuntimeError):
    pass


class LimitedOutput(io.StringIO):
    def write(self, value):
        if self.tell()+len(value)>16384:
            raise OutputLimitError('Captured output exceeded 16384 characters.')
        return super().write(value)


def custom_case(request, root):
    stdout, stderr=LimitedOutput(),LimitedOutput()
    previous_stdin,previous_argv=sys.stdin,sys.argv
    checks=any(key in request for key in ('expected','expected_exception','expected_stdout','expected_exit'))
    result={}
    try:
        sys.stdin=io.StringIO(request.get('stdin',''))
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            if request['mode']=='script':
                script=root/request['file']
                sys.argv=[str(script)]
                sys.path.insert(0,str(script.parent))
                exit_code=0
                try:
                    runpy.run_path(str(root/request['file']),run_name='__main__')
                except SystemExit as error:
                    if error.code is None: exit_code=0
                    elif isinstance(error.code,int): exit_code=error.code % 256
                    else:
                        exit_code=1; stderr.write(str(error.code)+'\n')
                except ModuleNotFoundError as error:
                    return {'status':'INCONCLUSIVE','phase':'import','exception':type(error).__name__,
                            'message':str(error)[:2000],'stdout':stdout.getvalue()[:2000],'stderr':stderr.getvalue()[:2000]}
                except Exception as error:
                    exit_code=1
                    stderr.write(traceback.format_exc()[-8000:])
                    result['exception']=type(error).__name__
                passed=(exit_code==request.get('expected_exit',0) and
                        ('expected_stdout' not in request or stdout.getvalue()==request['expected_stdout']))
                result.update(status=('PASS' if passed else 'FAIL') if checks else
                              ('COMPLETED' if exit_code==0 else 'FINDING'),exit_code=exit_code,
                              message=('Supplied expectations matched.' if passed else 'Output or exit status did not match.')
                                      if checks else 'Script executed; output correctness is not verified.')
            else:
                try:
                    module=importlib.import_module(request['module'])
                    function=getattr(module,request['function'])
                except Exception as error:
                    return {'status':'INCONCLUSIVE','phase':'import','exception':type(error).__name__,
                            'message':str(error)[:2000],'stdout':stdout.getvalue()[:2000],'stderr':stderr.getvalue()[:2000]}
                try:
                    value=function(*request.get('args',[]),**request['inputs'])
                except BaseException as error:
                    expected=request.get('expected_exception')
                    passed=bool(expected and isinstance(error,getattr(builtins,expected)))
                    result={'status':('PASS' if passed else 'FAIL') if checks else 'FINDING',
                            'exception':type(error).__name__,'message':str(error)[:2000],
                            'traceback':traceback.format_exc()[-8000:]}
                else:
                    if inspect.isawaitable(value) or inspect.isgenerator(value):
                        result={'status':'INCONCLUSIVE','message':'Async/generator results require a specialized harness.'}
                    else:
                        passed='expected_exception' not in request and ('expected' not in request or bool(value==request['expected']))
                        result={'status':('PASS' if passed else 'FAIL') if checks else 'COMPLETED',
                                'returned':repr(value)[:2000],
                                'message':('Supplied expectation matched.' if passed else 'Return value or exception did not match.')
                                          if checks else 'Returned; correctness is not verified.'}
    except OutputLimitError as error:
        result={'status':'FAIL' if checks else 'FINDING','exception':type(error).__name__,'message':str(error)}
    except Exception as error:
        result={'status':'INCONCLUSIVE','phase':'harness','exception':type(error).__name__,
                'message':str(error)[:2000],'traceback':traceback.format_exc()[-8000:]}
    finally:
        sys.stdin,sys.argv=previous_stdin,previous_argv
    result['stdout']=stdout.getvalue()[:2000]
    result['stderr']=stderr.getvalue()[:2000]
    result['output_truncated']=len(stdout.getvalue())>2000 or len(stderr.getvalue())>2000
    return result


def main():
    request_path, result_path, gate_path = map(Path, sys.argv[1:4])
    while not gate_path.exists():
        time.sleep(.01)
    request = json.loads(request_path.read_text(encoding='utf-8'))
    root = Path(request['workspace'])
    phase = 'setup'
    try:
        os.chdir(root)
        sys.path[:0] = [str(root), str(root/'src')]
        configure_limits(request['timeout'])
        if request.get('origin') == 'custom':
            result = custom_case(request, root)
            result_path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')
            return
        phase = 'import'
        module = importlib.import_module(request['module'])
        function = getattr(module, request['function'])
        phase = 'call'
        value = function(**request['inputs'])
        if inspect.isawaitable(value) or inspect.isgenerator(value):
            result = {'status': 'INCONCLUSIVE', 'message': 'Async/generator result requires a specialized test harness.'}
        else:
            result = {'status': 'COMPLETED', 'message': 'Returned without exception; correctness is not verified.',
                      'returned': repr(value)[:2000]}
    except BaseException as error:
        if phase in ('setup', 'import'):
            status = 'INCONCLUSIVE'
        elif isinstance(error, (ValueError, TypeError)):
            status = 'REJECTED_INPUT'
        else:
            status = 'FINDING'
        result = {'status': status, 'phase': phase, 'exception': type(error).__name__,
                  'message': str(error)[:2000], 'traceback': traceback.format_exc()[-8000:]}
    result_path.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
