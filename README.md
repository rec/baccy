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
~/Backups/baccy
```

This makes `baccy backup`, `baccy watch`, and a normally installed service
useful without creating a configuration file. A missing path supplied
explicitly with `--config` remains an error.

Pass `--config PATH` to any backup, watch, or service-install command to use a
different file.

```toml
backup_root = "/Volumes/Backups/baccy"
discover_removable = true
poll_seconds = 60
stability_seconds = 60

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

## Run once

Run one complete scan and exit:

```sh
uv run baccy backup
uv run baccy backup --config /path/to/baccy.toml
uv run baccy backup --dry-run
```

The first form needs no configuration file. It discovers qualifying removable
media and stores selected photo files or recs sessions on the main drive under
`~/Backups/baccy`.

The command prints a JSON summary. It exits nonzero when a configured source is
unavailable or a copy fails. A missing removable card or mounted share does not
delete or alter prior backups.

Use `-d` or `--dry-run` to print the same summary with `would_copy` results
without creating the backup root, lock, catalog, temporary files, or versions.
`baccy watch -d` repeatedly performs the same non-writing preview.

## Watch in the foreground

Run repeated scans in the current terminal:

```sh
uv run baccy watch
```

`SIGINT` and `SIGTERM` stop the watcher after its current copy operation. The
watcher calls the same one-pass backup engine as `baccy backup`; it is not a
second backup implementation.

## Start automatically after login

Install the per-user LaunchAgent:

```sh
uv run baccy service install
uv run baccy service status
```

The agent uses `launchd` with `RunAtLoad` and `KeepAlive`. It starts after the
user logs in after a restart and restarts after an unexpected exit. It has the
same user access to mounted volumes and network shares as `baccy watch`.

```sh
uv run baccy service stop
uv run baccy service start
uv run baccy service restart
uv run baccy service uninstall
```

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

Internal data lives under `BACKUP_ROOT/.baccy/`:

- `catalog.jsonl` records successful copies and failures.
- `versions/` contains the previous bytes of files that were later replaced.
- `lock` prevents concurrent backup passes against one backup root.

Each new file is copied to a temporary file beside its destination, flushed to
disk, and atomically renamed only after the source has passed its snapshot
checks. A partial copy is never promoted to the visible destination. When a
file changes, baccy hard-links the existing destination into `versions/` before
the atomic replacement, preserving it without a second large copy.

To restore a current file, copy it from `sources/`. To restore a previous
version, select the corresponding file under `.baccy/versions/` and copy it
back to the desired path.

## Version-one boundaries

baccy does not mount shares, connect to remote hosts, manage credentials, or
publish to a public server. Configure a mounted share as a path source instead.
It also does not preserve ownership or extended attributes in this version.

## License

`baccy` is licensed under the [MIT License](LICENSE).
