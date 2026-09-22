# baccy

`baccy` is a permanent backup library and macOS command-line application for
audio files, photos, and other assets. It copies configured local directories,
memory cards, and already-mounted network shares to one central backup root.
It can run once from a terminal or continuously as a per-user LaunchAgent.

The first version never deletes backups because a source disappears. When a
source path changes, baccy retains the previous destination bytes under the
backup root before atomically replacing the visible current copy.

## Install

Use `uv` from a checkout:

```sh
uv sync
```

The `baccy` command is then available through `uv run baccy`.

## Configuration

By default baccy looks for:

```text
~/Library/Application Support/baccy/config.toml
```

If that file does not exist, baccy still runs with automatic removable-drive
discovery enabled and writes selected backups to:

```text
~/baccy
```

This makes `baccy backup`, `baccy watch`, and a normally installed service
useful without creating a configuration file. A missing path supplied
explicitly with `--config` remains an error.

Pass `--config PATH` to any backup, watch, or service-install command to use a
different file.

```toml
backup_root = "/path/to/baccy"
discover_removable = true
poll_seconds = 60
stability_seconds = 60
verbose = true

[destinations.show_server]
kind = "ssh"
url = "user@example.org:/srv/shows"

[destinations.archive]
kind = "s3"
bucket = "show-recordings"
prefix = "upcoming-shows"

[access.show_listeners]
ssh_mode = "0644"
s3_acl = "public-read"

[[projects.concert.uploads]]
name = "main-mp3"
match = "main and duration > 120"
encoding = { format = "mp3", bitrate_kbps = 128 }
filename = "{timestamp}.{extension}"
destination = "show_server"
access = { profile = "show_listeners" }

[[projects.concert.uploads]]
name = "channel-archive"
match = "True"
encoding = { format = "flac" }
filename = "{session}/{device}/{track}/{timestamp}.{extension}"
destination = "archive"
access = { from = "player" }

[[sources]]
kind = "path"
name = "recs"
path = "/Volumes/Recordings/recs"
exclude = [".DS_Store"]

[[sources]]
kind = "volume"
name = "camera-card"
uuid = "F0B5CF26-1C8C-4E33-B36D-4D4F84C2F685"
relative_path = "DCIM"
expected_name = "CAMERA"
```

Path sources are fixed local directories or network shares that macOS has
already mounted. Volume sources are found below `/Volumes` by volume UUID, not
by their display names. Find the UUID for a mounted card with:

```sh
diskutil info -plist /Volumes/CAMERA
```

With `discover_removable = true`, the default, baccy also examines newly
mounted removable or ejectable volumes that are not already configured. It
selects content only when either:

- the volume root contains a `DCIM` directory, identifying a camera card; or
- any directory contains a valid recs `recording.toml` or
  `session-record.jsonl`, identifying recs sessions.

For a camera volume, only recognized photo files below `DCIM` are backed up;
videos, sidecars, manuals, and unrelated files are ignored. Supported photo
families include JPEG, HEIC/HEIF, PNG, TIFF, DNG, and common camera RAW formats.
For a recs volume, every file inside each detected session directory is backed
up, preserving the portable session; unrelated files elsewhere on the volume
are ignored.

Other unconfigured removable drives are ignored. Automatically discovered
volumes use their filesystem UUID as part of the backup source identity, so a
renamed volume continues in the same destination. Volumes without a UUID and
the volume containing `backup_root` are ignored. Set
`discover_removable = false` to use configured sources only.

`include` and `exclude` are optional lists of path-match patterns. Sources use
`include = ["**"]` and no exclusions by default. Source names must be unique,
and neither a source nor the backup root may contain the other.

Set `verbose = true` to include unchanged files in each JSON summary. When the
installed service is running, it also sends macOS notifications when it
recognizes a backup disk or network machine and again when that backup pass
finishes. The current default includes unchanged individual results while
retaining the `unchanged` count.

## Project publication

Projects are the first directory below any recs source baccy has backed up.
Each `uploads` rule independently selects finalized audio segments, so one
recording can create several artifacts. Publication always reads the completed
copy in the backup root, never an active recorder or removable drive.

`match` is a restricted expression over `duration`, `main`, `device`,
`channels`, `track`, `format`, `player`, and `has_player`. It supports
comparisons, membership, `and`, `or`, and `not`, but no calls, attributes,
subscripts, arithmetic, or Python evaluation. The default `main` tracks are
the two highest numbered channels of the device with the greatest observed
channel number. If devices tie, rules referring to `main` are deferred.

Rules encode `source`, `flac`, or `mp3`. MP3 rules require `bitrate_kbps`.
Derived FLAC and MP3 files are written atomically to `artifacts/` under the
backup root, keyed by source content and the complete rule definition. The
cache is disposable: the permanent source backup remains authoritative.

`filename` accepts only `{project}`, `{session}`, `{device}`, `{track}`,
`{channels}`, `{timestamp}`, `{rule}`, and `{extension}`. It must render a
safe relative path. Baccy rejects every colliding target before it starts an
encoder or upload.

SSH destinations use the existing non-interactive SSH configuration and keys;
their static access profile may set `ssh_mode`. S3 destinations use boto3 and
the host's normal AWS credential chain. An optional `s3_acl` applies a canned
ACL. Baccy records an artifact identity in S3 object metadata and skips an
object with the same identity. Credentials never appear in the TOML or event
log.

