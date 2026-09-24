# One-time imported-session metadata repair

## Problem

`plan/missing-files.txt` identifies 56 FLAC files in one imported session that
are not described by that session's top-level `session-record.jsonl`. The
top-level file is an import stub. The completed audio lifecycle records remain
in `evidence/session-record-v3.jsonl`, but its `path` fields refer to the
pre-import session directory rather than the canonical `audio/` directory.

As a result, upload planning cannot select those files even though their
duration, device, track, and channel metadata still exist.

## Desired behavior

This is a one-time repair of the affected session data. It is not a recurring
upload behavior and must not add migration-specific code to baccy.

The repair program uses completed audio lifecycle records from the evidence
journal and resolves a recorded audio path as follows:

1. Use the recorded path when it exists inside the canonical session.
2. Otherwise, look for a file with the same basename in the session's `audio/`
   directory.
3. Use that basename fallback only when it names exactly one regular file.
4. Defer the artifact with a clear event reason when neither path exists or the
   basename is ambiguous.

The evidence record supplies the segment metadata. The repair writes a new
canonical session journal with paths that point at the session's `audio/`
directory. Once that journal is in place, ordinary baccy code can plan uploads
without special cases.

## Implementation

1. Write a standalone, uncommitted repair program outside the baccy repository.
2. Read the affected session's top-level and evidence journals.
3. Validate evidence paths exactly as current journal paths are validated.
   Never allow an evidence path to escape the session root.
4. Resolve old evidence paths only through the unique basename rule above.
   Do not infer a file from timestamps, channel numbers, or fuzzy matching.
5. Produce a proposed canonical journal and a report of every repaired,
   missing, or ambiguous record before modifying the session.
6. After review, replace the top-level import-stub journal with the canonical
   journal, retaining the original journal and evidence files as backup
   artifacts beside it.
7. Run `baccy --dry-run sync` to verify that the repaired files now produce
   transfer requests.

## Tests

1. Run the repair program in report-only mode and inspect its proposed journal
   and unresolved-file report.
2. Confirm every listed path resolves uniquely before applying the repair.
3. Run `baccy --dry-run sync` after the repair and inspect the resulting
   transfer requests.

## Additional work beyond the prompt

None.
