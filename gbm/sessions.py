"""Persistent experiment sessions, including monitored directory existence."""
import json
import os
import time
import uuid
from pathlib import Path
from .core import GBMError


def snapshot(project):
    files = project.scan()
    directories = []
    def onerror(error):
        raise error
    for directory, dirs, _ in os.walk(project.root, followlinks=False, onerror=onerror):
        parent = Path(directory)
        dirs[:] = [name for name in dirs if project.eligible((parent/name).relative_to(project.root))
                   and not (parent/name).is_symlink()
                   and not (hasattr(parent/name, 'is_junction') and (parent/name).is_junction())]
        for name in dirs:
            rel = (parent/name).relative_to(project.root).as_posix()
            project.path(rel)
            directories.append(rel)
    return {'files': files, 'directories': sorted(directories)}


def start(project):
    if project.pending():
        raise GBMError('Interrupted restore detected. Run gbm recover.')
    if project.get('session'):
        raise GBMError('A session is already active. End it before starting another.')
    state = snapshot(project)
    project.capture(state['files'])
    session = {'id': uuid.uuid4().hex, 'started': time.time(), 'state': state,
               'exclude': project.config()['exclude']}
    project.set('session', session)
    return session


def preview(project, recover=False):
    if project.pending():
        raise GBMError('Interrupted restore detected. Run gbm recover.')
    session = project.get('session')
    if recover:
        if session:
            raise GBMError('End the active session before recovering a discarded experiment.')
        backup = project.get('session_backup')
        if not backup:
            raise GBMError('No discarded session is available to recover.')
        target, exclusions = backup['state'], backup['exclude']
    else:
        if not session:
            raise GBMError('No session is active. Run gbm session start.')
        target, exclusions = session['state'], session['exclude']
    if exclusions != project.config()['exclude']:
        raise GBMError('Exclusions changed during the session. Restore the original exclusion settings first.')
    return {'before': snapshot(project), 'target': target, 'session': session,
            'recover': recover, 'exclude': exclusions}


def validate(project, before, target):
    for rel, digest in target['files'].items():
        project.path(rel)
        project.object(digest)
    target_dirs = set(target['directories'])
    for rel in target_dirs:
        project.path(rel)
    # Never remove an ignored file or follow a link to remove a directory.
    for rel in set(before['directories']) - target_dirs:
        directory = project.path(rel)
        for child in directory.iterdir():
            child_rel = child.relative_to(project.root).as_posix()
            if child_rel not in before['files'] and child_rel not in before['directories']:
                raise GBMError(f'Cannot remove folder {rel}: it contains excluded or linked items. Move them out first.')


def write(project, before, target):
    validate(project, before, target)
    for rel in before['files']:
        if before['files'][rel] != target['files'].get(rel):
            project.path(rel).unlink(missing_ok=True)
    for rel in sorted(set(before['directories']) - set(target['directories']),
                      key=lambda value: len(Path(value).parts), reverse=True):
        project.path(rel).rmdir()
    for rel in sorted(target['directories'], key=lambda value: len(Path(value).parts)):
        project.path(rel).mkdir(parents=True, exist_ok=True)
    for rel, digest in target['files'].items():
        if before['files'].get(rel) != digest:
            path = project.path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            project.atomic(path, project.object(digest))
    if snapshot(project) != target:
        raise GBMError('Session restoration verification failed. Run gbm recover.')


def finish(project, plan, keep):
    if project.pending():
        raise GBMError('Interrupted restore detected. Run gbm recover.')
    if project.get('session') != plan['session'] or snapshot(project) != plan['before']:
        raise GBMError('Session conflict: project or session changed after preview. Retry session end.')
    if project.config()['exclude'] != plan['exclude']:
        raise GBMError('Session conflict: exclusions changed after preview.')
    if keep:
        project.capture(plan['before']['files'])
        project.set('session', None)
        return
    validate(project, plan['before'], plan['target'])
    project.checkpoint(plan['before']['files'], 'safety')
    journal = dict(plan)
    project.set('session_pending', journal)
    write(project, plan['before'], plan['target'])
    complete(project, journal, rollback=False)


def complete(project, journal, rollback):
    target = journal['before'] if rollback else journal['target']
    # Checkpoint and metadata are committed together before clearing the journal.
    with project.db() as db:
        cursor = db.execute('INSERT INTO checkpoints(created,kind,state) VALUES (?,?,?)',
                            (time.time(), 'automatic', json.dumps(target['files'])))
        values = {'baseline': target['files'], 'head': cursor.lastrowid,
                  'project_redo': [], 'session_pending': None,
                  'session': journal['session'] if rollback else None}
        if not rollback:
            values['session_backup'] = (None if journal['recover'] else
                                        {'state': journal['before'], 'exclude': journal['exclude']})
        for key, value in values.items():
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))
        db.execute('DELETE FROM redo')
        db.execute("DELETE FROM meta WHERE key LIKE 'file_head:%'")


def recover_interrupted(project):
    journal = project.get('session_pending')
    if project.config()['exclude'] != journal['exclude']:
        raise GBMError('Restore original exclusion settings before recovering the session.')
    current = snapshot(project)
    project.checkpoint(current['files'], 'safety')
    write(project, current, journal['before'])
    complete(project, journal, rollback=True)
