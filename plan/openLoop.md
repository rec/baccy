# Bringing openLoop into baccy

## Scope and evidence

Inspected `openLoop/source/` and `openLoop/target/` on 2026-10-08 using
directory entries and filesystem metadata only. No file contents, audio headers,
scripts, project files, hashes, or extended-attribute values were read. Nothing
in the collection was changed. Repository implementation files were not read
either, so implementation details below require a later code review.

The user subsequently identified the Audacity projects as copies and deleted
them, with a separate backup retained. The counts below were refreshed using
filesystem metadata only after that deletion. The Audacity projects are no
longer part of this import.

## Is target a subset of source?

Not literally: its audio filenames have a different suffix. However, **every
target audio file has a corresponding source file**, with the same directory
and filename stem:

| Collection | Regular files | Audio/conversion observations | Total bytes |
| --- | ---: | --- | ---: |
| source | 166 | 121 `.Sd2f` and other exports | 100,904,894,478 |
| target | 106 | 100 `.wav` and six `.DS_Store` files | 74,339,646,312 |

Every one of the 100 target WAVs is exactly 44 bytes larger than its matching
source `.Sd2f`. For example:

```text
source/2002/02/23/2002-02-23-1.Sd2f   845882688 bytes
target/2002/02/23/2002-02-23-1.wav    845882732 bytes
```

This is consistent with a conversion adding a small header. It does not prove
identical samples, valid decoding, channel count, sample rate, or bit depth.
Treat target as a probable converted subset, not as grounds to delete originals.
The six identical relative paths across the trees are `.DS_Store` files, not
audio. No content identity is asserted for those either.

## What remains outside target

There are 21 source `.Sd2f` files without a corresponding target WAV:

```text
2003/08/06/2003-08-06-3.Sd2f
2003/08/09/2003-08-09-1.Sd2f
2003/08/09/2003-08-09-2.Sd2f
2003/08/09/2003-08-09-3.Sd2f
2003/08/23/2003-08-23-2.Sd2f
2003/08/23/2003-08-23-3.Sd2f
2003/08/30/2003-08-30-1.Sd2f
2003/08/30/2003-08-30-2.Sd2f
2003/09/06/2003-09-06-1.Sd2f
2003/09/06/2003-09-06-2.Sd2f
2003/09/13/2003-09-13-1.Sd2f
2003/09/13/2003-09-13-2.Sd2f
2003/09/19/2003-09-19-1.Sd2f
2003/09/19/2003-09-19-2.Sd2f
2003/09/19/2003-09-19-3.Sd2f
2003/09/20/2003-09-20-1.Sd2f.not.Sd2f
2003/09/20/2003-09-20-2.Sd2f
2003/09/20/20030920-2.Sd2f
2003/11/18/2003-11-18-4.Sd2f
unknown/xx-05-02-1.Sd2f
unknown/xx-05-02-2.Sd2f
```

Source now also contains six WAVs, two AIFs, one AIFF, three MP3s,
two `.image` files, and the extensionless `2003/02/22/030222-4`. Suffixes
are classification hints, not verified formats. Do not assume these are
duplicates or that `.image` files are audio.

The refreshed inventory contains no `.au`, `.aup`, `.bak`, or `.ogg` files.
Do not attempt to recover or import the deleted Audacity projects.
Exclude Finder metadata, the two shell scripts, and the editor-lock symlink
`2003/.#rename2.sh` from recording selection; leave them untouched in the archive.

## Recommended representation

Use one recording project, provisionally named `openLoop`. Start with the 100
target WAVs because they provide a clearly mapped, manageable first batch.
Retain source as the original archive and record the source/target relationship
without claiming verified equivalence.

Use filenames as the primary evidence. Group recordings by an unambiguous
calendar date where present, with multiple takes/files retained in that group.
Keep the complete original basename and relative path as provenance. Preserve
labels such as `XX`, `last`, `final`, `remix`, `2b`, and `3-s`; do not infer their
meaning, collapse variants, or deduplicate by name or size.

Do not derive recording time from filesystem timestamps. Do not invent
participants, locations, source devices, or an exact recording time. Missing
information stays unknown. Before implementation, inspect the current recs
project/session model and baccy importer to determine how date-only recordings
can be represented. If a storage timestamp is unavoidable, agree on an explicit
convention that distinguishes it from a known recording time before writing
session records.

### Agreed date correction

Treat the two formerly unknown recordings as the missing takes 1 and 2 from
Saturday, May 3, 2003, alongside the existing take 3. This is a user-approved
assumption, not a conclusion established by inspecting audio:

