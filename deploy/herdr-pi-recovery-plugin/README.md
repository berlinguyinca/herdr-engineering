# Pi recovery menu

This companion plugin adds **Preview Pi recovery** and **Recover stalled Pi
sessions** to HerdR's global menu. Preview creates a private local plan without
sending prompts. Recover explicitly runs one bounded, staggered recovery batch.
Both commands emit progress to the native plugin command log. Requires an installed `herdr-eng` with the recovery command and a
controller checkout containing its private fleet inventory.

Link this directory using the installed native HerdR plugin interface. Linking
does not require replacing or restarting the running server. Review the emitted
plan ID, then apply explicitly from a managed terminal:

```sh
herdr-pi-recover apply --id PLAN_ID
herdr-pi-recover status --id PLAN_ID
```

The controller preserves at least 60 seconds after every recovery attempt,
including interrupted runs. Accepted submission is not proof of completed work.
See `skills/pi-recovery/SKILL.md` for prerequisites and exclusion rules.

## Isolated activation

Install the committed source into its own immutable release directory using
`git archive COMMIT`, preserving `scripts/herdr-pi-recover` executable permission.
Point a dedicated `~/.local/bin/herdr-pi-recover` wrapper at that release's script;
do not replace the global `herdr-eng` executable or install into its shared venv.
Set `HERDR_PI_RECOVERY_PYTHON` to an existing compatible Python interpreter whose
dependencies can be read, and `HERDR_PI_RECOVERY_INVENTORY` to the controller's
private absolute inventory path. The script sets its own release PYTHONPATH and
HERDR_ENGINEERING_REPO so immutable runtime staging finds its packaged playbooks
regardless of the menu caller's working directory.
Optional `HERDR_PI_RECOVERY_REVISION` pins an explicitly reviewed immutable Pi
commit for per-session maintenance; omit it to avoid runtime upgrades.

Link the release's plugin directory through `herdr plugin link ABSOLUTE_PATH`.
Use `herdr plugin action list` and invoke the preview action to confirm a started
command log and inspect its progress. The native action runner starts commands
asynchronously; no background service, TUI replacement or restart is needed.
