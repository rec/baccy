# Upload language plan

## Goal

Replace the current single upload policy per project with an ordered,
declarative set of rules. Each rule independently selects completed recs audio
segments, optionally transforms them, names the resulting object, assigns an
access policy, and sends it to a destination.

Rules are additive rather than first-match. The same source segment may produce
multiple artifacts, such as a 128 kbit/s MP3 for the main channels and a FLAC
archive for every channel.

Keep this feature downstream of permanent backup. Selection and encoding use
only the completed files and metadata in baccy's backup root, never a live recs
process or an original removable/network source.

## Terms

- A **segment** is one completed recs audio file described by matching
  `file_started` and `file_finished` journal records.
- A **device** is the recs `source` recorded for a segment.
- A **track** is a recorded file covering one or more `source_channels`.
- An **artifact** is the file produced by applying one upload rule to one
  segment. It may be the original bytes or a derived encoding.
- An **access profile** is a symbolic policy name. Destination drivers translate
  that name into their own permission mechanism.

Do not use “permissions” to mean both POSIX modes and S3 authorization directly.
The rule language selects an access profile; each destination defines what that
profile means.

## Default main channels

Define the default main channels as follows:

1. Determine the input-channel count for each device represented in the
   session.
2. Select the device with the largest input-channel count.
3. Select its two highest-numbered input channels. A one-channel device has one
   main channel.
4. A recorded track is a main track when its `source` is that device and its
   non-empty `source_channels` are wholly contained in the main-channel set.

For example, if an eight-channel device is the largest device, channels 7 and 8
are main. Tracks `[7]`, `[8]`, and `[7, 8]` qualify; tracks `[6, 7]` and
`[1, 2, 3, 4, 5, 6, 7, 8]` do not.

The authoritative device channel count should eventually be snapshotted by recs
into the session journal. Until that metadata exists, baccy can derive a width
from the highest positive `source_channels` value observed for each device,
which matches the current implementation. If two devices tie for largest, the
rule is ambiguous and should be reported as deferred rather than silently
choosing by device name. A later `main_channels` project setting will allow the
device and channel count to be specified explicitly.

## Proposed configuration

Use named destinations and access profiles so rules remain readable and shared
configuration is not repeated. Use one top-level `uploads` list for every
project.

```toml
[destinations.show_server]
kind = "ssh"
url = "user@example.org:/srv/shows"

[destinations.archive]
kind = "s3"
bucket = "show-recordings"
prefix = "upcoming-shows"

[access.show_listeners]
ssh_mode = "0644"

[[uploads]]
name = "main-mp3"
match = "main and duration > 120"
encoding = { format = "mp3", bitrate_kbps = 128 }
filename = "{timestamp}.{extension}"
destination = "show_server"
access = { profile = "show_listeners" }

[[uploads]]
name = "channel-archive"
match = "True"
encoding = { format = "flac" }
filename = "{session}/{device}/{track}/{timestamp}.{extension}"
destination = "archive"
access = { from = "player" }
```

This is a target shape, not a commitment to these exact field spellings. Before
implementation, encode it as frozen Pydantic models and use their generated
validation errors to refine the final TOML.

## Rule model

Each upload rule has five independent parts:

1. **Selection**
   - `match` is a restricted `simpleeval` expression evaluated once for every
     completed audio segment.
   - `main` exposes the default main-channel decision described above;
     `match = "main"` selects those tracks and `match = "True"` selects all
     completed tracks.
   - `duration` is `frame_count / sample_rate` in seconds. Preserve the stated
     operator: `duration > 120` is strictly greater than two minutes, not
     greater than or equal to two minutes.
   - Rules can combine facts without adding a new configuration field for each
     selector, for example `main and duration > 120` or
     `format in ['wav', 'flac'] and duration >= 60`.

2. **Encoding**
   - `format = "source"` uploads the original bytes.
   - `format = "flac"` passes an existing compatible FLAC through unchanged or
     losslessly encodes another supported source format.
   - `format = "mp3"` requires an explicit bitrate such as `bitrate_kbps = 128`.
   - Encoding writes to a temporary artifact, verifies successful completion,
     and atomically promotes it into a local artifact cache before upload.

