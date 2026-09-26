# Web-safe upload paths

## Goal

Every path baccy publishes to an S3 bucket or SSH destination must be a
web-safe URL path.  Baccy will use `reccy.paths.legal_url_path` as the sole
normalization rule.

The local backup is archival data.  Its audio filenames and
`session-record.jsonl` paths remain unchanged.  Only the remote target names,
including links rendered into landing pages, are normalized.

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

## Ordering

Implement and release in these stages:

1. Add a pure, tested target-normalization helper around
   `reccy.paths.legal_url_path`.
2. Apply it to all artifact and landing-page plans, collision validation,
   upload presence checks, event records, and `baccy list`.
3. Add regression fixtures with spaces and every character changed by
   `legal_url_path`.  Verify matching S3 keys, SSH paths, and HTML links.
4. Run a normal sync. Confirm that `baccy list` shows
   only normalized targets and that every landing-page link resolves.
