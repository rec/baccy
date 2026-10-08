# Bringing openLoop into baccy

## Scope and evidence

Inspected `openLoop/source/` and `openLoop/target/` on 2026-10-08 using
directory entries and filesystem metadata only. No file contents, audio headers,
scripts, project files, hashes, or extended-attribute values were read. Nothing
in the collection was changed. Repository implementation files were not read
either during that inventory. Implementation review and the limited WAV header
inspection described below were subsequently authorized and performed.

The user subsequently identified the Audacity projects as copies and deleted
them, with a separate backup retained. The counts below were refreshed using
filesystem metadata only after that deletion. The Audacity projects are no
longer part of this import.

### Source WAV header sample

Read headers from three source WAVs, seeking past sample data without decoding
or copying audio. All three have PCM `fmt ` and `data` chunks only within their
declared RIFF boundaries, with no descriptive metadata chunks:

| Source path | Channels | Sample rate | Bits/sample | Declared frames |
| --- | ---: | ---: | ---: | ---: |
| `2002/03/02/02032-1.wav` | 2 | 44,100 | 16 | 10,000,000 |
| `2006/02/10/2006-02-10.wav` | 2 | 48,000 | 24 | 147,147,776 |
| `2007/08/30/2007-07-30 open loop.wav` | 2 | 44,100 | 24 | 183,656,192 |

The 2006 WAV has 44 trailing bytes outside its declared RIFF boundary. They
are not a declared metadata chunk. Validate that file before import rather than
assuming the entire physical file consists of declared sample data.

Use filenames and the agreed corrections for descriptive information; do not
plan to recover titles, dates, participants, or devices from these WAVs.
Technical header information remains necessary for importing audio. This
sample does not establish metadata absence or valid decoding for every file.

### Execution constraint

Never directly move, copy, delete, or convert collection files. Write a script
for the user to review and run for every such operation. The agent must not run
that script against the collection. Keep originals unchanged and distinguish
read-only proposal generation from any eventual execution script.

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
calendar date where present. These are recordings of shows made directly to
CD, often lasting four hours or more. Numeric suffixes identify successive
discs from the same session, not takes or independent shows. Import one session
per show date, retaining each disc as a separate audio file in numeric order
(1, 2, 10, not lexical order 1, 10, 2). Preserve disc boundaries; do not infer
continuous coverage, gap lengths, or exact disc start times.
Keep the complete original basename and relative path as provenance. Preserve
labels such as `XX`, `last`, `final`, `remix`, `2b`, and `3-s`; do not infer their
meaning, collapse variants, or deduplicate by name or size. Non-numeric labels
are not evidence of edits or alternate mixes. Preserve them without asking the
user to reconstruct their meaning from twenty-year-old memories. Leave their
disc order unspecified unless it can be established from evidence.

Do not derive recording time from filesystem timestamps. Do not invent
participants, locations, source devices, or an exact recording time. Missing
information stays unknown. The approved convention is one session per known
date, with midnight explicitly recorded as a synthetic unknown-time placeholder
in metadata. It is not the factual show or disc start time.

### Agreed date correction

Treat the two formerly unknown recordings as the missing discs 1 and 2 from
Saturday, May 3, 2003, alongside the existing disc 3. This is a user-approved
assumption, not a conclusion established by inspecting audio:

| Original source path | Import date | Corrected recording basename |
| --- | --- | --- |
| `unknown/xx-05-02-1.Sd2f` | 2003-05-03 | `2003-05-03-1.Sd2f` |
| `unknown/xx-05-02-2.Sd2f` | 2003-05-03 | `2003-05-03-2.Sd2f` |

Apply this mapping in the eventual import proposal, preserving the original
paths as provenance. These files remain among the 21 unconverted sources.
This plan update does not rename the originals or create converted files.

### Saved review results

- `2007/02/open loop!!!.wav` is deferred and excluded from session proposals.
  The user suspects duplication, but that remains unverified; do not delete it.