3. **Naming**
   - Templates use a small fixed vocabulary rather than arbitrary Python format
     evaluation: `project`, `session`, `device`, `track`, `channels`,
     `timestamp`, `rule`, and `extension`.
   - `timestamp` is the segment start timestamp from the matching
     `file_started` journal record, rendered in a filesystem-safe canonical
     form. Do not extract it from a filename with an ad hoc regular expression.
   - Normalize every rendered name as a relative POSIX path and reject `..`, an
     absolute path, empty components, or collisions between selected artifacts.

4. **Destination**
   - SSH destinations retain the current non-interactive SSH/SCP behavior.
   - S3 destinations identify a bucket and optional key prefix; credentials are
     supplied by the normal host environment, never embedded in baccy TOML or
     the event log.
   - Destination-specific settings belong on the named destination, not on
     individual rules.

5. **Access**
   - `profile = "name"` selects a configured static access profile.
   - `from = "player"` resolves the player assigned to the segment's device and
     channels, then reads that player's symbolic access-profile name from the
     session metadata.
   - Missing or conflicting player assignments defer that artifact and record a
     clear reason. They must never fall back to broader access.

## Match expression safety

Use `simpleeval` 1.0.6 or newer as the expression parser and evaluator. Version
1.0.5 fixed a sandbox escape, so older releases are not acceptable. Add the
dependency in its own dependency commit when implementation begins.

Do not use `simpleeval` with its default feature set. Configure one shared,
locked-down evaluator that exposes only flat scalar and list values supplied by
baccy. Pre-validate the parsed AST and allow only:

- names and literal strings, numbers, booleans, `None`, lists, and tuples;
- comparisons: `==`, `!=`, `<`, `<=`, `>`, and `>=`;
- membership: `in` and `not in`;
- Boolean composition: `and`, `or`, and `not`;
- parentheses.

Disallow calls, attribute access, subscripting, comprehensions, arithmetic,
bitwise operators, conditional expressions, lambdas, and container literals
other than bounded lists and tuples. Pass `functions={}` and
`allowed_attrs={}` even though AST validation already excludes those forms.

Initially expose these names:

- `duration`: floating-point seconds derived from `frame_count / sample_rate`;
- `main`: whether the segment belongs wholly to the default main channels;
- `device`: recs source/device name;
- `channels`: ascending list of positive source channel numbers;
- `track`: recorded track name or an empty string;
- `format`: lowercase source audio format;
- `player`: stable assigned player identifier or `None`;
- `has_player`: whether the player assignment is complete and unambiguous.

Unknown names are configuration errors. Parse expressions once when loading the
configuration and reuse the parsed tree for each segment. The result does not
need to have Boolean type: normal Python truthiness decides whether the rule
matches.

Resource safety still needs explicit limits because a restricted expression can
consume excessive memory or time. Limit the expression text to 512 characters,
literal strings to 256 characters, and literal lists/tuples to 32 items. Set a
small AST-node limit, reject nesting deeper than a fixed bound, and retain
`simpleeval`'s own size protections. Since calls and arithmetic are absent,
there is no exponentiation or user-supplied function path.

## recs metadata boundary

Baccy should not query a live recs player database while publishing an old
session. Sessions need to be self-describing and reproducible.

The current recs header already snapshots channel-to-musician assignments as
`channel_musicians`, but it does not snapshot the corresponding musician/player
records or an upload access-profile name. Extend recs so a session header
contains the minimum immutable player information needed by publication:

- stable player identifier;
- the device/channel assignment already represented by `channel_musicians`;
- a symbolic upload access-profile name;
- optionally display/copyright names if they are needed in later templates.

Keep public keys and backend credentials out of the session unless a separate
recs design explicitly establishes that they belong there. Baccy needs the name
of an access policy, not secret material or a complete live database snapshot.

Until recs emits this metadata, `access = { from = "player" }` should validate as
supported syntax but defer matching artifacts with `player access metadata is
missing`. This allows the language and evaluator to land before the player
database integration without granting accidental access.

Device topology should also be snapshotted in the session header so the main
device is determined from its actual input-channel count rather than inferred
from whichever channels happened to record.

## Compilation and evaluation

Separate configuration parsing from runtime work:

1. Parse TOML into validated project, destination, access, selector, encoder,
   naming, and rule models.
2. Parse and validate every `match` expression once, rejecting unknown names or
   forbidden syntax before any session is scanned.
3. Read a journal into typed session facts: header, completed segments, device
   topology, player assignments, and timestamps.
