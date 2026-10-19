# Context handoff

As of 2026-10-19. This records conversation-only context, not implementation
details already available in the repository history and documentation.

## Recording-aware network backup

The requested implementation has been handed off. No deployment, daemon restart,
or transfer against a real recording machine was performed by this context.
The user has not subsequently confirmed deploying or exercising it. Do not
infer running daemon versions from the checked-out repositories.

Validation used automated tests and local process-supervision simulations,
not an actual SSH session. The remaining operational verification is to exercise
capture starting during a large transfer, subsequent idle recovery, event-stream
loss and shutdown on real machines. Obtain permission before running those
flows or restarting anything, especially a recorder that might be in use.

The user expects interruptions to be infrequent and explicitly chose abandoning
partial copies over retaining resumable transfers. Avoid adding resume/cache
machinery on the assumption that retransmitting an unfinished file is unwanted.

## Existing local edit

The unstaged change in `baccy/discovery.py` predates this implementation and
belongs to the user. It was deliberately neither reverted nor committed.
Python 3.13 verification used a temporary archive of the staged tree, excluding
that edit; Python 3.15 verification also ran against the working checkout.
Do not mistake those two verification contexts for the same source tree.

## Separate unresolved observation

The user reported that `baccy service stop` printed `running = false` together
with launchd's `Bad request` / service-not-found diagnostic. No live process
check or fix was performed. An initial assurance that shutdown had succeeded
was withdrawn: that output alone did not establish successful shutdown.
This remains an observation, not authorization to change service management.

## Additional work beyond the prompt

None.
