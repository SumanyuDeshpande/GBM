"""Read literal custom cases without importing or executing the case file."""
import ast
import builtins
import io
import math
from pathlib import Path
import sys
import time
import tokenize
from .core import GBMError
from .experiments import snapshot_manifest


def source(project, manifest, rel):
    data = project.object(manifest[rel])
    encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
    return ast.parse(data.decode(encoding), filename=rel)


def json_value(value, depth=0):
    if depth>30:
        return False
    if value is None or type(value) in (str,int,bool):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(json_value(item,depth+1) for item in value)
    if type(value) is dict:
        return all(type(key) is str and json_value(item,depth+1) for key,item in value.items())
    return False


def build_plan(project, use, target, timeout=2.0, budget=30.0):
    if not math.isfinite(timeout) or not .1<=timeout<=30 or not math.isfinite(budget) or not 1<=budget<=300:
        raise GBMError('--timeout must be 0.1..30 and --budget must be 1..300 seconds.')
    if project.pending():
        raise GBMError('Resolve the interrupted restoration with gbm recover first.')
    manifest=snapshot_manifest(project)
    cases_file, target_file=project.file_arg(use),project.file_arg(target)
    if cases_file==target_file:
        raise GBMError('--use and --for must select different files.')
    for rel in (cases_file,target_file):
        if not rel.endswith('.py') or rel not in manifest:
            raise GBMError('Both --use and --for must name existing monitored Python files.')
    if len(project.object(manifest[cases_file]))>262144:
        raise GBMError('Custom case file must be at most 256 KiB.')
    try:
        tree=source(project,manifest,cases_file)
        assignments=[]
        for node in tree.body:
            if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
                continue
            if not (isinstance(node,ast.Assign) and len(node.targets)==1 and
                    isinstance(node.targets[0],ast.Name) and node.targets[0].id=='CASES'):
                raise GBMError('Case file must contain only CASES = [...] literal data (and optional docstrings). It is never executed.')
            assignments.append(node.value)
        if len(assignments)!=1:
            raise GBMError('Case file must define CASES exactly once.')
        cases=ast.literal_eval(assignments[0])
        target_tree=source(project,manifest,target_file)
    except (SyntaxError,UnicodeError,ValueError,TypeError,RecursionError) as error:
        raise GBMError(f'Invalid literal case file or Python target: {error}')
    if type(cases) is not list or not 1<=len(cases)<=100 or not json_value(cases):
        raise GBMError('CASES must be a list of 1..100 dictionaries containing JSON-compatible literal values (no tuples, sets, expressions, or non-finite numbers).')
    functions={node.name for node in target_tree.body if isinstance(node,ast.FunctionDef)}
    parts=list(Path(target_file).with_suffix('').parts)
    if parts[0]=='src': parts=parts[1:]
    if parts[-1]=='__init__': parts=parts[:-1]
    module='.'.join(parts)
    probes=[]
    for index,case in enumerate(cases,1):
        prefix=f'Case {index}: '
        if type(case) is not dict:
            raise GBMError(prefix+'each case must be a dictionary.')
        label=case.get('name',f'case-{index}')
        if not isinstance(label,str) or not label or len(label)>128:
            raise GBMError(prefix+'name must be a nonempty string of at most 128 characters.')
        script='stdin' in case
        allowed=({'name','stdin','expected_stdout','expected_exit'} if script else
                 {'name','function','args','inputs','expected','expected_exception'})
        unknown=set(case)-allowed
        if unknown:
            raise GBMError(prefix+'unknown or incompatible keys: '+', '.join(sorted(unknown)))
        probe={'file':target_file,'module':module,'function':'<script>' if script else case.get('function'),
               'label':label,'origin':'custom','mode':'script' if script else 'function','inputs':case.get('inputs',{})}
        if script:
            if not isinstance(case['stdin'],str):
                raise GBMError(prefix+'stdin must be a string, with newline characters between input lines.')
            if 'expected_stdout' in case and not isinstance(case['expected_stdout'],str):
                raise GBMError(prefix+'expected_stdout must be a string.')
            if 'expected_exit' in case and (type(case['expected_exit']) is not int or not 0<=case['expected_exit']<=255):
                raise GBMError(prefix+'expected_exit must be an integer from 0 to 255.')
            probe['stdin']=case['stdin']
        else:
            function=case.get('function')
            if not isinstance(function,str) or function not in functions:
                raise GBMError(prefix+'function must name a top-level synchronous function defined in the --for file.')
            if not parts or not all(part.isidentifier() for part in parts):
                raise GBMError(prefix+'function target needs an importable Python module path.')
            if type(case.get('inputs',{})) is not dict or type(case.get('args',[])) is not list:
                raise GBMError(prefix+'inputs must be a dictionary and args must be a list.')
            if any(not key.isidentifier() for key in case.get('inputs',{})):
                raise GBMError(prefix+'input keys must be Python parameter names.')
            if 'expected' in case and 'expected_exception' in case:
                raise GBMError(prefix+'use expected or expected_exception, not both.')
            if 'expected_exception' in case:
                name=case['expected_exception']
                exception=getattr(builtins,name,None) if isinstance(name,str) else None
                if not isinstance(exception,type) or not issubclass(exception,Exception):
                    raise GBMError(prefix+'expected_exception must name a built-in Exception type, such as ValueError.')
            probe['args']=case.get('args',[])
        for key in ('expected','expected_exception','expected_stdout','expected_exit'):
            if key in case: probe[key]=case[key]
        probes.append(probe)
    return {'format':1,'mode':'custom','cases_file':cases_file,'target_file':target_file,
            'created':time.time(),'python':sys.version,'platform':sys.platform,'manifest':manifest,
            'timeout':timeout,'budget':budget,'probes':probes,'skipped':[],
            'discovered_functions':len(functions),'selected_functions':len({p['function'] for p in probes if p['mode']=='function'})}
