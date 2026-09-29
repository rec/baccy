# baccy issue inventory

Static review of the application, scripts, configuration, documentation, tests, and
the relevant `reccy` service API on 2026-09-29. Priorities describe potential
impact, not an implementation order. No live backup, daemon, S3, or SSH operation
was run. A scenario marked **risk** follows from the code but has not been
reproduced against a live service. This is an issue inventory, not a migration
plan.

Original issue numbers are retained for cross-reference.

## P1: reliability, concurrency, shutdown, and resource use

15. **Failed newly discovered network hosts are not visible in pass results.**
    [`baccy/network.py`](../baccy/network.py) now bounds SSH probes to eight
    workers and reports a previously discovered source that disappears from
    ARP as unavailable. A newly seen host whose SSH probe fails is still only
    logged when verbose mode is enabled. Decide whether it should produce an
    unavailable result without treating every unrelated ARP entry as a backup
    source.

22. **Service installation has no rollback or reliable restart identity.**
    [`baccy/application.py:54`](../baccy/application.py#L54) stops the old
    service before installing the new one, and a failed install leaves it
    stopped. [`baccy/cli.py:364`](../baccy/cli.py#L364) accepts any running
    daemon responding at the control socket, without checking that it is the
    newly installed release. Release directories from failed or superseded
    installations also accumulate. This is application-specific release
    handling around reccy's service controller, not a duplicate of the
    controller itself.

24. **Large inventories are repeatedly materialized.**
    [`baccy/scan.py:9`](../baccy/scan.py#L9) recursively builds a complete
    path list and candidate list; [`baccy/catalog.py:57`](../baccy/catalog.py#L57)
    rereads the entire append-only catalog at the start of passes;
    [`baccy/upload.py:75`](../baccy/upload.py#L75) walks all journals in both
    preflight and planning. Normal publication hashes every matching audio
    source in [`baccy/upload.py:389`](../baccy/upload.py#L389) before even
    checking its catalog record. These are plausible causes of long, silent
    scans and high I/O or memory use. Measure before choosing a streaming or
    indexing change.

25. **`list` still has no progress output for large inventories.**
    [`baccy/listing.py`](../baccy/listing.py) now checks only planned S3 keys
    and splits SSH requests into batches of 16 paths. It still holds all rows
    until completion, so a long run gives the user no indication of progress.
    Exceptionally long paths could also make one SSH batch exceed command-line
    limits. Report progress and bound batches by command length as well as count.

26. **The event log grows without bound.**
    [`baccy/catalog.py`](../baccy/catalog.py) now rejects corrupt interior
    records and warns about an incomplete final line. It still rereads the
    complete append-only log on each pass, and each append opens and fsyncs the
    file separately. Define a retention or compaction policy before it becomes
    too large.

## P2: user-facing semantics, maintainability, and tests

27. **Metadata-only sync cannot prove remote content is correct.**
    `sync` compares available size metadata and S3 identity where a catalog
    record exists. A same-size SSH replacement or an object without a prior
    catalog record can still look healthy. Normal publication can also skip a
    deleted remote object based solely on local catalog state. Make those
    guarantees explicit in CLI help and consider an opt-in verification mode.

33. **Some names and configuration surfaces mislead.**
    `list_uploaded` in [`baccy/listing.py:21`](../baccy/listing.py#L21) means
    "currently present files that the present rules could produce," not all
    files baccy ever uploaded. `SshDestination.url` is `HOST:PATH`, not an
    actual URL, and cannot express an IPv6 host with colons
    ([`baccy/models.py:60`](../baccy/models.py#L60)). `S3Destination.prefix`
    and `endpoint_url` exist in the model but cannot be supplied by the
    string-only TOML destination parser
    ([`baccy/models.py:105`](../baccy/models.py#L105)). Decide which of
    those surfaces are supported, then rename or document accordingly.

35. **The implementation has a few concentrated and repeated areas.**
    `baccy/upload.py` is 1,090 lines and handles journal interpretation, rule
    planning, HTML, transcoding, SSH, S3, and remote inventory. `baccy/cli.py`
    is 538 lines and handles parsing, service control, reporting, and daemon
    logging. These are the clearest split candidates when touched next. The
    identical SSH options and subprocess handling in `upload.py`,
    `listing.py`, and `server_test.py`, and separate failure-result builders in
    `copy.py` and `network.py`, are modest duplication. The tiny modules
    (`s3.py`, `sync.py`, `recs.py`, `notifications.py`) have cohesive ownership
    and do not merit inlining merely because they are short. `reccy` already
    owns service lifecycle, status, and RPC; baccy's release building,
    backup loop, and notifications are application policy, not a second
    implementation of those services.

36. **Failure-mode tests are sparse relative to the failure surface.**
    `test/test_rename.py` lacks injected delete and local rename failures.
    Watch tests now cover a pending trigger, a transient exception, and prompt
    stopping while idle, but not an interrupt during a transfer. Listing tests
    cover SSH batching and CLI remote failure, but not a partial failure after
    several successful batches. Upload tests cover same-content landing pages
    and encoder timeout, but not interruption during an S3 or SCP transfer.
    These are high-value additions; do not duplicate the broad axto fixture
    regression for each small failure case.

37. **The test layout has one oversized concentration, but not obvious excess.**
    `test/test_cli.py` is 814 lines of separate command behaviors and
    `test/test_upload.py` is 419 lines. Splitting by command or publication
    feature would improve navigation as tests grow. The large axto results
    fixture exercises realistic planning; its exact transfer snapshot is
    valuable and does not, by itself, show redundant testing. Smaller tests
    should cover failure branches rather than duplicate that snapshot.

## Additional work beyond the prompt

None.
