# baccy implementation plan

## Goal

Build `baccy` as a macOS-first Python backup library and command-line
application. It discovers configured local, removable, and mounted network
sources, plus unconfigured removable camera and recs volumes, copies their files
into one central backup root without propagating source deletions, and can
either run one backup pass from a terminal or stay running under `launchd`.

The first version must work particularly well for recs session directories.
It backs up every supported file byte-for-byte, prioritizing `.toml` and
`.jsonl` metadata so that session evidence is copied before large media. It
does not import recs or rewrite, finalize, migrate, or otherwise interpret a
recs session.

## Version-one scope

Version one supports:

- macOS only;
- explicitly configured local directories;
- removable volumes and network shares after macOS has mounted them;
- automatic discovery of removable camera volumes and volumes containing recs
  sessions;
- one configured central backup directory;
- one-shot command-line backups;
- a foreground polling mode with the same behavior as the background service;
- installation and control of a per-user `launchd` service;
- recs session metadata, including `recording.toml`,
  `session-record.jsonl`, native event JSONL files, and recovery or edit TOML;
- ordinary files such as audio and photos once they are stable;
- durable records of completed copies and failures.

Version one does not mount remote machines or manage credentials. It can publish
selected recs sessions with the user's existing SSH configuration. Each project
supplies an SCP-style `HOST:/absolute/path` base, a minimum audio duration, and
optionally named tracks. Upload session-relative files below that base, creating
the corresponding remote directories. By default publish the journal and
finalized recording document plus the final two channels of the source device
with the most channels, excluding audio shorter than one minute. Record local
upload size and modification-time state so unchanged selected files are not
uploaded again.

## macOS service behavior

Use the existing `reccy.services` support instead of implementing another
`launchctl` wrapper. Add a baccy service specification with a stable launchd
label, and have the installed service run the same foreground command that is
available in a terminal.

The version-one service is a per-user LaunchAgent with `RunAtLoad` and
`KeepAlive`. It starts after that user logs in following a restart, restarts
after an unexpected exit, and has access to that user's mounted volumes and
network shares. This is intentionally not a privileged LaunchDaemon. Running
before login would require a system-wide installation, root-owned
configuration, and a decision about credentials and volume access; that is a
different deployment model and must be confirmed before implementation if
"after restart" is intended to mean "before any user logs in."

## User interface

Define Tyro commands as frozen Pydantic models and keep command routing thin:

- `baccy backup`: perform one discovery and backup pass, report a summary, and
  exit nonzero if any requested source or copy failed;
- `baccy backup -d` or `baccy backup --dry-run`: report files that would copy
  without changing the backup root;
- `baccy watch`: run the polling loop in the foreground until interrupted;
- `baccy service install|status|start|stop|restart|uninstall`: manage the
  LaunchAgent through reccy.

Both `backup` and `watch` call the same synchronous scan-and-copy engine.
`watch` only adds polling, signal-aware shutdown, and repeated status updates.
The service invokes `baccy watch`; there is no separate daemon implementation.

When the service sees a new copy failure, display a macOS notification. Suppress
repeated identical failures until they recover or change. Do not notify for
unavailable sources, automatic removable-media absence, or rejected network
hosts.

For a recs session, append `session-record.jsonl` only after verifying the
already backed-up prefix. Defer WAV and FLAC files while their lifecycle journal
contains `file_started` without a corresponding terminal record. Copy them as
ordinary atomic snapshots after `file_finished` or `file_discarded` closes that
lifecycle entry.

Every ten seconds, watch inspects the local ARP table. A newly seen unicast MAC
address is tried with batch-mode SSH. Baccy does not record or verify host keys,
then relies on certificate authentication to determine whether the system is
eligible. Connection failures are retried after two and four seconds;
authentication rejections and systems without `~/recs` are remembered until the
process exits. If `~/recs` exists, its files become a network source; other
files on the system are never considered.

Read settings from one TOML configuration file, with a command-line option to
select a different file. When the standard configuration file is absent, use
automatic removable discovery and `~/baccy` on the main drive. A
missing explicitly selected configuration remains an error. The model contains:

- the central backup root;
- a list of sources, each with a unique stable name and either a fixed path or
  a mounted-volume match;
- whether qualifying removable volumes are discovered automatically;
- include and exclude patterns;
- the polling interval;
- the interval for which an ordinary file must remain unchanged before it is
  eligible to copy.

Use source names, not transient `/Volumes` paths, as destination identities.
Reject duplicate names, a backup root inside a source, a source inside the
backup root, and overlapping source roots before copying anything.

## Package structure

Keep the modules small and responsibility-based:

