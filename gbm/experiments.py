"""Deterministic, reproducible Python edge-case experiments (no AI required)."""
import ast
from collections import Counter
import json
import io
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import tokenize
import uuid
import venv
from .core import GBMError

MAX_FILES = 500
MAX_BYTES = 20*1024*1024


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding='utf-8')


def samples(argument, default):
    annotation = ast.unparse(argument.annotation) if argument.annotation else ''
    base = annotation.split('[',1)[0]
    if base in ('int','float') or isinstance(default,(int,float)) and not isinstance(default,bool):
        return [1,0,-1,10**50,None,'',[],{}]
    if base == 'str' or isinstance(default,str):
        return ['sample','',' \t\n','\u0000','\u6587\U0001f680','x'*4096,None,0]
    if base in ('list','List','tuple','Tuple') or isinstance(default,(list,tuple)):
        return [[1,2],[],[None],[0]*256,['x',1,None],None,'',{}]
    if base in ('dict','Dict') or isinstance(default,dict):
        return [{'key':1},{},{'':None},{'nested':{'value':[]}},None,[],0,'']
    if base == 'bool' or isinstance(default,bool):
        return [True,False,None,0,1,'',[],{}]
    return [0,None,'',[],{},-1,'\u6587', [0]*256]


def discover(project, target=None):
    manifest = project.scan()
    if len(manifest)>MAX_FILES:
        raise GBMError(f'Experiment copy limit is {MAX_FILES} monitored files. Narrow your monitoring scope first.')
    total = sum((project.home/'objects'/digest).stat().st_size for digest in manifest.values())
    if total>MAX_BYTES:
        raise GBMError('Experiment copy limit is 20 MiB. Narrow your monitoring scope first.')
    selected_file, selected_function = None, None
    if target:
        file_part, separator, function = target.rpartition(':')
        if separator and function.isidentifier() and file_part.endswith('.py'):
            selected_file, selected_function = project.file_arg(file_part), function
        else:
            selected_file = project.file_arg(target)
        if selected_file not in manifest:
            raise GBMError('Selected Python file does not exist in the monitored scope.')
    functions, skipped = [], []
    for rel in sorted(manifest):
        if not rel.endswith('.py') or selected_file and rel != selected_file:
            continue
        parts = list(Path(rel).with_suffix('').parts)
        if parts[0] == 'src': parts = parts[1:]
        if parts[-1] == '__init__': parts = parts[:-1]
        if not parts or not all(part.isidentifier() for part in parts):
            skipped.append({'file':rel,'reason':'Not an importable Python module path.'}); continue
        try:
            data = project.object(manifest[rel])
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            tree = ast.parse(data.decode(encoding), filename=rel)
        except (SyntaxError,UnicodeError) as error:
            skipped.append({'file':rel,'reason':str(error)}); continue
        for node in tree.body:
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                continue
            if selected_function and node.name != selected_function: continue
            if not selected_function and (node.name.startswith('_') or node.name.startswith('test_')): continue
            args = node.args
            parameters = list(args.args)+list(args.kwonlyargs)
            if (isinstance(node,ast.AsyncFunctionDef) or node.decorator_list or args.vararg or args.kwarg
                    or args.posonlyargs or len(parameters)>4):
                skipped.append({'file':rel,'function':node.name,'reason':'Requires a specialized harness (async, decorators, variadic/positional-only, or >4 parameters).'}); continue
            defaults = [None]*(len(args.args)-len(args.defaults))+list(args.defaults)+list(args.kw_defaults)
            choices = {}
            for parameter, default_node in zip(parameters,defaults):
                try: default = ast.literal_eval(default_node) if default_node is not None else None
                except (ValueError,TypeError): default = None
                choices[parameter.arg] = samples(parameter,default)
            functions.append({'file':rel,'module':'.'.join(parts),'function':node.name,'choices':choices})
    return manifest, functions, skipped


