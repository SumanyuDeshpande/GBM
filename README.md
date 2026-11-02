# GBM — Automatic Project Checkpointing and Recovery

GBM records saved project files locally and restores earlier versions without Git commits. Python 3.10+; no runtime dependencies. This implementation uses portable filesystem polling and argparse rather than the proposed Watchdog and Typer dependencies. Windows is the primary platform; automated CI tests target Windows, Linux, and macOS with Python 3.10 and 3.13.

## Install

Install the latest published version from PyPI:

```console
python -m pip install gbm-local
```

Update an existing installation:

```console
python -m pip install --upgrade gbm-local
```

For development, run `python -m pip install .` from the source checkout. If the `gbm` command is not on PATH, use `python -m gbm` instead. Run `gbm --help` or `gbm <command> --help` for command help.

## Command reference

Run these commands inside your project. First run `gbm watch`: every project command requires an active watcher, except `gbm watch`, `gbm status`, and help. After `gbm stop`, commands return an inactive-monitoring error with a suggestion to run `gbm watch` again. `<file>` means any monitored file path, not just `main.py`; quote paths containing spaces. Replace placeholders such as `<file>` and `N` with actual values.

| Command | Purpose |
| --- | --- |
| `gbm watch` | Record the starting state and start background monitoring. |
| `gbm status` | Show watcher status and checkpoint information. |
| `gbm stop` | Capture current changes and stop monitoring. |
| `gbm undo` | Restore the preceding project checkpoint, with confirmation. |
| `gbm undo --yes` | Restore the preceding project checkpoint without prompting. |
| `gbm undo <file>` | Restore the previous distinct recorded file version. |
| `gbm undo <file> --minutes N` | Restore the file from the latest checkpoint at or before N minutes ago. |
| `gbm redo` | Reverse a project undo, with confirmation. |
| `gbm redo --yes` | Reverse a project undo without prompting. |
| `gbm redo <file>` | Reverse a file-specific undo. |
| `gbm session start` | Begin an experiment and preserve starting files and folders. |
| `gbm session end` | Choose `y` to keep changes or `n` to restore the starting state. |
| `gbm session status` | Show whether a session is active. |
| `gbm session recover` | Review and recover the latest discarded experiment. |
| `gbm history` | Browse the latest 20 automatic project checkpoints. |
| `gbm history <file>` | Browse the latest 20 recorded changes to a file. |
| `gbm history [<file>] --limit 50` | Show up to 50 entries for the project or selected file. |
| `gbm history [<file>] --all` | Include safety snapshots in project or file history. |
| `gbm recover` | Confirm rollback of an interrupted restoration. |
| `gbm recover --yes` | Roll back an interrupted restoration without prompting. |
| `gbm experiment [file.py[:function]]` | Review and run generated Python edge-case probes. |
| `gbm experiment --plan` | Inspect the probe plan without executing project code. |
| `gbm experiment replay <id>` | Rerun a saved experiment against its recorded source snapshot. |
| `gbm --help` | List commands. |
| `gbm <command> --help` | Show a command's arguments and options. |

Square brackets above indicate an optional argument; do not type the brackets. History options can be combined, for example `gbm history src/app.py --all --limit 50`.

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