- `2007/08/30/2007-07-30 open loop.wav` has conflicting directory and filename
  dates. The user confirmed **2007-07-30**, which overrides the directory date.
- Compact or unusual names can use their unambiguous date-directory evidence;
  preserve their original names. Their spelling is not itself a user decision.

The first two decisions are persisted in `plan/openLoop-decisions.json`, keyed
by collection-relative path. A date confirms the session date; `null` explicitly
defers the file. Planning and review both read this file. Deferral is not deletion
and does not assert duplication.

The only interactive questions are unresolved or conflicting dates. Do not ask
whether a recording is original, edited, duplicated, truncated, or valid based
only on a suffix or header. Determine technical questions through investigation
and report evidence separately. In particular, the 10,000,000-frame WAV is a
technical anomaly, not a request for the user to remember how it was made.

## Implementation sequence

### Started: model review and read-only proposal

Reviewed `baccy/importer.py`, `baccy/upload_plan.py`, the shared Project model,
and recs' session records and `recording/baccy_import.py`.

- baccy's importer discovers `session-record.jsonl`; loose WAVs cannot be
  imported by simply pointing the existing command at this collection.
- The shared Project model has descriptive fields, but no session/date policy.
- recs' session header requires `started_at`, with free-form metadata available
  to record provenance and uncertainty. Its specialized existing baccy importer
  recognizes only `totm` and `oderg in duo`; it is not a generic loose-audio
  importer and also writes staging data. Do not run it on this collection.
- baccy's audio upload planning expects paired lifecycle records and technical
  fields such as frame count, channels, sample rate, and timestamp. Inspect
  compatibility with the current recs record format before producing journals;
  do not introduce an independently invented session schema.
- The current MP3 target uses the audio basename (or the portion after its last
  ` + `), replacing its suffix with `.mp3`, under the project name. Preserving
  these date-and-disc basenames therefore preserves separate listening exports
  without changing the existing naming rule.

Added `scripts/plan_openloop.py`, a proposal generator that never changes media:

```sh
uv run python scripts/plan_openloop.py openLoop
```

It now displays only review/decision items as concise plain text. Each item
starts with its full path, followed by useful information that is not already
in that path. Automatic source/target pairings and the approved May 3 correction
do not require review and are omitted. Sizes, provenance paths, redundant dates,
and empty fields are not displayed.

For a step-by-step review:

```sh
uv run python scripts/plan_openloop.py openLoop --interactive
```

It asks only for unresolved dates. Enter a date, leave it blank to defer, or
enter `q` to end the review. Supplied dates are printed in answer order at the
end. Each valid answer or deferral is saved immediately to the decisions file
using an atomic replacement, so completed answers survive cancellation and
interruption. Saved dates override inferred dates, and deferred files are not
asked about again or included in sessions. Dated recordings are included in
the proposal without an inclusion/duplicate/export question. To revisit a
decision, edit or remove its entry in the decisions file.

Use `--decisions PATH` to select the review state file; its default is the
repository's `plan/openLoop-decisions.json`. Noninteractive review never writes
it. Audio is unchanged in every mode.

To see grouped show sessions using the saved decisions:

```sh
uv run python scripts/plan_openloop.py openLoop --sessions
```

This lists session dates and their ordered disc/file paths, with the deferred
file omitted. It does not execute an import or inspect audio samples.

For review WAVs it reads headers, not samples, to derive duration and skips
files shorter than ten seconds. Exactly ten seconds remains eligible. WAV header
errors and the suspicious 10,000,000-frame count are reported separately on
stderr as technical concerns, never as memory-based questions. File size alone
cannot reliably establish duration for unknown and compressed formats; that
uncertainty does not trigger a user question. No media files are moved, copied,
deleted, converted, or written; only interactive review state is saved.
The script has not been run against the collection by the agent.

`plan_sessions` groups known-date proposals into show sessions and orders their
numbered discs, including the approved corrected discs and the saved July 30
date. Unresolved and deferred files remain outside these groups.

