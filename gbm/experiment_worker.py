"""Worker for bounded Python probes. Invoked with an isolated interpreter."""
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
