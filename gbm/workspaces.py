"""Named project copies with fresh, independent Python environments."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import venv
from . import __version__
from .core import GBMError
from .experiments import ProcessTree

MAX_FILES = 20000
MAX_BYTES = 512*1024*1024


def valid_name(name):
    reserved={'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',name) or name.upper() in reserved:
        raise GBMError('Workspace name must be 1..64 letters, digits, hyphens, or underscores; start with a letter/digit and avoid reserved device names.')


def prepare(project, name, parent=None, require_active=None):
    valid_name(name)
    check_active = require_active or (lambda: None)
    check_active()
    if project.pending():
        raise GBMError('Resolve the interrupted restore with gbm recover before creating a workspace.')
    if parent is None:
        key=hashlib.sha256(str(project.root).encode()).hexdigest()[:10]
        parent=project.root.parent/'.gbm-workspaces'/(project.root.name+'-'+key)
    parent=Path(parent).expanduser().resolve()
    destination=parent/name
    if destination.resolve().is_relative_to(project.root) or project.root.is_relative_to(destination.resolve()):
        raise GBMError('Workspace destination must be outside the original project and cannot contain it.')
    if destination.exists() or destination.is_symlink():
        raise GBMError(f'Workspace already exists: {destination}. Choose another name or destination; nothing overwritten.')
    files, directories, skipped, size = {}, [], [], 0
    def walk_error(error):
        raise error
    for directory, dirs, names in os.walk(project.root,followlinks=False,onerror=walk_error):
        check_active()
        base=Path(directory)
        kept=[]
        for item in sorted(dirs):
            path=base/item; rel=path.relative_to(project.root).as_posix()
            linked=path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction())
            if linked or item=='.venv' or not project.eligible(rel) or (path/'pyvenv.cfg').is_file():
                skipped.append(rel+'/'); continue
            project.path(rel); kept.append(item); directories.append(rel)
        dirs[:]=kept
        for item in sorted(names):
            check_active()
            path=base/item; rel=path.relative_to(project.root).as_posix()
            if path.is_symlink() or rel=='.venv' or not project.eligible(rel):
                skipped.append(rel); continue
            path=project.path(rel)
            if not path.is_file():
                skipped.append(rel); continue
            before=path.stat()
            if len(files)>=MAX_FILES or size+before.st_size>MAX_BYTES:
                raise GBMError('Workspace copy limit: 20000 files / 512 MiB. Exclude generated or dependency files and retry.')
            data=path.read_bytes()
            after=path.stat()
            if before.st_mtime_ns!=after.st_mtime_ns or before.st_size!=after.st_size:
                raise GBMError(f'File changed while preparing workspace: {rel}. Save and retry.')
            size+=len(data)
            if size>MAX_BYTES:
                raise GBMError('Workspace copy exceeds 512 MiB.')
            digest=hashlib.sha256(data).hexdigest()
            obj=project.home/'objects'/digest
            if not obj.exists(): project.atomic(obj,data)
            files[rel]={'hash':digest,'mode':stat.S_IMODE(before.st_mode)}
    return {'format':1,'name':name,'source':str(project.root),'destination':str(destination),
            'created':time.time(),'gbm_version':__version__,'python':sys.version,
            'files':files,'directories':sorted(directories),'skipped':sorted(skipped),'bytes':size}


def bootstrap_pip(interpreter, internal, require_active):
    temp=internal/'setup-temp'; temp.mkdir()
    env=os.environ.copy()
    env.update(TEMP=str(temp),TMP=str(temp),TMPDIR=str(temp))
    options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
    log=internal/'setup.log'
    with log.open('wb') as output:
        process=subprocess.Popen([str(interpreter),'-I','-m','ensurepip','--upgrade','--default-pip'],
                                 stdin=subprocess.DEVNULL,stdout=output,stderr=output,env=env,**options)
    tree=None
    try:
        tree=ProcessTree(process)
        deadline=time.monotonic()+60
        while process.poll() is None:
            require_active()
            if time.monotonic()>deadline:
                raise GBMError(f'Workspace pip setup exceeded 60 seconds. See {log}.')
            try: process.wait(timeout=.1)
            except subprocess.TimeoutExpired: pass
        if process.returncode:
            raise GBMError(f'Workspace pip setup failed. See {log}.')
    finally:
        if tree: tree.close()
        else:
            process.kill(); process.wait(timeout=10)


def create(project, plan, require_active):
    require_active()
    destination=Path(plan['destination'])
    if destination.resolve().is_relative_to(project.root) or project.root.is_relative_to(destination.resolve()):
        raise GBMError('Workspace destination must be outside the original project.')
    # mkdir without exist_ok prevents overwriting another process's named workspace.
    destination.parent.mkdir(parents=True,exist_ok=True)
    try: destination.mkdir()
    except FileExistsError:
        raise GBMError(f'Workspace already exists: {destination}. Nothing overwritten.')
    internal=destination/'.gbm'; internal.mkdir()
    metadata=internal/'workspace.json'
    record=dict(plan,status='CREATING')
    def persist():
        project.atomic(metadata,json.dumps(record,ensure_ascii=True,indent=2).encode())
    try:
        persist()
        for rel in sorted(plan['directories'],key=lambda value:len(Path(value).parts)):
            require_active()
            path=destination/rel
            if not path.resolve().is_relative_to(destination.resolve()): raise GBMError('Invalid workspace directory path.')
            path.mkdir(parents=True,exist_ok=True)
        for rel,entry in plan['files'].items():
            require_active()
            path=destination/rel
            if not path.resolve().is_relative_to(destination.resolve()): raise GBMError('Invalid workspace file path.')
            path.parent.mkdir(parents=True,exist_ok=True)
            data=project.object(entry['hash'])
            with path.open('xb') as stream: stream.write(data)
            if hashlib.sha256(path.read_bytes()).hexdigest()!=entry['hash']:
                raise GBMError(f'Workspace verification failed: {rel}')
            path.chmod(entry['mode'])
        require_active()
        environment=destination/'.venv'
        venv.EnvBuilder(with_pip=False,symlinks=os.name!='nt').create(environment)
        interpreter=environment/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        require_active()
        bootstrap_pip(interpreter,internal,require_active)
        require_active()
        record.update(status='READY',completed=time.time(),interpreter=str(interpreter))
        persist()
    except (GBMError,OSError,KeyboardInterrupt) as error:
        record.update(status='INCOMPLETE',error=str(error) or 'Interrupted by user.')
        try: persist()
        except OSError: pass
        if isinstance(error,KeyboardInterrupt): raise
        raise GBMError(f'{error}\n[GBM] Incomplete workspace retained for inspection: {destination}. Original project was not restored or overwritten.')
    return record
