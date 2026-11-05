"""Constrained file tools and test execution for an agent workspace."""
import difflib
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from .core import GBMError
from .experiments import ProcessTree


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


TEXT = {'type': 'string'}
TOOLS = [
    schema('list_files', 'List eligible workspace files with relative paths.', {'path': TEXT}, []),
    schema('read_file', 'Read UTF-8 text; select up to 200 lines.',
           {'path': TEXT, 'start_line': {'type': 'integer'}, 'lines': {'type': 'integer'}}, ['path']),
    schema('write_file', 'Create or replace a UTF-8 file, max 64 KiB. Parent folders are created.',
           {'path': TEXT, 'content': TEXT}, ['path', 'content']),
    schema('delete_file', 'Delete one eligible workspace file.', {'path': TEXT}, ['path']),
    schema('create_directory', 'Create an eligible workspace directory.', {'path': TEXT}, ['path']),
    schema('delete_directory', 'Delete an EMPTY directory, never recursive.', {'path': TEXT}, ['path']),
    schema('run_tests', 'Run unittest or pytest; no shell or package installation. Fresh environment may lack dependencies.',
           {'runner': {'type': 'string', 'enum': ['unittest', 'pytest']}, 'path': TEXT}, []),
]
SCHEMAS = {tool['function']['name']: tool['function']['parameters'] for tool in TOOLS}
SECRET_PATTERNS = ['.env', '.env.*', '*.pem', '*.key', '*.pfx', 'pyvenv.cfg']