This starts steps 1 and 2 below; grouping and the date-only convention are now
settled, but execution paths and journals are not implemented. Do not mistake
these proposals for executable transfer instructions. Next, extend
the proposal with intended paths and collision checks, then provide a separately
reviewable execution script that uses the existing session/import mechanisms.

1. Review the existing importer, session model, project definitions, and upload
   rules. Reuse their intended mechanisms. Determine whether importing legacy
   recordings needs a model change; obtain approval before an architectural
   change. Do not fabricate a contemporary recording journal.
2. Produce a deterministic metadata-only import proposal for the 100 target
   WAVs. Show original path, proposed project/session, preserved title, and
   intended local and remote paths. Detect collisions before writing anything.
   Keep the collection out of Git.
3. After permission to inspect the selected batch, validate it with the existing
   audio tooling. Obtain actual frame counts, sample rates, and channels from
   audio inspection, never from file size or the 44-byte difference. Use existing
   lossless import/conversion facilities if suitable, preserving originals.
   Report invalid files individually rather than silently omitting them.
4. Provide a script for the user to import a small representative batch first:
   several files on one date,
   `final`/`remix` variants, and an unusual basename. Review the session records,
   dry-run upload destinations, and generated page before enabling transfers.
5. Provide the remaining validated target WAV import as a user-run script.
   Verify that each selected input
   maps to exactly one intended imported recording and that repeating the import
   does not create duplicates or overwrite a different disc.
6. Separately classify the 21 unmatched `.Sd2f` files and other source exports.
   With permission, establish which can be decoded, which are distinct works,
   and how they relate to the show/disc inventory. Preserve available macOS
   metadata when archiving originals; establish decoding requirements before choosing
   an archive/copy method. Apply the agreed May 3, 2003 mapping to discs 1 and 2;
   resolve remaining ambiguous dates with the user.
7. Keep unknown-format assets intact as archive material.
   Decide separately whether baccy's current rules can back up these assets;
   do not force them into its recording model or promise coverage it lacks.

## Uploads and publication

Review configured rules before enabling this project. Preserve date-and-disc
basenames so the existing MP3 rule produces distinct exports such as
`openLoop/2003-05-03-1.mp3`. Check collisions after the rule's ` + ` stripping
and URL sanitation; do not assign every disc the same synthetic-timestamp
basename. Retain one listening file per disc, in session order, rather than
silently concatenating a four-hour show or selecting a single disc as a master.

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
- Approved: one session per known date, with midnight explicitly identified in
  metadata as an unknown-time placeholder, not a factual recording time.
- Settled: numbered files are successive discs of the same show session.
  Preserve disc identity and numeric order in local records and listening pages.
- Confirm backup destinations and public availability. Recommendation: private
  backup first, public exports only after reviewing the inventory.
- Authorize later audio/header inspection and import explicitly. This plan does
  not authorize executing transfers, decoding files, or editing the collection.
  The three source WAV header samples above were specifically authorized.

## Verification for the eventual implementation

Test date extraction, the agreed May 3 correction with original-path provenance,
unknown dates, conflicting dates, variant retention,
sanitized-name collisions, multi-disc export naming, numeric disc ordering, and
repeat-import behavior. Reuse existing tests rather than adding a parallel import path.
Audio regression fixtures should be WAV files at 48,000 samples per second,
at least one second long. Review a dry-run mapping and a small completed batch
before the full import. The proposal script has focused inventory and interactive
review tests, including the approved date correction, unresolved dates, short
WAV filtering, round frame counts, saved date/deferral reuse, and cancellation
after an answer has been saved. Generated WAV fixtures use
48,000 samples per second. Tests do not modify actual collection audio.

## Additional work beyond the prompt

None. Header inspection, model review, and the read-only proposal script are
within the request to start implementation. No collection files were moved,
copied, deleted, converted, or otherwise changed.
