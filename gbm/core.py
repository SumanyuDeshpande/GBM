"""Persistent history, bounded filesystem access, and serialized recovery."""
import contextlib
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

DEFAULTS = {"settle_seconds": 2.0, "poll_seconds": 0.5,
            "exclude": [".gbm", ".git", "node_modules", ".venv", "venv", "__pycache__",
                        "dist", "build", ".pytest_cache", ".mypy_cache", ".env", ".env.*",
                        "*.pem", "*.key", "*.pfx", "*.pyc", "*.swp", "*.tmp"]}

class GBMError(Exception):
    pass

class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / '.gbm'
        if self.home.is_symlink():
            raise GBMError('.gbm must not be a symbolic link.')

    @classmethod
    def discover(cls, start=None):
        start = Path(start or Path.cwd()).resolve()
        for directory in (start, *start.parents):
            if (directory / '.gbm').is_dir():
                return cls(directory)
        return cls(start)

    def initialize(self):
        self.home.mkdir(exist_ok=True)
        (self.home / 'objects').mkdir(exist_ok=True)
        if not (self.home / 'config.json').exists():
            self.atomic(self.home / 'config.json', json.dumps(DEFAULTS, indent=2).encode())
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS checkpoints(id INTEGER PRIMARY KEY, created REAL, kind TEXT, state TEXT);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS redo(path TEXT PRIMARY KEY, stack TEXT);
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, created REAL, status TEXT, before_state TEXT, target_state TEXT);
            ''')
        # Git's local exclude avoids changing tracked project configuration.
        git = self.root / '.git'
        if git.is_dir() and not git.is_symlink():
            exclude = git / 'info' / 'exclude'
            exclude.parent.mkdir(exist_ok=True)
            old = exclude.read_text() if exclude.exists() else ''
            if '/.gbm/' not in old.splitlines():
                exclude.write_text(old + '\n/.gbm/\n')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.home / 'history.db', timeout=15)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self):
        with (self.home / 'lock').open('a+b') as f:
            f.seek(0)
            f.write(b'0')
            f.flush()
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                deadline = time.monotonic() + 15
                while True:
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise GBMError('Project is busy; retry shortly.')
                        time.sleep(.05)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                f.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    @staticmethod
    def atomic(path, data):
        temp = path.with_name(path.name + '.gbm-' + uuid.uuid4().hex)
        try:
            with temp.open('xb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def config(self):
        config = json.loads((self.home / 'config.json').read_text())
        if config['poll_seconds'] <= 0 or config['settle_seconds'] < 0:
            raise GBMError('Monitoring intervals must be positive (settle may be zero).')
        return config

    def eligible(self, rel):
        parts = Path(rel).parts
        return '.gbm' not in parts and '.git' not in parts and not any(
            fnmatch.fnmatch(part, pattern) for part in parts for pattern in self.config()['exclude'])

    def path(self, rel):
        candidate = self.root / rel
        if Path(rel).is_absolute() or '..' in Path(rel).parts or not self.eligible(rel):
            raise GBMError('Path is outside the monitored scope.')
        current = self.root
        for part in Path(rel).parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise GBMError('Symbolic links and junctions are not monitored.')
        if not candidate.resolve().is_relative_to(self.root):
            raise GBMError('Path escapes the project.')
        return candidate

    def file_arg(self, value):
        candidate = Path(value)
        if '..' in candidate.parts:
            raise GBMError('Path is outside the monitored scope.')
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        # Canonicalize Windows short-name aliases just as Project.__init__ does.
        # Check the original path first so resolution cannot hide a symlink.
        for part in (candidate, *candidate.parents):
            if part.resolve() == self.root:
                break
            if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
                raise GBMError('Symbolic links and junctions are not monitored.')
        try:
            rel = candidate.resolve().relative_to(self.root).as_posix()
        except ValueError:
            raise GBMError('File must be inside the project.')
        self.path(rel)
        return rel

    def scan(self):
        state = {}
        def scan_error(error):
            raise error
        for directory, dirs, files in os.walk(self.root, followlinks=False, onerror=scan_error):
            parent = Path(directory)
            dirs[:] = [d for d in dirs if self.eligible((parent / d).relative_to(self.root))
                       and not (parent / d).is_symlink()
                       and not (hasattr(parent / d, 'is_junction') and (parent / d).is_junction())]
            for name in files:
                path = parent / name
                rel = path.relative_to(self.root).as_posix()
                if not self.eligible(rel) or path.is_symlink():
                    continue
                data = self.path(rel).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                obj = self.home / 'objects' / digest
                if not obj.exists():
                    self.atomic(obj, data)
                state[rel] = digest
        return state

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def pending(self):
        with self.db() as db:
            return db.execute("SELECT id FROM operations WHERE status='pending'").fetchall()

    def checkpoint(self, state, kind='automatic'):
        with self.db() as db:
            cursor = db.execute('INSERT INTO checkpoints(created,kind,state) VALUES (?,?,?)',
                                (time.time(), kind, json.dumps(state)))
            number = cursor.lastrowid
        return number

    def capture(self, state=None):
        if self.pending():
            raise GBMError('Interrupted restore detected. Run gbm recover before continuing.')
        state = self.scan() if state is None else state
        baseline = self.get('baseline')
        if baseline == state:
            return None
        number = self.checkpoint(state)
        self.set('project_redo', [])
        # New edits invalidate redo only for the edited files.
        with self.db() as db:
            for rel in set(baseline or {}) | set(state):
                if baseline is not None and baseline.get(rel) != state.get(rel):
                    db.execute('DELETE FROM redo WHERE path=?', (rel,))
                    db.execute('DELETE FROM meta WHERE key=?', ('file_head:' + rel,))
        self.set('baseline', state)
        self.set('head', number)
        return number

    def preview(self, rel=None, redo=False, minutes=None):
        if minutes is not None:
            if redo or not rel:
                raise GBMError('--minutes requires a file-specific undo.')
            if not math.isfinite(minutes) or minutes <= 0:
                raise GBMError('--minutes must be a finite number greater than zero.')
        cutoff = time.time() - minutes * 60 if minutes is not None else None
        if self.pending():
            raise GBMError('Interrupted restore detected. Run gbm recover.')
        current = self.scan()
        self.capture(current)
        if redo and rel is None:
            stack = self.get('project_redo', [])
            if not stack:
                raise GBMError('No eligible project redo exists. Undo the project first.')
            entry = stack[-1]
            if current != entry['expected'] or self.get('head') != entry['expected_head']:
                raise GBMError('Project changed since undo; project redo is no longer available.')
            return current, entry['state'], {'head': entry['head'], 'project_redo': stack[:-1],
                                              'expected_project_redo': stack,
                                              'expected_head': self.get('head')}
        if redo:
            with self.db() as db:
                row = db.execute('SELECT stack FROM redo WHERE path=?', (rel,)).fetchone()
            stack = json.loads(row[0]) if row else []
            if not stack:
                raise GBMError('No eligible redo state exists for this file.')
            target = dict(current)
            self.assign(target, rel, stack[-1]['value'])
            return current, target, {'redo': stack[:-1], 'rel': rel, 'file_head': stack[-1]['head']}
        head = self.get('head')
        if cutoff is not None:
            with self.db() as db:
                row = db.execute(
                    "SELECT id,created,state FROM checkpoints WHERE kind='automatic' AND created<=? "
                    "ORDER BY created DESC,id DESC LIMIT 1", (cutoff,)).fetchone()
            if not row:
                raise GBMError('No checkpoint exists at or before the requested time. Nothing restored.')
            number, created, payload = row
            target = dict(current)
            self.assign(target, rel, json.loads(payload).get(rel))
            return current, target, {'rel': rel, 'push': current.get(rel),
                                     'file_head': number,
                                     'old_head': self.get('file_head:' + rel, head),
                                     'checkpoint': number, 'created': created}
        with self.db() as db:
            rows = db.execute("SELECT id,state FROM checkpoints WHERE id<=? AND kind='automatic' ORDER BY id DESC", (head or 0,)).fetchall()
        if rel:
            cursor = self.get('file_head:' + rel, head)
            versions = []
            for number, payload in rows:
                if number > cursor:
                    continue
                value = json.loads(payload).get(rel)
                if not versions or versions[-1][1] != value:
                    versions.append((number, value))
            if len(versions) < 2:
                raise GBMError('No previous recoverable version exists for this file.')
            target = dict(current)
            self.assign(target, rel, versions[1][1])
            return current, target, {'rel': rel, 'push': current.get(rel),
                                     'file_head': versions[1][0], 'old_head': cursor}
        if len(rows) < 2:
            raise GBMError('No previous project checkpoint exists.')
        target = json.loads(rows[1][1])
        stack = self.get('project_redo', [])
        expected_stack = list(stack)
        stack.append({'state': current, 'head': head,
                      'expected': target, 'expected_head': rows[1][0]})
        return current, target, {'head': rows[1][0], 'project_redo': stack,
                                 'expected_project_redo': expected_stack, 'expected_head': head}

    @staticmethod
    def assign(state, rel, value):
        if value is None:
            state.pop(rel, None)
        else:
            state[rel] = value

    def apply(self, before, target, action):
        if self.scan() != before:
            raise GBMError('Recovery conflict: project changed after preview. Nothing restored.')
        if 'expected_project_redo' in action and (
                self.get('project_redo', []) != action['expected_project_redo'] or
                self.get('head') != action['expected_head']):
            raise GBMError('Recovery conflict: history changed after preview. Nothing restored.')
        self.checkpoint(before, 'safety')
        # Validate every object and destination before any project mutation.
        for rel, digest in target.items():
            self.path(rel)
            self.object(digest)
        operation = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?)',
                       (operation, time.time(), 'pending', json.dumps(before), json.dumps(target)))
        self.write_state(before, target)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                       ('project_redo', json.dumps(action.get('project_redo', []))))
            if 'rel' in action:
                rel = action['rel']
                if 'redo' in action:
                    stack = action['redo']
                else:
                    row = db.execute('SELECT stack FROM redo WHERE path=?', (rel,)).fetchone()
                    stack = json.loads(row[0]) if row else []
                    stack.append({'value': action['push'], 'head': action['old_head']})
                db.execute('INSERT OR REPLACE INTO redo VALUES (?,?)', (rel, json.dumps(stack)))
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('file_head:' + rel, json.dumps(action['file_head'])))
            else:
                db.execute('DELETE FROM redo')
                db.execute("DELETE FROM meta WHERE key LIKE 'file_head:%'")
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('baseline', json.dumps(target)))
            if 'head' in action:
                db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('head', json.dumps(action['head'])))
            db.execute("UPDATE operations SET status='complete' WHERE id=?", (operation,))

    def object(self, digest):
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise GBMError('Invalid history object reference.')
        data = (self.home / 'objects' / digest).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise GBMError('History object is corrupt; restoration stopped.')
        return data

    def write_state(self, before, target):
        for rel in sorted(set(before) | set(target), key=lambda r: (-len(Path(r).parts), r)):
            if before.get(rel) == target.get(rel):
                continue
            path = self.path(rel)
            if rel not in target:
                path.unlink(missing_ok=True)
                # Only remove now-empty directories, never unmonitored contents.
                parent = path.parent
                while parent != self.root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
        for rel, digest in target.items():
            if before.get(rel) != digest:
                path = self.path(rel)
                path.parent.mkdir(parents=True, exist_ok=True)
                self.atomic(path, self.object(digest))
        if self.scan() != target:
            raise GBMError('Restore verification failed; recovery journal retained.')

    def recover(self):
        with self.db() as db:
            rows = db.execute("SELECT id,before_state FROM operations WHERE status='pending' ORDER BY created DESC").fetchall()
        if not rows:
            raise GBMError('No interrupted restoration exists.')
        current = self.scan()
        self.checkpoint(current, 'safety')
        target = json.loads(rows[0][1])
        self.write_state(current, target)
        with self.db() as db:
            db.execute("UPDATE operations SET status='recovered' WHERE status='pending'")
            db.execute('DELETE FROM redo')
        self.set('baseline', target)
        self.set('project_redo', [])