| Original source path | Import date | Corrected recording basename |
| --- | --- | --- |
| `unknown/xx-05-02-1.Sd2f` | 2003-05-03 | `2003-05-03-1.Sd2f` |
| `unknown/xx-05-02-2.Sd2f` | 2003-05-03 | `2003-05-03-2.Sd2f` |

Apply this mapping in the eventual import proposal, preserving the original
paths as provenance. These files remain among the 21 unconverted sources.
This plan update does not rename the originals or create converted files.

Some other dates need manual decisions:

- `2007/02/open loop!!!.wav` has no day in its filename or parent path.
- `2007/08/30/2007-07-30 open loop.wav` has conflicting directory and filename
  dates. Prefer the filename as a proposal, but require confirmation.
- Compact or unusual names such as `02032-1.wav`, `030222-4`, and
  `2003-09-20-1.Sd2f.not.Sd2f` require classification, not silent normalization.

## Implementation sequence

1. Review the existing importer, session model, project definitions, and upload
   rules. Reuse their intended mechanisms. Determine whether importing legacy
   recordings needs a model change; obtain approval before an architectural
   change. Do not fabricate a contemporary recording journal.
2. Produce a deterministic metadata-only import proposal for the 100 target
   WAVs. Show original path, proposed project/session, preserved title, and
   intended local and remote paths. Detect collisions before writing anything.
   Keep the collection out of Git.
3. After permission to read audio, validate the first batch with the existing
   audio tooling. Obtain actual frame counts, sample rates, and channels from
   audio inspection, never from file size or the 44-byte difference. Use existing
   lossless import/conversion facilities if suitable, preserving originals.
   Report invalid files individually rather than silently omitting them.
4. Import a small representative batch first: several files on one date,
   `final`/`remix` variants, and an unusual basename. Review the session records,
   dry-run upload destinations, and generated page before enabling transfers.
5. Import the remaining validated target WAVs. Verify that each selected input
   maps to exactly one intended imported recording and that repeating the import
   does not create duplicates or overwrite a different take.
6. Separately classify the 21 unmatched `.Sd2f` files and other source exports.
   With permission, establish which can be decoded, which are distinct works,
   and which are alternate exports. Preserve available macOS metadata when
   archiving originals; establish any decoding requirements before choosing
   an archive/copy method. Apply the agreed May 3, 2003 mapping to takes 1 and 2;
   resolve remaining ambiguous dates with the user.
7. Keep unknown-format assets intact as archive material.
   Decide separately whether baccy's current rules can back up these assets;
   do not force them into its recording model or promise coverage it lacks.

## Uploads and publication

Review configured rules before enabling this project. Existing MP3 naming based
only on a session timestamp may collide when a legacy date contains several
independent recordings. Preserve each take in the import proposal and settle
the intended listening-export grouping/naming before uploading. Do not silently
choose a master or concatenate recordings.

Apply `reccy.paths.legal_url_path` through the existing destination machinery to
SSH paths and object paths inside S3 buckets. Check sanitized-path collisions
across the entire batch. Keep original names as provenance and readable link
labels even when remote paths differ.

Backups and public listening copies are separate decisions. Confirm which
recordings may be public before generating MP3s and landing pages. Use existing
bandwidth limits, temporary MP3 handling,
timestamped structured logging, and per-file progress behavior.

The current trees total about 175.24 GB (decimal), before any new copies or
conversions. Check free space before import and allow for the actual import
strategy and temporary files; do not assume a compression ratio.

## Decisions before importing

- Confirm the project name `openLoop` and whether filenames should remain the
  recording titles. Recommendation: yes to both initially.
- Agree on date-only/unknown-date representation after reviewing the current
  model. Recommendation: preserve uncertainty, never present midnight as fact.
- Decide whether multiple takes belong to one date-based session or independent
  sessions, and how MP3 names distinguish them. Recommendation: preserve date
  grouping and individual take identity if the existing model supports it.
- Confirm backup destinations and public availability. Recommendation: private
  backup first, public exports only after reviewing the inventory.
- Authorize later audio/header inspection and import explicitly. This plan does
  not authorize executing transfers, decoding files, or editing the collection.

## Verification for the eventual implementation

Test date extraction, the agreed May 3 correction with original-path provenance,
unknown dates, conflicting dates, variant retention,
sanitized-name collisions, multiple-take export naming, and repeat-import
behavior. Reuse existing tests rather than adding a parallel import path.
Audio regression fixtures should be WAV files at 48,000 samples per second,
at least one second long. Review a dry-run mapping and a small completed batch
before the full import. No application execution or tests are needed for this
documentation-only change.

## Additional work beyond the prompt

None. The steps above are proposals for later approval, not actions performed
as part of this inventory and plan.