Recovery captures pending changes first. File undo walks the distinct versions for that file; redo follows its undo stack. A new checkpointed edit to a file clears that file's redo stack. Unrelated edits retain it. Project undo walks backward through automatic project checkpoints, resets file redo stacks, and restores the whole monitored manifest. `gbm redo` reverses the last project undo; repeated undo/redo operations are supported. New recorded edits, file-specific restorations, or interruption recovery invalidate project redo. File restores do not advance the automatic project checkpoint sequence. Safety checkpoints preserve the state before every restoration and are excluded from ordinary undo navigation. Project redo reverses a project undo; safety snapshots can be inspected with `gbm history --all`.

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
gbm watch
gbm session start
# Edit, create, rename, or delete monitored files and folders; save your changes.
gbm session end
# Would you like to maintain the changes you made in the session? [y/n]:
```

Choose `y` to keep your current work and close the session. Choose `n` to restore the monitored files and folders to their starting state, preserving any uncommitted work that already existed when you started. Sessions also track empty folders and file/folder type changes. Only one session may be active per project. `gbm session status` shows whether one is active. The starting snapshot persists across terminal closures and computer restarts; ordinary checkpoints never move it. An active watcher is required: run `gbm watch` before starting or ending a session. Session start and end still capture their own precise boundary states.

Before discarding, GBM shows the number of changed files and folders. Invalid answers prompt again; Ctrl+C or EOF cancels and leaves the session active. Changes made while the question is open cause a conflict and require retrying the command. Save editor buffers before ending a session and avoid editing during restoration. A discarded experiment is retained as the latest session backup, including empty folders. Run `gbm session recover`, review the summary, and confirm to restore it. End any active session first. A later discard replaces that backup; successfully recovering it consumes the backup.

Sessions follow the configured exclusions, so `.git`, `.gbm`, dependency environments, sensitive filename patterns, and other excluded paths are not reverted. If an added directory contains excluded files or links, discard refuses to remove that directory before making project changes. Move those excluded items elsewhere and retry, or keep the session. Changing exclusion patterns during a session blocks ending it until the original patterns are restored. A failed restoration leaves a persistent journal; `gbm recover` rolls back the partial restoration to the pre-discard state and keeps the session active so you can retry.

Installed libraries, databases, running processes, external services, permissions, and unsaved editor buffers are outside session recovery. This is local file recovery, not an execution sandbox. After upgrading, stop and restart old watchers (`gbm stop`, then `gbm watch`) before starting a session. Older watchers are detected and must be restarted so they honor session recovery journals.

## Version 1.4 — checkpoint history

```powershell
gbm history                       # Latest 20 automatic project checkpoints
gbm history main.py               # Latest 20 recorded changes to this file
gbm history main.py --limit 50     # Show more entries
gbm history --all                 # Include pre-restore safety snapshots
```

History shows checkpoint IDs, local timestamps with UTC offsets, checkpoint kinds, and newest entries first. Project rows summarize additions (`+`), modifications (`~`), deletions (`-`), and total recorded file count relative to the previous automatic checkpoint. File rows show ADDED, MODIFIED, or DELETED with a short content hash. Repeated identical file contents in consecutive automatic checkpoints are omitted; delete/recreate transitions remain visible. Deleted files can be queried by their original path. An unrecorded file produces a meaningful empty-history message.

`--all` includes safety snapshots, explicitly labeled SNAPSHOT or ABSENT, without changing the comparison between automatic checkpoints. `*` marks the project's undo-navigation checkpoint, not the current on-disk state or a file's independent undo position. History includes recorded checkpoints on earlier development paths after undo; it is not a list of every undo/redo operation. Browsing never checkpoints current edits, restores files, or clears redo. Pending saved edits appear only after capture. Ordinary checkpoint history tracks files; session empty-folder state is stored separately. Selecting a checkpoint for restoration is not part of this release.

## Version 1.5 — watch activates project commands

Run `gbm watch` before using undo, redo, history, recovery, or any session command. `gbm stop` records the current eligible state and disables project commands. Calling a project command before watch, after stop, or after a watcher failure returns a nonzero exit status and:

```text
[GBM] Monitoring is inactive. Run 'gbm watch' in this project before using this command.
```

`gbm watch`, `gbm status`, and help remain available while inactive. Repeating `gbm stop` while already inactive also reports the inactive error. Activation is checked against the real watcher process and its recent heartbeat, not just the presence of `.gbm`. Commands recheck activation under the project lock, including after interactive confirmation: stopping in another terminal cancels a pending restoration before it writes files.

Stopping preserves recorded history and any active session. Restart with `gbm watch` to use them again; a session's starting point does not move. Changes made while monitoring is stopped are not recorded automatically, but restarting captures the current saved state. If an interrupted restoration exists, `gbm watch` starts the watcher with checkpointing paused so you can run `gbm recover`; checkpointing resumes once recovery completes.

## Version 1.6 — Python experiments (initial release)

Run `gbm watch` first. Then:

```console
gbm experiment --plan
gbm experiment
gbm experiment src/math_utils.py:divide --cases 12 --timeout 2 --budget 30
gbm experiment replay <experiment-id>
```

GBM statically inspects monitored Python files without importing them, identifies supported top-level functions, and generates deterministic edge inputs. Annotations and literal defaults guide numeric boundaries, empty collections, malformed values, Unicode, nulls, and bounded larger inputs. The first five eligible functions are selected, with probes distributed across them. Target a file or `file.py:function` to narrow the selection. Async functions, decorated functions, positional-only or variadic functions, functions with more than four parameters, classes, and external service/API interfaces require specialized harnesses and are not covered automatically. Unsupported source files/functions are reported. No AI model, account, or network service is required to generate tests.

The plan lists the exact functions and inputs (long values are abbreviated for display), then asks before execution. `--plan` stops before execution; `--yes` explicitly approves execution without prompting. Each probe gets a fresh monitored-file copy and a separate Python process using a stdlib-only virtual environment. Runtime dependencies are not installed automatically: missing dependencies/import failures are INCONCLUSIVE. Installed project plugins, package entry points, complex package layouts, and non-Python code are not automatically tested. This first release does not run existing test suites or infer business requirements.

Limits: `--cases` 1..100 (default 20); `--timeout` 0.1..30 seconds per probe (default 2); `--budget` 1..300 seconds for the execution loop (default 30, excluding environment setup); at most 500 monitored files and 20 MiB of copied contents. Process memory is capped at 256 MiB on Windows/Linux (no enforced memory cap on macOS; this is disclosed before execution); Windows limits the process tree to four concurrent processes, and POSIX workers also receive CPU and output-file limits. Timeout/stop cleanup terminates the Windows Job or POSIX process group. These limits are guardrails, not a guarantee against hostile code escaping, spawning detached processes on POSIX, filling disk on Windows, or using external services.

**A disposable copy is not a security sandbox.** Importing and calling project code can still access absolute paths, the network, databases, and other resources available to your user. Review the plan and run only code you trust. Common credential environment variables are not inherited, but this is not a credential-isolation boundary. Relative project-file writes normally land in the disposable copy; direct access to the original project remains possible. For adversarial/untrusted programs, use a separately secured container or virtual machine instead.

Results distinguish COMPLETED (returned, correctness unverified), REJECTED_INPUT (TypeError/ValueError, not automatically a bug), FINDING (other call exceptions requiring review), INCONCLUSIVE (import/harness failure), TIMEOUT, CRASH, and NOT_RUN (budget exhausted). No output oracle is inferred, so GBM cannot prove correctness or identify a wrong returned answer automatically. Results are probes rather than verified unit tests or meaningful performance benchmarks. Exit status zero means the experiment finished; consult the report for findings.

Plans and JSON reports are saved under `.gbm/experiments/<id>/`. Reports include the inputs, statuses, durations, exception tracebacks, interpreter version, and platform. Source manifests refer to the existing content-addressed history objects. `replay` runs the original source and inputs in a new disposable workspace and creates a new report; environment/version differences can affect reproducibility. The original history objects must remain available. Runtime workspaces/environments are removed on normal completion or cancellation where possible; report files remain. If GBM itself is forcibly terminated, runtime directories and a RUNNING report may remain for manual inspection. Experiment IDs and paths are printed after completion; cancelled runs retain their report beneath `.gbm/experiments`.

An active watcher is checked before planning, after confirmation, between cases, and during worker execution. `gbm stop` or a lost watcher cancels remaining work and terminates the current worker. Reports can contain sensitive returned values or errors; review them before sharing.