def build_plan(project, target=None, cases=20, timeout=2.0, budget=30.0):
    if not 1 <= cases <= 100 or not math.isfinite(timeout) or not .1<=timeout<=30:
        raise GBMError('--cases must be 1..100 and --timeout must be 0.1..30 seconds.')
    if not math.isfinite(budget) or not 1<=budget<=300:
        raise GBMError('--budget must be 1..300 seconds.')
    if project.pending(): raise GBMError('Resolve the interrupted restoration with gbm recover first.')
    manifest, functions, skipped = discover(project,target)
    if not functions:
        raise GBMError('No supported top-level Python functions found. Select a simple function with file.py:function.')
    selected = functions[:5]
    queues = []
    for function in selected:
        base = {name:values[0] for name,values in function['choices'].items()}
        variants = [('baseline',base)]
        for name, values in function['choices'].items():
            for index,value in enumerate(values[1:],1):
                inputs=dict(base); inputs[name]=value
                variants.append((f'{name}:edge-{index}',inputs))
        queues.append([(function,label,inputs) for label,inputs in variants])
    probes=[]
    while any(queues) and len(probes)<cases:
        for queue in queues:
            if queue and len(probes)<cases:
                function,label,inputs=queue.pop(0)
                probes.append({'file':function['file'],'module':function['module'],'function':function['function'],
                               'label':label,'inputs':inputs})
    return {'format':1,'created':time.time(),'python':sys.version,'platform':sys.platform,
            'manifest':manifest,'timeout':timeout,'budget':budget,'probes':probes,'skipped':skipped,
            'discovered_functions':len(functions),'selected_functions':len(selected)}