```text
src/baccy/
  __main__.py       command routing and user-facing errors
  cli.py            Tyro command models and handlers
  config.py         validated TOML configuration
  discovery.py      configured paths and mounted-volume discovery
  models.py         source, candidate, result, and status models
  scan.py           deterministic traversal and recs-aware priority
  copy.py           safe snapshots and atomic destination writes
  catalog.py        append-only backup, upload, and failure event records
  backup.py         one-pass orchestration
  watch.py          foreground polling loop and shutdown
  application.py    reccy lifecycle and service integration
  service.toml      service definition
```

The public library API should initially expose only the validated models and a
single one-pass backup operation. CLI formatting, launchd control, polling, and
macOS discovery remain application concerns.

## Source discovery

Support two explicit source forms:

1. A fixed path for local directories and already-mounted network shares.
2. A mounted volume identified by its macOS volume UUID, with an optional
   expected volume name for readable diagnostics.

By default, also inspect unconfigured removable or ejectable volumes. Admit an
automatic volume only when its root contains a case-insensitive `DCIM`
directory or its directory tree contains a valid recs `recording.toml` or
`session-record.jsonl`. For camera volumes, select only recognized photo files
below `DCIM`. For recs volumes, select complete detected session directories.
Never select unrelated files elsewhere on an automatically discovered volume.
Store selections under the stable source name `removable-VOLUME_UUID`. Ignore
ordinary unconfigured volumes, unrelated files that merely use the recs marker
names, volumes without a UUID, explicitly configured roots, and the volume
containing the backup root. Configuration can disable automatic removable
discovery.

Discover candidate volumes under `/Volumes` and obtain their identifiers from
`diskutil info -plist`. Never treat a matching display name alone as a durable
identity. A missing source is not an empty source and must never cause removal
from the backup. In one-shot mode it is reported as unavailable; in watch mode
it remains pending and is retried on later polls.

Traverse without following symlinks. Sort directory entries for deterministic
behavior, ignore baccy's own temporary files, and apply exclusions before
statting or hashing large files. Detect mount disappearance during a pass and
record the source as interrupted rather than guessing that traversal completed.

## Permanent backup layout and catalog

Store user-visible copies below:

```text
BACKUP_ROOT/sources/SOURCE_NAME/RELATIVE_PATH
```

Keep an append-only `BACKUP_ROOT/events.jsonl` containing backup and upload
successes, failures, and deferred files. A backup success includes source name,
source-relative path, source size and modification time, SHA-256, destination,
copy time, and result. Write success lines only after the destination commit
succeeds and flush each complete line.

The normal destination path represents the newest successfully copied bytes.
Replace changed files atomically, but never delete a destination merely because
a source file or source volume disappears.

Use the event log as an optimization, not the sole proof that a backup exists.
Before skipping a candidate, confirm that the destination still exists and
matches the recorded size. Hash it when metadata is inconsistent. Event
records are append-only so an interrupted write can be recovered by ignoring
one incomplete final line.

## Safe copy rules

Copy into a temporary file in the destination directory, flush and `fsync` it,
then use an atomic rename. Create parent directories as needed, but never
overwrite through a symlink. Preserve file modification time; do not require
ownership or extended-attribute preservation in version one.

For ordinary files, including audio and photos:

1. Require size and modification time to remain unchanged for the configured
   stability interval. In one-shot mode, a file younger than that interval is
   deferred; watch mode can additionally compare observations across passes.
2. Capture file identity and metadata before copying.
3. Stream the file through SHA-256 into the temporary destination.
4. Recheck identity, size, and modification time after copying.
5. Commit only if both observations match; otherwise discard the temporary
   candidate and retry on a later pass.

For append-only `.jsonl` files, take a bounded prefix snapshot so live recs
journals and event streams can be backed up without waiting for recording to
stop:

1. Open the file and capture its identity and starting size.
2. Copy exactly that many bytes.
3. Require an empty snapshot or a trailing newline, because recs writes one
   complete UTF-8 JSON record per line.
4. Confirm that the source still has the same identity and has not shrunk.
5. Commit the prefix even if later lines were appended during the copy.

If a JSONL file is replaced, truncated, has a partial final line, or changes in
any non-append way, do not commit that attempt. TOML files use the ordinary
stable-file rule. These rules are byte-level only and do not claim that generic
JSONL files have recs semantics.

## recs session handling

Recognize a recs session directory when it contains `session-record.jsonl` or
`recording.toml`. Preserve the complete relative directory structure because
paths inside both files are relative to the session directory and continuation
links can cross volume roots.

Within every scan, process candidates in this order:

