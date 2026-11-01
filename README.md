# GBM — Automatic Project Checkpointing and Recovery

GBM records saved project files locally and restores earlier versions without Git commits. Python 3.10+; no runtime dependencies. This implementation uses portable filesystem polling and argparse rather than the proposed Watchdog and Typer dependencies. Windows is the tested platform; Linux/macOS code paths require validation.

## Install

From this directory:

```powershell

python -m pip install .

gbm --help

```

Or install the wheel from `dist`. If your Python Scripts folder is not on PATH, use `python -m gbm` in place of `gbm`. Source checkout usage also works from this directory without installation.

## Use

Open a terminal in the project to protect:

```powershell

gbm watch                 # Initial checkpoint; detached background process

gbm status                # Process liveness + recent heartbeat

gbm undo main.py          # Previous distinct recorded file version

gbm undo main.py --minutes N # Jump to the last checkpoint at/before N minutes ago

gbm redo main.py          # Reapply file undo; supports multiple levels

gbm undo                  # Preview affected count and confirm project undo

gbm redo                  # Reverse project undo (asks for confirmation)

gbm redo --yes            # Reverse project undo without confirmation

gbm undo --yes            # Project undo without interactive confirmation

gbm stop                  # Capture current state, stop monitoring

gbm recover               # Roll back an interrupted restore

```

`watch` returns after the background process starts. It runs as your user, without a visible console window on Windows. It is not an operating-system service and does not restart at login. Run `watch` again after reboot or a watcher failure. Errors are written to `.gbm/watcher.log`.

Commands discover the nearest ancestor with `.gbm`; otherwise `watch` initializes the current directory. File arguments are relative to the current working directory. Checkpoints include additions and deletions, binary contents and empty files. A rename is recorded as deletion plus addition. Unsaved editor buffers are not recorded.

Example: start watching with `main.py` containing `print(1)`, save `print(2)`, wait about three seconds, then run `gbm undo main.py`. The file returns to `print(1)`; `gbm redo main.py` restores `print(2)`.

## History and recovery semantics

`.gbm/history.db` stores full content manifests and operation metadata; `.gbm/objects` stores deduplicated SHA-256 objects. History persists across terminal sessions. There is no automatic pruning or size limit; disk usage grows with unique contents.

The default scan interval is 0.5 seconds with two seconds of inactivity before a checkpoint. Files are read and hashed on each scan, so this release is intended for small to medium source trees. Rapid intermediate versions between scans are not guaranteed to be captured. Changes that never settle are saved when `stop` is called or a restore is requested.

Recovery captures pending changes first. File undo walks the distinct versions for that file; redo follows its undo stack. A new checkpointed edit to a file clears that file's redo stack. Unrelated edits retain it. Project undo walks backward through automatic project checkpoints, resets file redo stacks, and restores the whole monitored manifest. `gbm redo` reverses the last project undo; repeated undo/redo operations are supported. New recorded edits, file-specific restorations, or interruption recovery invalidate project redo. File restores do not advance the automatic project checkpoint sequence. Safety checkpoints preserve the state before every restoration and are excluded from ordinary undo navigation. Project redo reverses a project undo; a general safety-checkpoint browser is not included.

Restore plans compare the current manifest immediately before application. A mismatch aborts before project writes. GBM serializes its own operations with an OS file lock, checks object hashes, writes each file with atomic replacement, verifies the final manifest, and keeps a durable pending-operation journal. A failed or interrupted restore blocks further checkpointing/restores until `gbm recover` rolls back to its pre-restore state. Recovery first preserves the currently visible contents as another safety checkpoint. A multi-file restore is not a filesystem-wide atomic transaction. External editors do not honor GBM's lock: avoid saving files during restoration; races during the write phase can still occur.

## Scope and exclusions

Edit `.gbm/config.json` while monitoring is stopped. `exclude` is a list of shell-style patterns matched against each path component. `.gbm` and `.git` are always excluded. Defaults also exclude `node_modules`, `.venv`, `venv`, `__pycache__`, `dist`, `build`, `.pytest_cache`, `.mypy_cache`, `.env`, `.env.*`, `*.pem`, `*.key`, `*.pfx`, `*.pyc`, `*.swp`, and `*.tmp`.

These patterns are not a secret scanner. Other sensitive files will be stored unless you exclude them. Objects are not encrypted. Removing an exclusion or changing scope after recording history can make older restores fail; keep the monitoring policy stable. GBM never intentionally traverses symbolic links or Windows junctions. Keep its internal storage private and do not modify it manually. Paths and history are trusted local data, not a security boundary against another process actively replacing filesystem entries.