class ProcessTree:
    """Kill descendants and enforce a per-process memory ceiling on Windows."""
    def __init__(self, process):
        self.process=process; self.handle=None
        if os.name != 'nt': return
        import ctypes
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),('flags',w.DWORD),
                      ('min_work',ctypes.c_size_t),('max_work',ctypes.c_size_t),('processes',w.DWORD),
                      ('affinity',ctypes.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(ctypes.Structure):
            _fields_=[(name,ctypes.c_ulonglong) for name in ['reads','writes','other','read_bytes','write_bytes','other_bytes']]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',ctypes.c_size_t),('job_memory',ctypes.c_size_t),
                      ('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,w.LPCWSTR]; kernel.CreateJobObjectW.restype=w.HANDLE
        kernel.SetInformationJobObject.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,w.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        kernel.CloseHandle.argtypes=[w.HANDLE]
        handle=kernel.CreateJobObjectW(None,None)
        if not handle: raise GBMError('Cannot create experiment process limits on this Windows system.')
        limits=Extended(); limits.basic.flags=0x2000|0x100|0x8
        limits.basic.processes=4; limits.process_memory=256*1024*1024
        if (not kernel.SetInformationJobObject(handle,9,ctypes.byref(limits),ctypes.sizeof(limits))
                or not kernel.AssignProcessToJobObject(handle,int(process._handle))):
            kernel.CloseHandle(handle)
            raise GBMError('Cannot apply Windows experiment limits. No project code was executed.')
        self.handle=handle; self.kernel=kernel

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle); self.handle=None
        elif os.name != 'nt':
            try: os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        if self.process.poll() is None: self.process.kill()
        self.process.wait(timeout=10)


def case_run(project, folder, interpreter, plan, probe, index, timeout, require_active):
    work=folder/'workspace'
    if work.exists():
        # Fixed, resolved destination beneath this experiment's owned directory.
        if work.is_symlink() or not work.resolve().is_relative_to(folder.resolve()):
            raise GBMError('Experiment workspace path was replaced; refusing cleanup.')
        shutil.rmtree(work)
    work.mkdir()
    for rel,digest in plan['manifest'].items():
        destination=work/rel
        if not destination.resolve().is_relative_to(work.resolve()): raise GBMError('Invalid experiment manifest path.')
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_bytes(project.object(digest))
    request=folder/'request.json'; output=folder/'result.json'; gate=folder/'gate'
    output.unlink(missing_ok=True); gate.unlink(missing_ok=True)
    save(request,dict(probe,workspace=str(work),timeout=timeout))
    env={key:os.environ[key] for key in ('SystemRoot','WINDIR') if key in os.environ}
    temp=work/'.temp'; temp.mkdir(exist_ok=True)
    env.update(TEMP=str(temp),TMP=str(temp),HOME=str(work),USERPROFILE=str(work),PATH=os.defpath)
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    diagnostics=folder/'worker-stderr.log'
    with diagnostics.open('wb') as errors:
        process=subprocess.Popen([str(interpreter),'-I',str(Path(__file__).with_name('experiment_worker.py')),
                                  str(request),str(output),str(gate)],cwd=work,env=env,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=errors,**options)
    tree=None; started=time.monotonic()
    try:
        tree=ProcessTree(process)
        gate.write_text('go')
        deadline=time.monotonic()+timeout
        while process.poll() is None:
            require_active()
            if diagnostics.stat().st_size>65536:
                return dict(probe,status='CRASH',message='Worker stderr exceeded 64 KiB.',duration=time.monotonic()-started)
            remaining=deadline-time.monotonic()
            if remaining<=0:
                return dict(probe,status='TIMEOUT',message=f'Exceeded {timeout:g} seconds.',duration=time.monotonic()-started)
            try: process.wait(timeout=min(.1,remaining))
            except subprocess.TimeoutExpired: pass
        if output.exists() and output.stat().st_size<=32768:
            result=json.loads(output.read_text(encoding='utf-8'))
        else:
            with diagnostics.open('rb') as errors:
                errors.seek(max(0, diagnostics.stat().st_size-8192))
                diagnostic=errors.read(8192).decode('utf-8',errors='replace')
            result={'status':'CRASH','message':f'Worker exited without a result (exit {process.returncode}).',
                    'stderr':diagnostic}
        return dict(probe,**result,duration=time.monotonic()-started)
    finally:
        if tree: tree.close()
        else:
            process.kill(); process.wait(timeout=10)


def load_plan(project, experiment_id):
    if not experiment_id or len(experiment_id)!=32 or any(c not in '0123456789abcdef' for c in experiment_id):
        raise GBMError('Invalid experiment ID.')
    path=project.home/'experiments'/experiment_id/'plan.json'
    if not path.is_file(): raise GBMError('Experiment not found in this project.')
    plan=json.loads(path.read_text(encoding='utf-8'))
    if plan.get('format')!=1: raise GBMError('Unsupported experiment format.')
    return plan


def execute(project, plan, require_active):
    require_active()
    if project.pending(): raise GBMError('Resolve the interrupted restoration with gbm recover first.')
    folder=project.home/'experiments'/uuid.uuid4().hex
    folder.mkdir(parents=True)
    save(folder/'plan.json',plan)
    report={'id':folder.name,'started':time.time(),'status':'RUNNING','results':[],
            'python':sys.version,'platform':sys.platform,
            'note':'Generated probes have no correctness oracle. Findings require review; this is not a security sandbox.'}
    save(folder/'report.json',report)
    try:
        environment=folder/'environment'
        venv.EnvBuilder(with_pip=False, symlinks=os.name != 'nt').create(environment)
        interpreter=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        deadline=time.monotonic()+plan['budget']
        for index,probe in enumerate(plan['probes']):
            require_active()
            remaining=deadline-time.monotonic()
            if remaining<=0:
                report['results'].append(dict(probe,status='NOT_RUN',message='Total execution budget exhausted.'))
                continue
            result=case_run(project,folder,interpreter,plan,probe,index,min(plan['timeout'],remaining),require_active)
            report['results'].append(result)
            save(folder/'report.json',report)
        report['status']='FINISHED'
    except (GBMError,OSError,KeyboardInterrupt) as error:
        report['status']='CANCELLED'; report['message']=str(error) or 'Interrupted by user.'
        raise
    finally:
        report['finished']=time.time()
        report['summary']=dict(Counter(row['status'] for row in report['results']))
        save(folder/'report.json',report)
        # Only remove generated runtime directories, preserving the reproducible manifest and report.
        for name in ('workspace','environment'):
            owned=folder/name
            if owned.exists() and not owned.is_symlink() and owned.resolve().is_relative_to(folder.resolve()):
                try: shutil.rmtree(owned)
                except OSError: pass
    return report,folder/'report.json'
