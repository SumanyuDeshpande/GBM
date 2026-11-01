"""Read-only checkpoint history browsing."""
from collections import deque
from contextlib import closing
import json
import sqlite3
from .core import GBMError


def history(project, rel=None, limit=20, include_safety=False):
    if limit <= 0:
        raise GBMError('--limit must be greater than zero.')
    if rel is not None:
        project.path(rel)
    # Opening in read-only mode prevents browsing from creating or updating history.
    uri = (project.home / 'history.db').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute('BEGIN')
        row = db.execute("SELECT value FROM meta WHERE key='head'").fetchone()
        head = json.loads(row[0]) if row else None
        query = 'SELECT id,created,kind,state FROM checkpoints'
        if not include_safety:
            query += " WHERE kind='automatic'"
        query += ' ORDER BY id'
        entries = deque(maxlen=limit)
        previous = {}
        for number, created, kind, payload in db.execute(query):
            state = json.loads(payload)
            if kind == 'automatic':
                if rel is not None:
                    old, new = previous.get(rel), state.get(rel)
                    previous = state
                    if old == new:
                        continue
                    change = 'ADDED' if old is None else 'DELETED' if new is None else 'MODIFIED'
                    detail = (new[:12] if new else '-')
                else:
                    added = len(state.keys() - previous.keys())
                    deleted = len(previous.keys() - state.keys())
                    modified = sum(state[p] != previous[p] for p in state.keys() & previous.keys())
                    change = f'+{added} ~{modified} -{deleted}'
                    detail = f'{len(state)} files'
                    previous = state
            elif rel is not None:
                if rel not in state and rel not in previous:
                    continue
                change = 'SNAPSHOT' if rel in state else 'ABSENT'
                detail = state[rel][:12] if rel in state else '-'
            else:
                change, detail = 'SNAPSHOT', f'{len(state)} files'
            entries.append({'id': number, 'created': created, 'kind': kind,
                            'change': change, 'detail': detail, 'head': number == head})
        return list(reversed(entries))
