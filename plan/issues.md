# baccy issue inventory

Static review of the application, scripts, configuration, documentation, tests, and
the relevant `reccy` service API on 2026-09-29. Priorities describe potential
impact, not an implementation order. No live backup, daemon, S3, or SSH operation
was run. A scenario marked **risk** follows from the code but has not been
reproduced against a live service. This is an issue inventory, not a migration
plan.

Original issue numbers are retained for cross-reference.

## P1: reliability, concurrency, shutdown, and resource use

8. **A daemon sync request can be lost.**
   [`baccy/watch.py:12`](../baccy/watch.py#L12) waits on `trigger`, then clears
   it. A request arriving after the wait returns and before the clear is
   erased. The RPC reply says `scheduled`, so the caller has no way to detect
   this race. Consume requests without a wait/clear gap or use a counted queue.

10. **Termination is not prompt or bounded.**
    [`baccy/watch.py:12`](../baccy/watch.py#L12) sets a stop event on SIGINT or
    SIGTERM but lets the current action finish. With a trigger, it can then
    wait another ten seconds on the trigger instead of the stop event. SSH,
    SCP, and ffmpeg calls in [`baccy/upload.py:819`](../baccy/upload.py#L819)
    have no timeout, so the action can hang indefinitely. The daemon needs a
    bounded, interruptible shutdown policy that does not corrupt transfers.

12. **Upload failure handling misses local and post-transfer failures.**
    [`baccy/upload.py:600`](../baccy/upload.py#L600) materializes HTML before
    its exception handler; [`baccy/upload.py:706`](../baccy/upload.py#L706)
    calls `source.stat()` and `catalog.append()` after its handler. Full disk,
    vanished source, or read-only storage can escape the per-file result path
    and terminate a daemon cycle. Protect those stages and distinguish an
    uploaded remote object from a failed local catalog write.

13. **Disk-full handling still starts later work.**
    [`baccy/backup.py:95`](../baccy/backup.py#L95) breaks its local-copy loop
    on ENOSPC/EROFS but proceeds to network copies and publication. This can
    make a resource-exhaustion incident worse and produce misleading partial
    results. Stop storage-dependent work for that pass.

15. **Network-source discovery can go stale or exhaust threads.**
    [`baccy/network.py:81`](../baccy/network.py#L81) returns cached sources
    within ten seconds even if a host has disappeared; later failures are
    handled per file. It creates one probe worker per pending ARP host, without
    a cap. Bound concurrency and make failed/disappeared hosts visible in the
    pass result.

16. **A remote file is accepted after only a size comparison.**
    [`baccy/network.py:269`](../baccy/network.py#L269) lists size/mtime,
    streams `cat`, checks only size, and hashes the downloaded file without
    comparing it to a remote digest. A same-size change during transfer can
    be committed as a coherent backup. Use remote snapshot semantics or
    re-stat/hash verification, depending on the intended trust level.

17. **Import is non-atomic, especially across filesystems.**
    [`baccy/importer.py:11`](../baccy/importer.py#L11) uses `shutil.move` or
    `copytree` directly into the final path and records the event afterward.
    A failure can leave a partial destination and possibly a partially moved
    source. Subsequent imports reject that destination as already existing.
    Stage and verify a session before publishing its final directory.

18. **The `--project` import value can escape the backup root.**
    [`baccy/importer.py:11`](../baccy/importer.py#L11) joins the raw override
    beneath `backup_root/audio` without validating it as one path component.
    `--project ../...` or an absolute path changes where files are written.
    Validate the override before creating directories, in both preview and
    actual import.

19. **Regular copies leave temporary files on `KeyboardInterrupt`.**
    [`baccy/copy.py:15`](../baccy/copy.py#L15) cleans up only after
    `_snapshot` returns; interruptions during snapshot creation can leave
    `.baccy-*.tmp` files. [`baccy/upload.py:819`](../baccy/upload.py#L819)
    similarly catches normal subprocess/filesystem errors but not an
    interrupt during ffmpeg. Repeated interruptions can exhaust disk space.

20. **No per-process recovery boundary exists in `watch`.**
    [`baccy/watch.py:12`](../baccy/watch.py#L12) lets an exception from a scan,
    catalog, remote listing, notification, or report escape the loop. Some
    failures should stop the service, but transient remote failures should
    become structured failure results and permit another pass. Define that
    boundary and test it with injected failures.

21. **Notifications can turn a backup pass into a daemon failure.**
    [`baccy/application.py:61`](../baccy/application.py#L61) and
    [`baccy/application.py:79`](../baccy/application.py#L79) call `notify`
    during recognition and reporting. [`baccy/notifications.py:12`](../baccy/notifications.py#L12)
    does not handle failure to launch `osascript`. Notification delivery
    should not control backup liveness. Also, "Backup complete" is emitted
    even if that pass has failed, unavailable, or deferred files.

22. **Service installation has no rollback or reliable restart identity.**
    [`baccy/application.py:54`](../baccy/application.py#L54) stops the old
    service before installing the new one, and a failed install leaves it
    stopped. [`baccy/cli.py:364`](../baccy/cli.py#L364) accepts any running
    daemon responding at the control socket, without checking that it is the
    newly installed release. Release directories from failed or superseded
    installations also accumulate. This is application-specific release
    handling around reccy's service controller, not a duplicate of the
    controller itself.

23. **Summary construction is quadratic in file count.**
    [`baccy/models.py:259`](../baccy/models.py#L259) serializes and validates
    the whole `BackupSummary`, including all prior results, for each new file.
    A large backup or sync repeatedly copies an ever-growing list. Accumulate
    results and counts once per pass, or use a mutable internal accumulator.

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

25. **`list` can be expensive on both remote types.**
    [`baccy/listing.py:72`](../baccy/listing.py#L72) sends one giant SSH shell
    command for every planned path and buffers the output. The command can
    exceed argument-size limits. [`baccy/listing.py:108`](../baccy/listing.py#L108)
    lists all S3 objects under each project's prefix even when only a subset
    of keys is planned. It holds all rows until completion, so the user sees
    no progress. Consider bounded batches and a cost-based choice between
    exact object metadata requests and prefix listing.

26. **The event log silently tolerates corruption and grows without bound.**
    [`baccy/catalog.py:57`](../baccy/catalog.py#L57) ignores malformed lines
    anywhere, not just an interrupted final append. Lost success records can
    cause reprocessing, while the operator gets no warning. Each append also
    opens and fsyncs the file separately. Define a recovery/compaction policy
    and report corruption.

## P2: user-facing semantics, maintainability, and tests

27. **Metadata-only sync cannot prove remote content is correct.**
    `sync` compares available size metadata and S3 identity where a catalog
    record exists. A same-size SSH replacement or an object without a prior
    catalog record can still look healthy. Normal publication can also skip a
    deleted remote object based solely on local catalog state. Make those
    guarantees explicit in CLI help and consider an opt-in verification mode.

28. **Landing pages can be skipped for the wrong directory.**
    [`baccy/upload.py:537`](../baccy/upload.py#L537) computes a content
    identity from the page configuration, project data, and URL basenames, but
    not the target directory. [`baccy/upload.py:600`](../baccy/upload.py#L600)
    uses that identity as the catalog key in ordinary publication. Two
    directories with the same listed filenames can therefore share a key;
    after the first succeeds, the second is reported unchanged without an
    upload. Include destination target in the catalog identity/check.

29. **Landing pages may link to deferred audio.**
    [`baccy/upload.py:182`](../baccy/upload.py#L182) adds all artifact plans
    before upload outcomes, and [`baccy/upload.py:517`](../baccy/upload.py#L517)
    builds pages from those plans. A collision or transfer failure can leave
    a published `index.html` pointing to a missing MP3. Either publish from
    confirmed remote objects or clearly mark incomplete pages.

30. **CLI flag parsing consumes positional rename text.**
    [`baccy/cli.py:312`](../baccy/cli.py#L312) strips `-d`/`--dry-run` from
    every argument, and [`baccy/cli.py:319`](../baccy/cli.py#L319) consumes
    `--config` and `--daemon` regardless of position. These strings can be
    legitimate rename patterns or replacements. Reserve global flags before
    the subcommand or use a parser that distinguishes positional arguments.

31. **Basic CLI help depends on installed daemon metadata.**
    [`baccy/cli.py:102`](../baccy/cli.py#L102) resolves configuration before
    `--help`; [`baccy/cli.py:345`](../baccy/cli.py#L345) fails if there is no
    installed daemon metadata. A new user cannot inspect usage with plain
    `baccy --help`. Parse help before daemon configuration, and distinguish
    default configuration from explicit `--daemon` in documentation.

32. **CLI status output hides actionable failure information.**
    [`baccy/cli.py:401`](../baccy/cli.py#L401) prints paths but not result
    statuses or failure details, omits deferred results entirely, and returns
    success for a pass with only deferred work. The `--dry-run` flag is also
    silently ignored by `list` and `test`, while `service --dry-run` accepts
    even an unknown action. Define clear output and exit-status contracts per
    command.

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

34. **The README describes several superseded behaviors.**
    [`README.md:8`](../README.md#L8) promises retention of overwritten backup
    bytes, but [`baccy/copy.py:285`](../baccy/copy.py#L285) replaces the
    destination without versioning. The README also says daemon configuration
    is selected only with `--daemon`, ordinary backup works without an
    installed service, and backup prints a JSON summary. Current CLI and copy
    code disagree.
    Correct the user guide after the intended behaviors are confirmed.

35. **The implementation has a few concentrated and repeated areas.**
    `baccy/upload.py` is 975 lines and handles journal interpretation, rule
    planning, HTML, transcoding, SSH, S3, and remote inventory. `baccy/cli.py`
    is 464 lines and handles parsing, service control, reporting, and daemon
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
    `test/test_watch.py` has two normal-loop tests but no trigger race,
    exception, or shutdown test.
    `test/test_listing.py` has three tests, none for a large SSH target set or
    remote failure. Upload tests cover ordinary behavior and the missing-file
    preflight and cross-session collisions, but not same-content landing pages
    or an interrupted transfer. Network tests cover discovery and normal
    copies, not same-size mutation. These are high-value additions; do not
    duplicate the broad axto fixture regression for each small failure case.

37. **The test layout has one oversized concentration, but not obvious excess.**
    `test/test_cli.py` is 703 lines of separate command behaviors and
    `test/test_upload.py` is 409 lines. Splitting by command or publication
    feature would improve navigation as tests grow. The large axto results
    fixture exercises realistic planning; its exact transfer snapshot is
    valuable and does not, by itself, show redundant testing. Smaller tests
    should cover failure branches rather than duplicate that snapshot.

## Additional work beyond the prompt

None.