4. Resolve `main` once per session.
5. Evaluate every rule against every completed segment and produce immutable
   artifact plans.
6. Validate target names, permission resolution, and collisions before encoding
   or network I/O.
7. Materialize required encodings into the local artifact cache.
8. Apply destination access policy and upload.
9. Record the result in `events.jsonl`.

The pure compilation and evaluation stages should not perform filesystem writes,
encoding, SSH, or S3 calls. This makes selector semantics and rule interactions
fully testable.

## Artifact identity and idempotency

The current upload catalog keys only on source path metadata. Derived artifacts
need a stronger identity. Key each upload by:

- source identity and source content hash;
- project and stable rule name;
- normalized encoding specification;
- rendered destination name;
- destination identity;
- resolved access-profile name.

Changing bitrate, filename template, destination, or access profile must produce
a new artifact/upload decision. An unchanged rule and unchanged source must not
encode or upload again.

Store derived files below a non-user-facing artifact cache in the backup root,
addressed by the complete artifact identity. The cache is disposable because
the permanent backup remains authoritative, but eviction is a later policy and
must not be mixed into the first implementation.

## Failure and safety rules

- Only `file_finished` audio participates. Open WAV/FLAC files are never encoded
  or uploaded.
- A missing source backup, unsupported input format, encoder failure, ambiguous
  main device, missing player access metadata, name collision, or destination
  failure affects that artifact and does not block unrelated rules.
- Derived artifacts are never written into the permanent source-copy tree.
- Dry run performs selection, naming, and permission resolution and reports the
  intended encoding/upload, but performs no encoding, cache write, directory
  creation, SSH, or S3 operation.
- Event records include the rule, source, artifact identity, encoding,
  destination, target name, resolved access profile, result, and failure or
  deferral reason. Do not record credentials.

## Implementation phases

1. **Language and evaluator**
   - Add frozen Pydantic models for named destinations, access profiles, and
     upload rules.
   - Add `simpleeval>=1.0.6` in a separate dependency commit and wrap it with
     the restricted AST and resource limits above.
   - Add the canonical top-level rules form.
   - Build typed session facts and implement `main`, match evaluation, naming,
     collision detection, and additive rule evaluation.
   - Produce dry-run artifact plans without encoding or uploading.

2. **Artifact pipeline and SSH**
   - Add the local artifact cache and transform identity.
   - Choose and document the encoder executable before adding it as an external
     runtime requirement.
   - Implement FLAC and 128 kbit/s MP3 materialization and route SSH uploads
     through the new destination model.
   - Apply static SSH access profiles after upload.

3. **S3 destination**
   - Select an S3 client implementation explicitly rather than silently adding
     a dependency.
   - Implement bucket/prefix upload, existing-object checks, retry behavior, and
     backend-specific access-profile application.

4. **recs player and device metadata**
   - Extend recs session headers with device channel counts and the minimal
     player access snapshot.
   - Resolve `access = { from = "player" }` from that snapshot.
   - Add later configuration for overriding the default main device and number
     of main channels.

5. **Migration and documentation**
   - Convert existing project upload configuration to named destinations plus
     rules in one intentional migration.
   - Remove the old selection/upload implementation rather than retaining two
     policy engines.
   - Document exact TOML, dry-run output, credential discovery, encoder setup,
     access semantics, and recovery from partial failures.

## Tests

Cover at least:

- an eight-channel device selects channels 7 and 8 as main;
- mono and stereo devices select the available highest channels;
- tied largest devices defer `main` rules;
- a track spanning non-main and main channels is not a main track;
- exactly 120 seconds fails `match = "duration > 120"`;
- `and`, `or`, `not`, membership, parentheses, and normal truthiness work;
- unknown names and every forbidden AST form fail during configuration loading;
- calls, attributes, subscripts, comprehensions, and oversized expressions or
  literals are rejected;
- one segment matches both additive example rules;
- an MP3 rule plans 128 kbit/s output and renders only the timestamp filename;
- FLAC input passes through the FLAC rule while WAV input plans a lossless
  transform;
- two artifacts rendering the same target path are rejected before network I/O;
- missing or conflicting player metadata defers rather than broadens access;
- rule or encoding changes invalidate the prior artifact identity;
- unchanged artifacts skip encoding and upload;
- dry run performs no writes or external commands;
- SSH and S3 drivers receive the same resolved artifact/access model;
- credentials and secret values never appear in `events.jsonl`.
