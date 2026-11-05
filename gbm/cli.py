"""Command line entry point."""
import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from . import __version__
from .core import Project, GBMError
from .watcher import active, run


def require_active(project):
    if not (project.home / 'history.db').exists() or not active(project):
        raise GBMError("Monitoring is inactive. Run 'gbm watch' in this project before using this command.")


@contextmanager
def command_lock(project, command):
    with project.lock():
        if command not in ('watch', 'status'):
            require_active(project)
        yield


def main(argv=None):
    parser = argparse.ArgumentParser(prog='gbm', description='Automatic local project checkpoints and recovery')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('watch', help='Start background monitoring')
    commands.add_parser('status', help='Show watcher heartbeat and checkpoint count')
    commands.add_parser('stop', help='Stop monitoring gracefully')
    commands.add_parser('setup', help='Configure Groq credentials and default model once for all projects')
    browse = commands.add_parser('history', help='Browse recorded project or file checkpoints')
    browse.add_argument('file', nargs='?')
    browse.add_argument('--limit', type=int, default=20, help='Maximum entries to show (default: 20)')
    browse.add_argument('--all', action='store_true', help='Include safety snapshots as well as automatic checkpoints')
    undo = commands.add_parser('undo', help='Restore the previous project or file version')
    undo.add_argument('file', nargs='?')
    undo.add_argument('--minutes', type=float, help='Restore file from the latest checkpoint at or before N minutes ago')
    undo.add_argument('-y', '--yes', action='store_true', help='Confirm project restoration')
    redo = commands.add_parser('redo', help='Redo the last project or file-specific undo')
    redo.add_argument('file', nargs='?')
    redo.add_argument('-y', '--yes', action='store_true', help='Confirm project restoration')
    recover = commands.add_parser('recover', help='Roll back an interrupted restore')
    recover.add_argument('-y', '--yes', action='store_true')
    session = commands.add_parser('session', help='Start, end, inspect, or recover an experiment session')
    session.add_argument('action', choices=['start', 'end', 'status', 'recover'])
    experiment = commands.add_parser('experiment', help='Review and run bounded Python edge-case probes')
    experiment.add_argument('target', nargs='?', help='Optional file.py[:function], or replay')
    experiment.add_argument('experiment_id', nargs='?', help='Experiment ID when using replay')
    experiment.add_argument('--use', dest='cases_file', help='Read only the literal CASES supplied in this Python file')
    experiment.add_argument('--for', dest='target_file', help='Python file to test with --use')
    experiment.add_argument('--cases', type=int, default=None, help='Maximum generated probes, 1..100')
    experiment.add_argument('--timeout', type=float, default=2.0, help='Per-case timeout in seconds, 0.1..30')
    experiment.add_argument('--budget', type=float, default=30.0, help='Total execution budget in seconds, 1..300 (excludes environment setup)')
    experiment.add_argument('--plan', action='store_true', help='Inspect and display probes without executing project code')
    experiment.add_argument('-y', '--yes', action='store_true', help='Approve executing the displayed probes without prompting')
    workspace = commands.add_parser('try', help='Create a named project copy with its own fresh Python environment')
    workspace.add_argument('name', help='Workspace name, such as new-auth')
    workspace.add_argument('--path', help='Parent directory for the named workspace (outside the original project)')
    agent = commands.add_parser('agent', help='Groq coding agent with workspace edits, tests, and a final merge decision')
    modes = agent.add_mutually_exclusive_group(required=True)
    modes.add_argument('--manual', action='store_true', help='Approve each tool action')
    modes.add_argument('--auto', action='store_true', help='Perform bounded tool actions automatically')
    agent.add_argument('--test', action='store_true', required=True, help='Require final test execution or explicitly report denied/missing tests')
    agent.add_argument('task', nargs='?', help='Task to perform; prompted if omitted')
    agent.add_argument('--model', help='Override GROQ_MODEL, saved setup model, or built-in fallback')
    agent.add_argument('--max-steps', type=int, default=10)
    agent.add_argument('--max-seconds', type=int, default=300)
    agent.add_argument('--test-runner', choices=['unittest','pytest'], default='unittest')
    internal = commands.add_parser('_watch', help=argparse.SUPPRESS)
    internal.add_argument('root')
    internal.add_argument('token')
    args = parser.parse_args(argv)
    if args.command == 'undo' and args.minutes is not None and not args.file:
        parser.error('--minutes requires a file, for example: gbm undo main.py --minutes 5')
    try:
        if args.command == 'setup':
            from . import setup
            setup.run()
            return 0
        if args.command == '_watch':
            run(Project(args.root), args.token)
            return 0
        project = Project.discover()
        if args.command not in ('watch', 'status'):
            require_active(project)
        if args.command != 'watch' and not (project.home / 'history.db').exists():
            if args.command == 'status':
                print('[GBM] Watcher: INACTIVE. No history initialized.')
                return 0
            raise GBMError('No GBM history exists. Run gbm watch first.')
        if args.command == 'agent':
            import json
            from . import agent as coding_agent
            from .groq_client import GroqClient
            from .setup import resolve
            key, model = resolve(args.model)
            client = GroqClient(key, model)
            task = args.task or input('What should the agent do? ').strip()
            def approve(name, arguments, preview):
                print(f'[GBM] Proposed action: {name} '+json.dumps(arguments, ensure_ascii=True))
                if preview:
                    print(preview[:8000])
                    if len(preview)>8000:
                        folder=project.home/'agents'/'proposals'; folder.mkdir(parents=True, exist_ok=True)
                        patch=folder/(uuid.uuid4().hex+'.diff')
                        patch.write_text(preview, encoding='utf-8')
                        print(f'[GBM] Full proposed diff: {patch}')
                return input('Approve this action? [y/N]: ').strip().lower()=='y'
            def confirm():
                return input('Merge these changes into your original project? [y/N]: ').strip().lower()=='y'
            coding_agent.run(project, task, client, args.manual, lambda: require_active(project), approve, confirm,
                             max_steps=args.max_steps, max_seconds=args.max_seconds, runner=args.test_runner)
            return 0
        if args.command == 'try':
            from . import workspaces
            with command_lock(project, args.command):
                pass
            # Preparation stores immutable objects but does not mutate history metadata.
            # Keep the watcher free to heartbeat while large trees are read.
            plan = workspaces.prepare(project, args.name, args.path, lambda: require_active(project))
            print(f"[GBM] Creating workspace: {plan['destination']}")
            print(f"[GBM] Copying {len(plan['files'])} files and {len(plan['directories'])} folders; skipped {len(plan['skipped'])} excluded/environment/link entries.")
            result = workspaces.create(project, plan, lambda: require_active(project))
            destination = Path(result['destination'])
            print(f"[GBM] Workspace ready: {destination}")
            print(f"[GBM] Fresh environment: {destination / '.venv'}")
            print('[GBM] Project dependencies are not installed automatically. Open this folder and activate its environment.')
            activate = destination / '.venv' / ('Scripts/activate.bat' if os.name == 'nt' else 'bin/activate')
            print(f'[GBM] Activate (cmd): "{activate}"' if os.name == 'nt' else f'[GBM] Activate: source "{activate}"')
            print(f"[GBM] Interpreter: {result['interpreter']}")
            print('[GBM] Run gbm watch inside the new workspace to enable GBM commands there.')
            print('[GBM] Review and copy selected code/dependency-file changes back manually; never copy .venv back. This is not a security sandbox.')
            return 0
        if args.command == 'experiment':
            from . import experiments
            if bool(args.cases_file) != bool(args.target_file):
                raise GBMError('--use and --for must be provided together.')
            if args.cases_file and (args.target or args.experiment_id or args.cases is not None):
                raise GBMError('Custom cases cannot be combined with a positional target, replay, or --cases; all supplied cases are used.')
            if args.experiment_id and args.target != 'replay':
                raise GBMError('Use gbm experiment replay <experiment-id>.')
            with command_lock(project, args.command):
                if args.cases_file:
                    from .custom_experiments import build_plan
                    plan = build_plan(project, args.cases_file, args.target_file, args.timeout, args.budget)
                elif args.target == 'replay':
                    plan = experiments.load_plan(project, args.experiment_id)
                else:
                    plan = experiments.build_plan(project, args.target, 20 if args.cases is None else args.cases, args.timeout, args.budget)
            custom = plan.get('mode') == 'custom'
            if custom:
                print(f"[GBM] Custom experiment: {len(plan['probes'])} supplied cases from {plan['cases_file']} against {plan['target_file']}. No additional inputs generated.")
            else:
                print(f"[GBM] Python experiment: {len(plan['probes'])} probes, {plan['selected_functions']} selected functions, {len(plan['skipped'])} skipped entries.")
            memory = 'no enforced memory cap on macOS' if sys.platform == 'darwin' else '256 MiB/process memory cap'
            print(f"[GBM] Limits: {plan['timeout']:g}s/case, {plan['budget']:g}s execution budget, {memory}; isolated stdlib-only environment.")
            for index, probe in enumerate(plan['probes'], 1):
                supplied = probe.get('stdin', {'args':probe['args'],'inputs':probe['inputs']} if 'args' in probe else probe['inputs'])
                values = ascii(supplied)
                if len(values) > 160: values = values[:157] + '...'
                print(f"  {index}. {probe['file']}:{probe['function']} [{probe['label']}] {values}")
                if custom:
                    expectations = {key:probe[key] for key in ('expected','expected_exception','expected_stdout','expected_exit') if key in probe}
                    print('     Expectations: ' + (ascii(expectations)[:240] if expectations else 'none; output is observed without a correctness assertion'))
            for skipped in plan['skipped'][:5]:
                print(f"[GBM] Skipped {skipped['file']}: {skipped['reason']}")
            print('[GBM] Supplied expectations determine PASS/FAIL; cases without expectations are observations.' if custom else '[GBM] These are generated probes, not verified correctness tests. Imports execute project code.')
            print('[GBM] Runs use a disposable copy, not a security sandbox: absolute paths, network, or external services remain accessible.')
            if args.plan:
                print('[GBM] Plan only. No project code executed.')
                return 0
            if not args.yes and input('Run these probes in a disposable copy? [y/N]: ').strip().lower() != 'y':
                print('[GBM] Cancelled. No project code executed.')
                return 0
            with command_lock(project, args.command):
                if args.target != 'replay' and project.scan() != plan['manifest']:
                    raise GBMError('Project changed after experiment preview. Run the command again.')
            report, report_path = experiments.execute(project, plan, lambda: require_active(project))
            print(f"[GBM] Experiment ID: {report['id']}")
            print('[GBM] Results: ' + ', '.join(f'{key}={value}' for key, value in sorted(report['summary'].items())))
            for result in report['results']:
                if custom or result['status'] in ('FINDING', 'TIMEOUT', 'CRASH', 'INCONCLUSIVE'):
                    message = result.get('message', '')[:200]
                    print(f"[GBM] {result['status']}: {result['file']}:{result['function']} [{result['label']}] {message}")
            print(f'[GBM] Report: {report_path}')
            print(f"[GBM] Replay: gbm experiment replay {report['id']}")
            return 1 if custom and any(row['status'] not in ('PASS','COMPLETED') for row in report['results']) else 0
        if args.command == 'history':
            from .history import history
            rel = project.file_arg(args.file) if args.file else None
            with command_lock(project, args.command):
                entries = history(project, rel, args.limit, args.all)
            print(f"[GBM] History: {rel or project.root}")
            if not entries:
                print('[GBM] No recorded history for this file.' if rel else '[GBM] No checkpoints recorded.')
                return 0
            print('CHECKPOINT  LOCAL TIME                KIND        CHANGE             CONTENT / SIZE')
            for entry in entries:
                marker = '*' if entry['head'] else ' '
                stamp = time.strftime('%Y-%m-%d %H:%M:%S %z', time.localtime(entry['created']))
                print(f"{marker}#{entry['id']:<9} {stamp:<25} {entry['kind']:<11} {entry['change']:<18} {entry['detail']}")
            print('[GBM] Newest first. * = project undo position, not necessarily the current file contents.')
            return 0
        if args.command == 'session':
            from . import sessions
            if args.action == 'start':
                with command_lock(project, args.command):
                    if active(project) and project.get('watcher', {}).get('version') != __version__:
                        raise GBMError('Restart the older watcher with gbm stop and gbm watch before starting a session.')
                    sessions.start(project)
                print('[GBM] Session started. Monitored files and folders recorded.')
                return 0
            if args.action == 'status':
                with command_lock(project, args.command):
                    state = project.get('session')
                print(f"[GBM] Session: {'ACTIVE since ' + time.ctime(state['started']) if state else 'INACTIVE'}")
                return 0
            with command_lock(project, args.command):
                plan = sessions.preview(project, recover=args.action == 'recover')
            before, target = plan['before'], plan['target']
            affected = sum(before['files'].get(p) != target['files'].get(p)
                           for p in set(before['files']) | set(target['files']))
            folders = len(set(before['directories']) ^ set(target['directories']))
            print(f'[GBM] Files changed: {affected}; folders changed: {folders}')
            if args.action == 'recover':
                answer = input('Restore the last discarded experiment? [y/N]: ').strip().lower()
                if answer != 'y':
                    print('[GBM] Cancelled.')
                    return 0
                keep = False
            else:
                while True:
                    answer = input('Would you like to maintain the changes you made in the session? [y/n]: ').strip().lower()
                    if answer in ('y', 'n'):
                        break
                    print('[GBM] Enter y to keep or n to discard. Ctrl+C cancels.')
                keep = answer == 'y'
            with command_lock(project, args.command):
                sessions.finish(project, plan, keep)
            print('[GBM] Changes kept. Session ended.' if keep else
                  '[GBM] Discarded experiment recovered.' if args.action == 'recover' else
                  '[GBM] Starting files and folders restored. Session ended.')
            return 0
        if args.command == 'watch':
            project.initialize()
            with command_lock(project, args.command):
                if active(project):
                    print('[GBM] Already watching this project.')
                    return 0
                if not project.pending():
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
                    if project.pending():
                        print('[GBM] Interrupted restore detected. Checkpointing is paused; run gbm recover.')
                    return 0
                time.sleep(.1)
            raise GBMError('Watcher did not start. See .gbm/watcher.log.')
        if args.command == 'status':
            with command_lock(project, args.command):
                with project.db() as db:
                    count, latest = db.execute('SELECT COUNT(*),MAX(created) FROM checkpoints').fetchone()
                print(f'Project: {project.root}\nWatcher: {"ACTIVE" if active(project) else "INACTIVE"}')
                print(f'Checkpoints: {count}\nLast checkpoint: {time.ctime(latest) if latest else "none"}')
                if project.pending():
                    print('ATTENTION: Interrupted restoration. Run gbm recover.')
            return 0
        if args.command == 'stop':
            with command_lock(project, args.command):
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
            with command_lock(project, args.command):
                project.recover()
            print('[GBM] Pre-restore state recovered. Any interrupted session remains active.')
            return 0
        rel = project.file_arg(args.file) if args.file else None
        with command_lock(project, args.command):
            before, target, action = project.preview(rel, args.command == 'redo', getattr(args, 'minutes', None))
        affected = sum(before.get(p) != target.get(p) for p in set(before) | set(target))
        if 'checkpoint' in action:
            print(f"[GBM] Target checkpoint: #{action['checkpoint']} ({time.ctime(action['created'])})")
        print(f'[GBM] Files affected: {affected}')
        if affected == 0:
            print('[GBM] Current files already match the target. Nothing restored.')
            return 0
        if not rel and not args.yes and input('Restore project? [y/N]: ').lower() != 'y':
            return 0
        with command_lock(project, args.command):
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
