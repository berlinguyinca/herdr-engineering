Checkpoint payload and manifest were replaced separately, so a failed or concurrent
write could destroy a valid recovery snapshot. Paths could follow symlinks, and
retrieval did not validate the requested checkpoint or expected execution lease.

Publish one immutable private container atomically through a pinned directory
descriptor. Reject conflicting duplicate IDs, symlink/nonregular files, and
identity mismatches; clean failed temporary writes and fsync publication.

Legacy experimental `.json`/`.bin` files remain untouched and require explicit
verified conversion. This change does not enable migration or claim full scheduler
acceptance. It is one scoped delivery slice for #290.

Validation: 22 checkpoint/workspace tests passed on a clean worktree based on
`origin/main`, including concurrent publication, failed fsync, root replacement,
symlink access, and copied/incorrect lease identity. Targeted Ruff and diff checks
passed. The checkpoint module received an independent scoped review.
