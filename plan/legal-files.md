# Web-safe upload paths

## Goal

Every path baccy publishes to an S3 bucket or SSH destination must be a
web-safe URL path.  Baccy will use `reccy.paths.legal_url_path` as the sole
normalization rule.

The local backup is archival data.  Its audio filenames and
`session-record.jsonl` paths remain unchanged.  Only the remote target names,
including links rendered into landing pages, are normalized.

This is a remote namespace migration.  It must neither delete local audio nor
rewrite recs session records.

## Target-path rule

1. Continue producing an artifact's current, relative target path from its
   upload rule.
2. Apply `legal_url_path` to that relative path.
3. Require the result to remain a non-empty relative POSIX path with no `..`
   component.
4. Use the normalized result for every remote operation, result record,
   remote-presence check, listing, and landing-page URL.

Do not apply the function to a destination specification.  In particular, the
S3 bucket name, S3 prefix, SSH host, and SSH destination root are configuration
rather than generated URL components.  Join the normalized relative target to
those values only after normalization.

The normalization must happen in one shared target-planning function, before
plans are used for collision detection or network I/O.  There must not be
separate S3 and SSH sanitizers.

## New uploads

Update artifact planning so both source-file uploads and derived uploads use
the normalized target.  This includes FLAC, MP3, and future encodings.

Landing-page plans use the same normalized artifact targets.  Their `index.html`
targets are normalized too, and the template receives URLs derived from those
targets.  Do not URL-quote the old filename after it has been normalized: the
link must name the actual remote object.

All existing target collision checks must operate on the pair of destination
identity and normalized target.  `legal_url_path` is many-to-one, so two
different source names can normalize to the same URL.  Such a collision is a
configuration/data error: report each colliding artifact as deferred and do no
upload for that target.

## Existing remote objects

Add a dedicated migration command after the new-write behavior is covered:

```sh
baccy relocate-urls
baccy relocate-urls --dry-run
baccy relocate-urls --yes
```

The exact command spelling can be settled with the CLI implementation, but it
must have this behavior:

1. Build both the legacy target and normalized target for every current upload
   plan.  Only plans whose names differ are candidates.
2. Group candidates by destination and reject normalized-target collisions
   before changing anything.
3. Without `--yes`, print the complete `old -> new` preview and ask for one
   confirmation.  `--dry-run` prints the same plan and performs no remote
   queries or writes.
4. For each candidate, cheaply inspect only the legacy and normalized remote
   names.  Do not download objects or calculate content hashes.
5. If only the legacy name exists, relocate it on the remote service.  S3 uses
   server-side copy followed by deletion only after copy succeeds.  SSH creates
   the new parent directory and uses a same-server rename.  The local backup
   and session record are never renamed.
6. If only the normalized name exists, report it as already migrated.  If both
   exist, stop with a conflict and keep both objects.  If neither exists,
   report it as absent and leave it for the next normal sync to upload.
7. Stop at the first remote failure.  Do not delete a legacy object when its
   replacement is uncertain.

The migration will emit timestamped structured records to the daemon log, one
record when an object starts and one terminal record for that object.  Its
interactive output is limited to the preview, confirmation, errors, and final
summary.

## Ordering

Implement and release in these stages:

1. Add a pure, tested target-normalization helper around
   `reccy.paths.legal_url_path`.
2. Apply it to all artifact and landing-page plans, collision validation,
   upload presence checks, event records, and `baccy list`.
3. Add regression fixtures with spaces and every character changed by
   `legal_url_path`.  Verify matching S3 keys, SSH paths, and HTML links.
4. Add the migration planner and test its no-network preview, collision,
   already-migrated, absent, S3 copy/delete, SSH rename, and failure behavior.
5. Run `relocate-urls --dry-run` against the production configuration, inspect
   every proposed mapping, then run it with `--yes` while the daemon is stopped.
6. Restart the daemon and run a normal sync.  Confirm that `baccy list` shows
   only normalized targets and that every landing-page link resolves.

## Verification and rollback

Before migration, save the dry-run mapping as an operator artifact.  It is the
rollback map from normalized target to legacy target.

For each relocation, verify remote metadata available without downloading
content: S3 object size and copy success, and SSH `stat` size after rename.
Preserve the legacy object on any uncertainty.  If an already migrated object
must be rolled back, use the saved mapping to perform the reverse server-side
copy/rename, again preserving the source until the destination is confirmed.

Unit tests must keep local filenames and session-record paths unchanged while
asserting normalized remote targets.  They must also demonstrate that a
collision prevents every remote mutation for that target.