`access = { from = "player" }` is accepted but currently deferred with
`player access metadata is missing`: current recs sessions do not snapshot the
player access profile needed to publish safely.

## Run once

Run one complete scan and exit:

```sh
uv run baccy backup
uv run baccy backup --config /path/to/baccy.toml
uv run baccy backup --dry-run
```

The first form needs no configuration file. It discovers qualifying removable
media and stores selected photo files or recs sessions on the main drive under
`~/baccy`.

The command prints a JSON summary. It exits nonzero when a configured source is
unavailable or a copy fails. A missing removable card or mounted share does not
delete or alter prior backups.

For recs sessions, baccy appends only newly completed lines from
`session-record.jsonl` after verifying its already backed-up prefix. WAV and
FLAC files named by an unfinished `file_started` record are deferred. They are
copied atomically once recs writes a matching `file_finished` record.

Use `-d` or `--dry-run` to print the same summary with `would_copy` and
`would_upload` results without creating the backup root, lock, event log,
artifact cache, temporary files, or network connections.
`baccy watch -d` repeatedly performs the same non-writing preview.

## Watch in the foreground

Run repeated scans in the current terminal:

```sh
uv run baccy watch
```

`SIGINT` and `SIGTERM` stop the watcher after its current copy operation. The
watcher calls the same one-pass backup engine as `baccy backup`; it is not a
second backup implementation.

Every 10 seconds, watch reads the local ARP table for newly visible systems. It
uses non-interactive SSH with host-key checking disabled. Baccy neither records
nor verifies host keys, so a host may change its key without interrupting
discovery, but it must still accept the user's existing SSH certificate before
it can qualify.
Each newly seen MAC address gets an immediate SSH attempt in parallel with the
other newly seen hosts and, for connection failures, one retry after two
seconds and another after four seconds.
Authentication rejections and hosts without a `~/recs` directory are not
retried until baccy restarts. A host that accepts SSH but does not yet have
`~/recs` is checked again on each later ARP scan. With `verbose = true`, baccy
notifies once per daemon session when it first recognizes an SSH-capable
machine. Multicast and broadcast ARP entries are ignored.
Qualifying `~/recs` directories are copied as network sources, with the catalog
skipping files whose remote size and modification time have not changed.

With `verbose = true`, the service log records each newly observed network
host, whether SSH failed or `~/recs` was absent, and recognized-source backup
start and completion. Watch it with:

```sh
tail -f ~/Library/Logs/baccy/baccy.log
```

When running as the installed service, baccy sends a macOS notification for a
new copy failure. It does not notify for unavailable configured sources,
unplugged removable media, or rejected network hosts. Repeated identical
failures are reported once until they recover or change.

## Start automatically after login

Install the per-user LaunchAgent:

```sh
uv run baccy service install
uv run baccy service status
```

The agent uses `launchd` with `RunAtLoad` and `KeepAlive`. Installation builds
an isolated release environment under `~/Library/Application Support/baccy/`,
so the service does not run code from this checkout. It starts after the user
logs in after a restart and restarts after an unexpected exit. It has the same
user access to mounted volumes and network shares as `baccy watch`.

```sh
uv run baccy service stop
uv run baccy service start
uv run baccy service restart
uv run baccy service uninstall
```

Run `uv run baccy service install` again to explicitly deploy a newer checkout.
`service restart` only restarts the installed release.

This is intentionally a per-user LaunchAgent, not a privileged LaunchDaemon.
It does not run before login. Pre-login backups would require a separate
system-wide deployment and credentials policy.

## recs sessions

baccy treats a recs session as ordinary portable files and never imports recs,
rewrites its files, or attempts finalization or migration. It preserves the
whole session-relative layout, so paths in `recording.toml` and
`session-record.jsonl` continue to be meaningful in a restored session.

Within a scan baccy copies TOML before JSONL, then copies other files. That
means `recording.toml`, `session-record.jsonl`, native MIDI, OSC, and key event
streams are backed up before large audio assets.

JSONL is handled as an append-only snapshot: baccy copies the complete prefix
that existed at the start of the copy, requires it to end on a newline, and
verifies that prefix again before committing it. This permits backing up an
active recs journal without waiting for recording to stop. TOML, audio, photos,
and other ordinary files must remain unchanged for `stability_seconds` before
they are copied.

## Backup layout and recovery

Current copies are stored below:

```text
BACKUP_ROOT/sources/SOURCE_NAME/RELATIVE_PATH
```

At the backup root:

- `events.jsonl` records copied, uploaded, failed, and deferred files.
  A deferred file is recorded once until it is successfully copied.
- `.lock` prevents concurrent backup passes against one backup root.

Each new file is copied to a temporary file beside its destination, flushed to
disk, and atomically renamed only after the source has passed its snapshot
checks. A partial copy is never promoted to the visible destination. When a
file changes, baccy atomically replaces the existing destination.

To restore a file, copy its current version from `sources/`.

## Version-one boundaries

baccy does not mount shares, connect to remote hosts, manage credentials, or
publish to a public server. Configure a mounted share as a path source instead.
It also does not preserve ownership or extended attributes in this version.

## License

`baccy` is licensed under the [MIT License](LICENSE).
