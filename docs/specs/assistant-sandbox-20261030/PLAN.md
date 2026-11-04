# Strict Pi Sandbox Profile Plan

Goal: add an opt-in `task-restricted-v1` sandbox profile that refuses to start Pi or another
host harness unless filesystem, brokered-network, process, memory, and wall-time controls are
all enforceable.

1. Add profile-contract tests for unknown profiles, missing private harness state, open network,
   optional mounts, unsafe egress sockets, malformed limits, unavailable systemd scopes, and the
   exact enforced launch arguments.
2. Validate the strict profile before constructing Bubblewrap bindings. Preserve the legacy
   behavior when no profile is selected.
3. Require a private runner harness directory, broker socket, task-only writable workspace, and
   numeric resource limits. Refuse SSH and extra read-only mounts in strict mode.
4. Require systemd user-scope enforcement and wrap the command in a bounded wall-time process.
   Never fall back to an uncapped strict run.
5. Keep packaged and deployed script copies byte-identical; document enforced properties and
   platform verification limits.

No Docker/Apptainer launcher, package activation, capability grant, or legacy-helper migration is
part of this slice.
