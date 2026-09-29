# baccy issue inventory

Static review of the application, scripts, configuration, documentation, tests, and
the relevant `reccy` service API on 2026-09-29. Priorities describe potential
impact, not an implementation order. No live backup, daemon, S3, or SSH operation
was run. A scenario marked **risk** follows from the code but has not been
reproduced against a live service. This is an issue inventory, not a migration
plan.

Original issue numbers are retained for cross-reference.

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