For a conventional `.git` directory, initialization adds `/.gbm/` to `.git/info/exclude`. For Git worktrees or before creating a Git repository, add `.gbm/` to your `.gitignore` yourself.

Only file bytes and existence are versioned. Permissions, timestamps, alternate data streams, hard-link relationships are not preserved. Ordinary checkpoints do not preserve empty directories; sessions track their existence separately. File/directory type collisions or locked files can prevent a restore; the journal permits recovery after resolving the obstruction. No cloud backup, GUI, encryption, history export, or production database recovery is included. GBM complements source control and backups.

## Validate and publish

```powershell

python -m unittest discover -s tests -v

python -m pip wheel . --no-deps --wheel-dir dist

```

Tests cover file/project history, repeated undo/redo, additions/deletions, preservation of pending edits, conflicts, object corruption, interruption recovery, exclusions, deduplication, and a real detached Windows watcher lifecycle.

The source is prepared for a GitHub repository, including MIT licensing and CI. The destination repository is https://github.com/SumanyuDeshpande/GBM. Published releases are available on PyPI as `gbm-local`; install or upgrade with `python -m pip install --upgrade gbm-local`. Treat this as an initial implementation requiring broader real-project testing before relying on it as your only recovery mechanism.

## Time-based file undo (1.1)

`gbm undo main.py --minutes 5` restores only that file from the most recent automatic checkpoint at or before five minutes before the command starts. Fractional minutes work (`--minutes 0.5` is 30 seconds). The selected checkpoint's local timestamp is displayed. If no checkpoint is old enough, GBM reports this without restoring anything; it never guesses an older version. If the file did not exist in the selected checkpoint, it is removed, with its current contents preserved for `gbm redo main.py`. Unrelated files stay unchanged. Redo reverses the entire jump in one command. Ordinary file undo after a jump continues backward from the selected checkpoint. A repeated time jump uses the current clock time, not the last undo position. Checkpoints record observed saved states, not exact historical filesystem contents between scans.

## Version 1.2 — project redo

Run `gbm redo` to restore the monitored project state preserved by the last successful `gbm undo`. It reverses file additions, modifications, and deletions together. Review the affected-file count and confirm, or use `gbm redo --yes`. File redo remains `gbm redo main.py`. Project redo is available across terminal sessions, but new saved edits or file-specific restores invalidate it. Stop and restart any watchers running an older GBM version after upgrading so they use the updated history rules. Before every restoration, current contents are preserved and the existing recovery journal and conflict checks remain in use.


## Version 1.3 — experiment sessions

```powershell
gbm session start
# Edit, create, rename, or delete monitored files and folders; save your changes.
gbm session end
# Would you like to maintain the changes you made in the session? [y/n]:
```

Choose `y` to keep your current work and close the session. Choose `n` to restore the monitored files and folders to their starting state, preserving any uncommitted work that already existed when you started. Sessions also track empty folders and file/folder type changes. Only one session may be active per project. `gbm session status` shows whether one is active. The starting snapshot persists across terminal closures and computer restarts; ordinary checkpoints never move it. Monitoring is optional: session start and end capture the required states themselves. Use `gbm watch` for intermediate automatic checkpoints.

Before discarding, GBM shows the number of changed files and folders. Invalid answers prompt again; Ctrl+C or EOF cancels and leaves the session active. Changes made while the question is open cause a conflict and require retrying the command. Save editor buffers before ending a session and avoid editing during restoration. A discarded experiment is retained as the latest session backup, including empty folders. Run `gbm session recover`, review the summary, and confirm to restore it. End any active session first. A later discard replaces that backup; successfully recovering it consumes the backup.

Sessions follow the configured exclusions, so `.git`, `.gbm`, dependency environments, sensitive filename patterns, and other excluded paths are not reverted. If an added directory contains excluded files or links, discard refuses to remove that directory before making project changes. Move those excluded items elsewhere and retry, or keep the session. Changing exclusion patterns during a session blocks ending it until the original patterns are restored. A failed restoration leaves a persistent journal; `gbm recover` rolls back the partial restoration to the pre-discard state and keeps the session active so you can retry.

Installed libraries, databases, running processes, external services, permissions, and unsaved editor buffers are outside session recovery. This is local file recovery, not an execution sandbox. After upgrading, stop and restart old watchers (`gbm stop`, then `gbm watch`) before starting a session. Older watchers are detected and must be restarted so they honor session recovery journals.