class AgentTools:
    def __init__(self, source, workspace, interpreter, check_active, approve, manual=False):
        self.source, self.workspace, self.interpreter = source, workspace, Path(interpreter)
        self.check_active, self.approve, self.manual = check_active, approve, manual
        self.audit, self.test_results, self.test_digest = [], [], None

    def path(self, value, allow_root=False):
        if not isinstance(value, str) or not value and not allow_root:
            raise GBMError('A relative workspace path is required.')
        if not value or value == '.':
            if allow_root:
                return self.workspace.root
            raise GBMError('Cannot modify the workspace root.')
        parts = Path(value).parts
        if any(fnmatch.fnmatchcase(part.lower(), pattern) for part in parts for pattern in SECRET_PATTERNS):
            raise GBMError('Secret/environment file is outside agent tool scope.')
        if not self.source.eligible(value):
            raise GBMError('Path is excluded by the original project.')
        return self.workspace.path(value)

    def inventory(self, base=None):
        base = base or self.workspace.root
        files, total = [], 0
        for directory, dirs, names in os.walk(base, followlinks=False):
            parent = Path(directory)
            keep = []
            for name in dirs:
                path = parent/name
                try:
                    self.path(path.relative_to(self.workspace.root).as_posix())
                except GBMError:
                    continue
                if not path.is_symlink() and not (hasattr(path, 'is_junction') and path.is_junction()):
                    keep.append(name)
            dirs[:] = keep
            for name in names:
                path = parent/name
                try:
                    path = self.path(path.relative_to(self.workspace.root).as_posix())
                except GBMError:
                    continue
                if not path.is_file():
                    continue
                total += path.stat().st_size
                if len(files) >= 20000 or total > 512*1024*1024:
                    raise GBMError('Agent workspace exceeds file/size limits.')
                files.append(path.relative_to(self.workspace.root).as_posix())
        return sorted(files)

    def digest(self):
        result = hashlib.sha256()
        for rel in self.inventory():
            result.update(rel.encode())
            result.update(hashlib.sha256(self.path(rel).read_bytes()).digest())
        return result.hexdigest()

    def validate_args(self, name, args):
        spec = SCHEMAS.get(name)
        if spec is None or not isinstance(args, dict):
            raise GBMError('Unknown tool or invalid arguments.')
        if set(args)-set(spec['properties']) or set(spec['required'])-set(args):
            raise GBMError('Unexpected/missing tool arguments.')
        for key, value in args.items():
            kind = spec['properties'][key]['type']
            if kind == 'string' and not isinstance(value, str) or kind == 'integer' and type(value) is not int:
                raise GBMError('Wrong tool argument type.')
        return args

    def invoke(self, name, args):
        self.check_active()
        args = self.validate_args(name, args)
        preview = ''
        if name == 'write_file':
            path = self.path(args['path'])
            content = args['content']
            if len(content.encode('utf-8')) > 65536:
                raise GBMError('File write exceeds 64 KiB.')
            old = path.read_text(encoding='utf-8') if path.is_file() and path.stat().st_size <= 65536 else ''
            preview = ''.join(difflib.unified_diff(old.splitlines(True), content.splitlines(True),
                                                fromfile=args['path'], tofile=args['path']))
        elif 'path' in args:
            self.path(args['path'], allow_root=name in ('list_files', 'run_tests'))
        summary = {key: value for key, value in args.items() if key != 'content'}
        if 'content' in args:
            summary['content_bytes'] = len(args['content'].encode())
        if self.manual and not self.approve(name, summary, preview):
            self.audit.append({'tool': name, 'arguments': summary, 'status': 'DENIED'})
            return {'status': 'DENIED', 'message': 'User declined this action. Do not retry it without a different user-approved approach.'}
        self.check_active()
        if name == 'list_files':
            base = self.path(args.get('path', ''), allow_root=True)
            if not base.is_dir():
                raise GBMError('Directory not found.')
            files = self.inventory(base)
            result = {'files': files[:500], 'total': len(files), 'truncated': len(files) > 500}
        elif name == 'read_file':
            path = self.path(args['path'])
            if not path.is_file() or path.stat().st_size > 262144:
                raise GBMError('Read requires a text file of at most 256 KiB.')
            start, lines = args.get('start_line', 1), args.get('lines', 200)
            if start < 1 or not 1 <= lines <= 200:
                raise GBMError('Read line range must start at 1 or later, with 1..200 lines.')
            contents = path.read_text(encoding='utf-8').splitlines(True)
            result = {'text': ''.join(contents[start-1:start-1+lines])[:10000], 'total_lines': len(contents)}
        elif name == 'write_file':
            path = self.path(args['path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            self.workspace.atomic(path, args['content'].encode('utf-8'))
            result = {'status': 'WRITTEN', 'path': args['path']}
        elif name == 'delete_file':
            self.path(args['path']).unlink()
            result = {'status': 'DELETED', 'path': args['path']}
        elif name == 'create_directory':
            self.path(args['path']).mkdir(parents=True, exist_ok=True)
            result = {'status': 'CREATED'}
        elif name == 'delete_directory':
            self.path(args['path']).rmdir()
            result = {'status': 'DELETED'}
        else:
            result = self.run_tests(args.get('runner', 'unittest'), args.get('path'))
        self.audit.append({'tool': name, 'arguments': summary, 'status': result.get('status', 'OK')})
        return result

    def run_tests(self, runner='unittest', path=None):
        if runner not in ('unittest', 'pytest'):
            raise GBMError('Only unittest and pytest runners are supported.')
        if path is None:
            path = 'tests' if (self.workspace.root/'tests').is_dir() else '.'
        selected = self.path(path, allow_root=True)
        if not selected.exists() or runner == 'unittest' and not selected.is_dir():
            raise GBMError('Test discovery path must exist (unittest requires a directory).')
        rel = selected.relative_to(self.workspace.root).as_posix()
        folder = self.workspace.home/'agent-tests'
        folder.mkdir(exist_ok=True)
        number = len(self.test_results)+1
        output, summary = folder/f'{number}.log', folder/f'{number}.json'
        temporary = folder/'temp'
        temporary.mkdir(exist_ok=True)
        env = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR') if key in os.environ}
        env.update(TEMP=str(temporary), TMP=str(temporary), TMPDIR=str(temporary), HOME=str(self.workspace.root),
                   USERPROFILE=str(self.workspace.root), PATH=str(self.interpreter.parent)+os.pathsep+os.defpath,
                   PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
        with output.open('wb') as log:
            process = subprocess.Popen([str(self.interpreter), '-I', str(Path(__file__).with_name('agent_test_worker.py')),
                                        str(self.workspace.root), runner, rel, str(summary)], cwd=self.workspace.root,
                                       env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        tree, started, status = None, time.monotonic(), None
        try:
            tree = ProcessTree(process)
            while process.poll() is None:
                self.check_active()
                if time.monotonic()-started > 30:
                    status = 'TIMEOUT'
                    break
                if output.stat().st_size > 1024*1024:
                    status = 'OUTPUT_LIMIT'
                    break
                try:
                    process.wait(timeout=.1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if tree:
                tree.close()
            else:
                process.kill()
                process.wait(timeout=10)
        if status:
            result = {'status': status, 'tests': None}
        elif summary.exists() and summary.stat().st_size < 16384:
            result = json.loads(summary.read_text(encoding='utf-8'))
        else:
            result = {'status': 'INCONCLUSIVE', 'tests': None, 'message': 'Test process exited without a result.'}
        with output.open('rb') as log:
            log.seek(max(0, output.stat().st_size-8000))
            result['output'] = log.read(8000).decode('utf-8', errors='replace')
        result.update(runner=runner, path=path, duration=time.monotonic()-started)
        self.test_results.append(result)
        self.test_digest = self.digest()
        return result
