"""Command line entry point."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from .core import Project, GBMError
from .watcher import active, run

def main(argv=None):
    parser = argparse.ArgumentParser(prog='gbm', description='Automatic local project checkpoints and recovery')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('watch', help='Start background monitoring')
    commands.add_parser('status', help='Show watcher heartbeat and checkpoint count')
    commands.add_parser('stop', help='Stop monitoring gracefully')
    undo = commands.add_parser('undo', help='Restore the previous project or file version')
    undo.add_argument('file', nargs='?')
    undo.add_argument('--minutes', type=float, help='Restore file from the latest checkpoint at or before N minutes ago')
    undo.add_argument('-y', '--yes', action='store_true', help='Confirm project restoration')
    redo = commands.add_parser('redo', help='Redo a file-specific undo')
    redo.add_argument('file')
    recover = commands.add_parser('recover', help='Roll back an interrupted restore')
    recover.add_argument('-y', '--yes', action='store_true')
    internal = commands.add_parser('_watch', help=argparse.SUPPRESS)
    internal.add_argument('root')
    internal.add_argument('token')
    args = parser.parse_args(argv)
    if args.command == 'undo' and args.minutes is not None and not args.file:
        parser.error('--minutes requires a file, for example: gbm undo main.py --minutes 5')
    try:
        if args.command == '_watch':
            run(Project(args.root), args.token)
            return 0
        project = Project.discover()
        if args.command != 'watch' and not (project.home / 'history.db').exists():
            if args.command == 'status':
                print('[GBM] Watcher: INACTIVE. No history initialized.')
                return 0
            raise GBMError('No GBM history exists. Run gbm watch first.')
        if args.command == 'watch':
            project.initialize()
            with project.lock():
                if active(project):
                    print('[GBM] Already watching this project.')
                    return 0
                project.capture()
                token = uuid.uuid4().hex
                project.set('watcher', {'token': token, 'running': True, 'heartbeat': 0})
                env = os.environ.copy()
                source = str(Path(__file__).resolve().parent.parent)
                env['PYTHONPATH'] = source + os.pathsep + env.get('PYTHONPATH', '')
                options = {'start_new_session': True} if os.name != 'nt' else {
                    'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                with (project.home / 'watcher.log').open('ab') as log:
                    subprocess.Popen([sys.executable, '-m', 'gbm', '_watch', str(project.root), token],
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, **options)
            for _ in range(50):
                if active(project):
                    print(f'[GBM] Watching {project.root}. Automatic checkpointing enabled.')
                    return 0
                time.sleep(.1)
            raise GBMError('Watcher did not start. See .gbm/watcher.log.')
        if args.command == 'status':
            with project.lock():
                with project.db() as db:
                    count, latest = db.execute('SELECT COUNT(*),MAX(created) FROM checkpoints').fetchone()
                print(f'Project: {project.root}\nWatcher: {"ACTIVE" if active(project) else "INACTIVE"}')
                print(f'Checkpoints: {count}\nLast checkpoint: {time.ctime(latest) if latest else "none"}')
                if project.pending():
                    print('ATTENTION: Interrupted restoration. Run gbm recover.')
            return 0
        if args.command == 'stop':
            with project.lock():
                status = project.get('watcher', {})
                status['running'] = False
                project.set('watcher', status)
                if not project.pending():
                    project.capture()
            print('[GBM] Monitoring stopped; current files checkpointed.')
            return 0
        if args.command == 'recover':
            if not args.yes and input('Roll back interrupted restoration? [y/N]: ').lower() != 'y':
                return 0
            with project.lock():
                project.recover()
            print('[GBM] Pre-restore project state recovered.')
            return 0
        rel = project.file_arg(args.file) if args.file else None
        with project.lock():
            before, target, action = project.preview(rel, args.command == 'redo', getattr(args, 'minutes', None))
        affected = sum(before.get(p) != target.get(p) for p in set(before) | set(target))
        if 'checkpoint' in action:
            print(f"[GBM] Target checkpoint: #{action['checkpoint']} ({time.ctime(action['created'])})")
        print(f'[GBM] Files affected: {affected}')
        if affected == 0:
            print('[GBM] File already matches the selected checkpoint. Nothing restored.')
            return 0
        if not rel and not args.yes and input('Restore project? [y/N]: ').lower() != 'y':
            return 0
        with project.lock():
            project.apply(before, target, action)
        print('[GBM] Restored successfully.')
        return 0
    except (GBMError, OSError, ValueError, KeyError) as error:
        print(f'[GBM] {error}', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n[GBM] Cancelled.', file=sys.stderr)
        return 130

if __name__ == '__main__':
    sys.exit(main())
