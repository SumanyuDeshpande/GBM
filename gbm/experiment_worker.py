"""Worker for bounded Python probes. Invoked with an isolated interpreter."""
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import time
import traceback


def main():
    request_path, result_path, gate_path = map(Path, sys.argv[1:4])
    while not gate_path.exists():
        time.sleep(.01)
    request = json.loads(request_path.read_text(encoding='utf-8'))
    root = Path(request['workspace'])
    os.chdir(root)
    sys.path[:0] = [str(root), str(root/'src')]
    if os.name != 'nt':
        import resource
        memory_limit = resource.RLIMIT_DATA if sys.platform == 'darwin' else resource.RLIMIT_AS
        resource.setrlimit(memory_limit, (256*1024*1024, 256*1024*1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024*1024, 1024*1024))
        resource.setrlimit(resource.RLIMIT_CPU, (int(request['timeout'])+1, int(request['timeout'])+2))
    phase = 'import'
    try:
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
        if phase == 'import':
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
