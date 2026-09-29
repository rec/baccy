# baccy issue inventory

Static review of the application, scripts, configuration, documentation, tests, and
the relevant `reccy` service API on 2026-09-29. Priorities describe potential
impact, not an implementation order. No live backup, daemon, S3, or SSH operation
was run. A scenario marked **risk** follows from the code but has not been
reproduced against a live service. This is an issue inventory, not a migration
plan.

Original issue numbers are retained for cross-reference.

## P2: user-facing semantics, maintainability, and tests

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
