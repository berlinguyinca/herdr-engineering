# Fleet canary rollout — dry-run validation (convergence 11)

Approved design: `docs/specs/pi-bolt-runtime-evaluation-20261023/rollout.md` and the
`herdr_fleet_update` ansible role (`ansible/roles/herdr_fleet_update/`).

## Acceptance (convergence 11-fleet-canary-rollout, FLEET-003 / FLEET-006)

> Ansible rollout with `serial=1` bender→beast→mac, health gate per hop, powerpack
> snapshot before and rollback on failure; dry-run evidence recorded.

## Mapping

| Criterion | Where | Verified |
|---|---|---|
| `serial=1` (one host at a time, bounded blast radius) | `ansible/playbooks/fleet-update.yml` (`serial: 1`) | present |
| Health gate per hop | `ansible/roles/herdr_fleet_update/tasks/busy-probe.yml` — reads active sessions from the control plane before the update and again right before the restart; a busy host is deferred, never forced without `fleet_update_force` | present |
| Powerpack snapshot before update | `ansible/roles/herdr_fleet_update/tasks/main.yml` (step 3, first task): `herdr-eng powerpack snapshot fleet-update-<host_id>` on the main host | added |
| Powerpack rollback on failure | same file `rescue:` block: `herdr-eng powerpack rollback fleet-update-<host_id>` when a step in the update block fails | added |
| Dry-run evidence | this file | here |

Hosts: `ansible/inventory/hosts.yml` lists `fry`, `beast`, `bender` (linux) and `mac`
(macos); `serial: 1` runs them one at a time.

## Dry-run procedure

Run from the main host (the fleet gateway) so the control-plane probes and the
controller-side powerpack calls resolve:

```
ansible-playbook -i inventory/hosts.yml playbooks/fleet-update.yml --check \
  --vault-password-file ~/.config/herdr-engineering/ansible-vault-pass
```

`--check` is genuinely write-free for this flow:

- Every state write, the powerpack snapshot, the powerpack rollback and the control-plane
  event POST are guarded by `when: not ansible_check_mode` (8 guard sites in
  `ansible/roles/herdr_fleet_update/tasks/main.yml`). The snapshot and rollback tasks added
  for this workset carry the same guard.
- Read-only control-plane probes (the busy health gate) run in `--check` too, so the
  defer-vs-update decision is exercised.
- The event POST records `'dry_run': ansible_check_mode`, so a check run is labeled
  dry-run in the host event.

## Syntax validation

`ansible-playbook -i ansible/inventory/hosts.yml ansible/playbooks/fleet-update.yml
--syntax-check` parses the playbook and both edited task files cleanly (the only warnings
are the expected "no inventory parsed / empty host list" when run without the vaulted
inventory on a non-gateway host). YAML of the edited `tasks/main.yml` parses.

## What a real (non-check) run does per host

1. Decide update vs defer from the health gate (defer if agents run / busy unknown, up to
   `fleet_update_max_defer_days=7`, never forced without `fleet_update_force`).
2. `powerpack snapshot fleet-update-<host_id>` — captures the controller lock/config/plugin
   policy before any mutation.
3. Update OS packages, herdr, pi, extensions, autospec, the fabric agent (pinned revisions only).
4. Re-check busy, restart the fabric agent once, publish one host event with pins + outcome.
5. On failure: `powerpack rollback fleet-update-<host_id>` restores the prior known-good
   snapshot, then the run records the host as failed and (with `fleet_update_stop_on_failure`)
   skips the remaining hosts.

## Caveat

The dry-run here was validated for syntax and check-mode write-safety from a non-gateway
host (no vault/inventory access to the real fleet). A production operator should re-run the
`--check` command on the gateway host to record live per-host decisions before any real run.
