# Restore imported session audio metadata

## Problem

`plan/missing-files.txt` identifies 56 FLAC files in one imported session that
are not described by that session's top-level `session-record.jsonl`. The
top-level file is an import stub. The completed audio lifecycle records remain
in `evidence/session-record-v3.jsonl`, but its `path` fields refer to the
pre-import session directory rather than the canonical `audio/` directory.

As a result, upload planning cannot select those files even though it has their
duration, device, track, and channel metadata.

## Desired behavior

For an imported session, baccy uses completed audio lifecycle records from the
top-level journal and, when present, the evidence journal. It resolves a
recorded audio path as follows:

1. Use the recorded path when it exists inside the canonical session.
2. Otherwise, look for a file with the same basename in the session's `audio/`
   directory.
3. Use that basename fallback only when it names exactly one regular file.
4. Defer the artifact with a clear event reason when neither path exists or the
   basename is ambiguous.

The evidence record continues to supply the segment metadata. The resolved
canonical path is used for hashing, FLAC reuse, and upload materialization.

## Implementation

1. Change session parsing to read the top-level journal and optional
   `evidence/session-record-v3.jsonl`.
2. Merge completed audio lifecycle records by their resolved canonical path.
   Prefer the top-level record if both journals describe the same file.
3. Validate evidence paths exactly as current journal paths are validated.
   Never allow an evidence path to escape the session root.
4. Resolve old evidence paths only through the unique basename rule above.
   Do not infer a file from timestamps, channel numbers, or fuzzy matching.
5. Preserve existing behavior for sessions without evidence journals and for
   records whose existing path already resolves normally.
6. Emit a deferred upload result and a single event for missing or ambiguous
   evidence-path resolution.

## Tests

1. Add a session fixture with a top-level import-stub journal, an evidence
   journal containing a completed FLAC record whose path has the old session
   prefix, and the corresponding canonical `audio/` file. Confirm `sync
   --dry-run` plans the configured FLAC and, when eligible, MP3 uploads.
2. Add fixtures for a missing basename and duplicate basename. Confirm both
   are deferred and neither produces an upload request.
3. Keep the current direct-path journal fixture as a regression test.
4. Extend the axto dry-run test with the evidence-journal fixture so the
   expected upload list includes the repaired imported-session transfer.

## Additional work beyond the prompt

None.