1. `session-record.jsonl`, `recording.toml`, and other `.toml` files;
2. event `.jsonl` files under `midi/`, `osc/`, and `key/`;
3. all remaining files, including audio and photos.

Do not consider `recording.toml` a substitute for its assets. A complete backup
eventually includes every selected file in the session. Do not invoke recs
finalization or migration, and do not reject open sessions: the append-only
journal is valuable backup evidence before a finalized recording document
exists.

Test fixtures should cover a finalized session, an active journal that grows
during a copy, a torn final JSONL line, a session with native event JSONL, and a
session whose large media remains unstable while metadata succeeds.

## Execution and failure model

Use `BACKUP_ROOT/.lock` so only one backup pass writes a backup root at a time.
A second CLI invocation must fail clearly rather than race the service.

Handle files independently after configuration and destination validation.
Record and log a failed file, continue with unrelated files, and return a
summary containing discovered, copied, unchanged, deferred, unavailable, and
failed counts. Treat a read-only or full destination as a pass failure and
stop scheduling more copies for that destination, while leaving prior backups
untouched.

The watch loop uses ordinary synchronous code. It catches termination signals,
finishes or abandons the current temporary copy safely, writes final status,
and exits. Reccy owns service installation, rotating logs, and service status;
baccy owns backup progress and backup-specific errors.

## Implementation sequence

### 1. Project and command foundation

- Add runtime and development dependencies in the required separate dependency
  commit: Pydantic, Tyro, reccy, pytest, Ruff, ty, pyupgrade, and the existing
  project test conventions.
- Add the package entry point, frozen command models, configuration loader, and
  configuration validation.
- Add CLI help and configuration tests before implementing copy behavior.

### 2. Discovery and deterministic scanning

- Implement fixed-path discovery, configured macOS volume UUID discovery, and
  qualified automatic removable-volume discovery.
- Implement safe traversal, include/exclude matching, source/destination
  containment checks, and metadata-first ordering.
- Test missing and disappearing sources, symlinks, overlapping roots, and recs
  priority with temporary directory fixtures.

### 3. Durable one-pass backup

- Implement the destination lock, append-only event log, stability tracking,
  streaming hashes, temporary writes, and atomic commits.
- Implement JSONL prefix snapshots separately from stable ordinary-file copies.
- Expose the one-pass library operation and wire it to `baccy backup`.
- Test interruption boundaries so neither a destination nor catalog claims an
  incomplete copy.

### 4. Foreground watch mode

- Add the synchronous polling loop and signal-aware shutdown.
- Persist enough observed file metadata to enforce the stability interval
  across passes without requiring a database.
- Add status summaries and focused tests with an injected clock and scanner;
  do not use real sleeps in unit tests.

### 5. LaunchAgent integration

- Add the baccy reccy application and service specification.
- Add service commands and install `baccy watch` as the service payload.
- Verify generated plist properties, command arguments, paths, `RunAtLoad`, and
  `KeepAlive` in unit tests.
- Manually verify install, restart after process failure, restart after macOS
  reboot and user login, log paths, status, and clean uninstall on a Mac.

### 6. Documentation and release check

- Expand the README with installation, configuration, one-shot, foreground,
  service, recs, restore, version-retention, and troubleshooting examples.
- Document that network shares must already be mounted and that publication is
  not part of version one.
- Run the repository's complete Python verification sequence and perform a
  final recovery exercise from both the current tree and one retained version.

## Acceptance criteria

Version one is complete when:

- the library can run one backup pass against configured sources and produce a
  structured result;
- without a configuration file, the CLI uses automatic removable discovery and
  writes selected content below `~/baccy`;
- `baccy backup` performs that pass without installing or starting a service;
- `baccy watch` runs the same engine repeatedly in the foreground;
- the LaunchAgent starts after login, stays alive, and returns after a reboot;
- an inserted configured volume is discovered by UUID and backed up;
- an unconfigured removable camera or recs volume is backed up, while an
  ordinary unconfigured removable volume is ignored;
- automatic camera backups contain only photo files below `DCIM`, and automatic
  recs backups contain only complete session directories;
- a configured mounted network share is backed up when present and merely
  reported unavailable when absent;
- recs TOML and complete JSONL prefixes are committed before stable media;
- changing ordinary files are deferred without corrupting the destination;
- source deletion, unmount, replacement, and same-path content changes never
  remove the last recoverable committed bytes;
- interrupted copies leave neither a partial visible destination nor a false
  success record;
- all automated checks pass, and the macOS lifecycle checks are recorded as
  manual validation rather than inferred from unit tests.

## Additional work beyond the prompt

None.
