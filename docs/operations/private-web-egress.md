# Optional private web egress bridge: review checkpoint

This slice supplies an opt-in deployment transaction, a separate read-only policy
validator, and a Compose override. It is wired into `deploy.sh` but **has not been
activated in production**. Root effects have been exercised only with unit stubs. No production
network, UFW rule, deployment, configuration, or credential was changed. Other
operators' deployments/locks remain authoritative. OpenViking migration remains
cancelled; it is unrelated to this networking work.

## Proposed mechanism

Explicitly selecting `deploy/control-plane/compose.web-egress.yaml` would keep
`control-plane-web` on its existing default network and add one dedicated bridge:

- Network: `herdr-control-plane_web-egress`.
- Stable Linux bridge: `br-herdr-cp-eg`.
- Candidate CIDR: `172.31.252.0/29`, configurable through the override's
  `HERDR_CP_WEB_EGRESS_CIDR` and the validator's matching `--cidr` argument.
- `gw_priority: 100` selects this network as the default; existing default-network
  priority stays zero. Connected service routes remain on the existing network.
- Only `control-plane-web` joins. No worker, database, model service, public
  collaboration/share sidecar, extra daemon, proxy or new published application
  port is added. A Compose service profile does not itself make an extra network
  opt-in; the override must be explicitly selected by the existing deploy path.

The proposed UFW permission is **host INPUT**, matching all of interface, source
CIDR, an explicitly configured private gateway IPv4 `/32`, TCP and port 443.
There is **no repository gateway-address default**. For example, the Bender
canary used `100.104.39.6/32`; other hosts must supply their own assigned address.
Preflight reads `ip -j -4 address show` and refuses a nonlocal destination, a
public/multicast/loopback address, or a noncanonical/non-/32 value. RFC1918 and
100.64.0.0/10 unicast addresses are accepted only when locally assigned, preserving
the host-INPUT assumption. The read-only validator emits argument vectors; only
the separately opted-in deployment executor can run the exact UFW add/delete.

This is not outbound confinement. Docker's ordinary forwarded/NAT egress remains
subject to its existing rules. The grant permits gateway TCP 443, including any
other SNI/HTTP route served there; it does not implement a memory-only policy or
replace TLS and application authorization. Default UFW input denial does not
imply denial of Docker-forwarded published ports.

## Read-only validation

```sh
python3 -m herdr_engineering.web_egress
python3 -m herdr_engineering.web_egress --enabled --gateway-ip YOUR_ASSIGNED_PRIVATE_IPV4/32
python3 -m herdr_engineering.web_egress --enabled --gateway-ip YOUR_ASSIGNED_PRIVATE_IPV4/32 --cidr 172.30.254.0/29
```

Without `--enabled`, no Docker or host inspection runs. With it, the validator
reads all IPv4 and IPv6 routing tables, host interface names, Docker network
metadata and the private web container's identity/labels/network endpoints. It
reads no credentials, fleet configuration, container environment or mounts.
It checks all current host/VPN and Docker subnet overlaps; it never picks an
alternative subnet or adds a route to bypass a conflict. CIDRs must be canonical
RFC1918 IPv4 networks between /28 and /30. Foreign bridge-name collisions fail.

If the named network already exists, supply its independently recorded ID with
`--expected-network-id FULL_ID`. A matching name alone is insufficient. Required
Compose project/network labels, policy label, driver, non-internal/IPv4-only
settings, bridge name, subnet/gateway and exclusive web membership are validated.
`--require-attached` also requires exact web endpoint network ID, source address,
prefix/gateway, and a strictly preferred priority of 100. Unexpected IDs, labels,
foreign members or network/route conflicts refuse; nothing is adopted or removed.

The JSON result always says `activation_ready: false`: static metadata and an
argument vector are not proof of packet filtering, authentication, or a safe
production activation. Existing-network inspection and route snapshots must be
repeated immediately before and after a future transition under the deploy lock.

## Actual temporary-canary comparison, 2026-11-05

Installed Docker Engine 29.1.3 and Compose 2.40.3 support `gw_priority`. Parsing the
base + collaboration + egress override succeeded, with only the private web
service on the new network. No environment/secret file was loaded for that check.

Uniquely named/labelled temporary containers used the same immutable live-image
snapshot in both modes:
`sha256:ba4de5a1c3e13cb4cc17586a280d9068fb37c1fed4db0c3986e100052bd4af4e`.
They had no credential/config mounts, no capabilities, a read-only root, bounded
resources, and no public ports. The first comparison published no ports at all.
Cleanup in `finally` checked ownership labels before removing only those test
containers and the now-empty test network. No canary resources remain.

| Check | Original default | New default |
| --- | --- | --- |
| Gateway | 172.16.4.1 | 172.31.252.1 |
| Source toward 100.104.39.6 | 172.16.4.7 | 172.31.252.2 |
| Docker/external DNS | Resolved | Resolved |
| Postgres TCP | Connected | Connected |
| Object-store health | HTTP 200 | HTTP 200 |
| Private model health | HTTP 200 | HTTP 200 |
| Host collaboration :9482 `/health` | HTTP 404 | HTTP 404 |
| GitHub HTTPS | HTTP 200 | HTTP 200 |
| Google OAuth host HTTPS `/` | HTTP 404 | HTTP 404 |
| Gateway HTTPS, without a new UFW grant | Timeout | Timeout |
| `host.docker.internal:9482` | Timeout | Timeout |

404 establishes an HTTP response path, not application correctness. No response
bodies, credentials or session data were captured. A synthetic
`host.docker.internal:host-gateway` mapping was supplied only to the canaries;
the production web has no such mapping. This host resolves that alias to
172.17.0.1, a distinct destination: no blind permission for it is proposed.

Host model :8088 and existing dashboard :80 timed out in the first mode and
returned 200 in the second. A foreign deployment was active during comparison,
so do not infer that the new route caused those improvements.

A separate reply-path canary published **only an ephemeral 127.0.0.1 port**, with
no public bind. Its synthetic HTTP server returned 200 to the host with both
default gateways. It used the same immutable image and was cleaned up along with
its temporary network. This tests a loopback-published return path, not a claim
that every tailnet-client/SSE path has been proven after production recreation.

## Remaining integration and activation gates

Do not widen the grant beyond 443 to repair another endpoint. Gateway HTTPS
remains unproved without the separately reviewed host-input rule. No production
activation or successful authenticated memory read is claimed here.

The implemented deploy integration reuses `deploy-lock.sh`'s existing
`rollout.lock`/compatibility lock and attempt snapshots; no second lock or
coordinator was added. Capture previous network selection, IDs and owned rule details
with the corresponding image/config/runtime-profile policy. Require the expected
running-image lineage and an available owned deploy lock; do not interfere with
an operator's ongoing deployment. The read-only helper's metadata is not lock enforcement; the executor checks the
inherited lock descriptor against that protected state directory and acquires the
same open-file description without unlocking the parent.

For disable, failed forward activation and rollback, the required order is:

1. Remove the exact owned firewall rule from the saved receipt, before abandoning
   the interface/network. Do not delete a rule by ordinal or a broad subnet match.
2. Restore the prior Compose membership and matching image/config/profile policy
   through the existing deploy transaction. Restore any previously approved rule
   only with its corresponding validated network/policy and route proof.
3. Remove an owned network only after checking its saved ID/labels and that it is
   empty; never detach foreign members or use project-wide `compose down`.

If rule removal or rollback validation fails, report failure and preserve recovery
state instead of abandoning a broad/stale grant. Future root-backed canaries must
prove exact host-input matching and no **new** permission for siblings, other
source networks or other host ports, accounting for pre-existing Docker forwarding
rules. Actual tailnet published replies, authenticated gateway access and policy
rollback remain activation acceptance checks. Nothing in this slice changes
viewer authorization or creates public exposure.


## Implemented opt-in deployment callbacks

After independent review and a separately coordinated live canary, the operator
can select `--memory-private-egress` with
`--memory-private-egress-gateway YOUR_ASSIGNED_PRIVATE_IPV4/32`. The latter may
also come from the trusted host environment
`HERDR_CP_MEMORY_PRIVATE_EGRESS_GATEWAY`. `--memory-private-egress-cidr CIDR`
selects an explicit small candidate; no overlap is automatically worked around.
The override is absent on an ordinary deploy. Omitting the opt-in on a later
deploy with an owned active receipt performs a transactional disable; this is
not a silent persistent opt-in. With no opt-in and no owned state there are zero
network inspections and zero root calls.

Enabled preflight is Linux-only. Before any runtime profile, network membership
or firewall mutation, it performs **read-only** noninteractive `sudo -n --
/usr/sbin/ufw status` and `show added` checks. Missing UFW, sudo needing a password,
unsupported status, and inactive UFW refuse enablement. Grant restoration also
requires active UFW. Disable/recover require noninteractive read/delete authority
but deliberately allow inactive UFW, removing persisted owned rules so a later
global reactivation cannot resurrect a stale grant. The executor never runs
`ufw enable`, changes defaults, adds a broader port permission, or operates on a
rule by ordinal.

`web-egress-deploy.sh` supplies callbacks inside the existing transaction:

- Preflight and dry-run validate without mutation; root reads are allowed only for
  explicit opt-in or existing owned cleanup state. A dry run never adds/deletes.
- Snapshot stores `previous.json` and `candidate.json` beside the existing attempt
  image/config and separate memory-profile archives. Private write-ahead receipts
  carry the exact rule vectors, destination, CIDR, network ID and a public
  ownership-generation label. They are synced before a root grant.
- `before` revokes owned permission before Compose can remove/change membership.
  Compose, under the same deploy lock, creates/attaches the bridge. Existing
  foreign networks/rules are refused rather than adopted.
- The matching runtime profile is installed before `after` validates the new web
  endpoint/network identity and priority, checks active UFW, and adds exactly the
  recorded rule. Post-add validation failure attempts restrictive revocation.
- For an opted-in forward rollout with a managed SDK profile, the installed
  canonical `herdr-memory --harness codex status` must complete within a 20-second
  caller deadline and report Boolean `healthy: true`. This runs after profile
  installation and the egress grant, before profile promotion or policy commit.
  Unhealthy/malformed status, nonzero exit and timeout enter the existing rollback;
  response bodies and stderr are withheld. Disabled, unmanaged and clear modes
  skip the gate. Restoring an old known state never depends on backend health.
- Rollback restores the saved selector/CIDR/generation with the previous commit's
  Compose, pinned image/config and **previous** memory profile before regranting.
  Invalid callback directions and wrong/missing pending attempts fail before
  selecting a policy; there are no guessed previous states.
- Forward commit clears the pending journal only after the existing deployment
  receipt is written. Disable or rollback cleanup can remove only an empty
  network with the saved ID, generation, labels, bridge and CIDR. Foreign members
  are never detached. Cleanup failure retains recovery state/network and reports
  it; a failed rule revocation blocks membership abandonment entirely.

### Hard-interruption recovery limit

Normal failures and trapped SIGTERM/EXIT use the existing coordinator's rollback,
including its previous profile. SIGKILL/host loss is different: this coordinator
keeps some host-file backups in temporary storage. A complete automatic restore
of those unrelated slices/units cannot be proven from the durable network
receipt, and this change does not introduce a second recovery coordinator.

`--memory-private-egress-recover SAVED_DEPLOY_ATTEMPT` reuses the same existing
rollout/compatibility locks and performs **permission-only recovery** from that
attempt's protected journal. It accepts inactive UFW and revokes persisted owned
rules without deleting a network, moving a container, reading fleet credentials,
or installing a profile. It deliberately exits 3 and leaves the pending attempt:
**image/config/profile restoration is not declared complete**. Ordinary deployment
refuses an unresolved pending journal. The parent must review/reconstruct the
same saved coordinator attempt before restoring the matching runtime/profile and
calling rollback completion. Revocation failure leaves all recovery evidence.


#### Operator command recipe after SIGKILL

These commands are for a reviewed recovery window, **not commands run during this
implementation**. Use the exact attempt path reported by the interrupted deploy
and recorded in `web-egress/pending.json`; never choose the newest directory or
infer a previous image/profile. Run the reviewed checkout's existing deploy entry
point as the deploying user, not a separate root shell:

```sh
reviewed_checkout=/absolute/path/to/reviewed/herdr-engineering
saved_attempt="$HOME/.local/state/herdr-engineering/deploys/attempt-EXACT_RECORDED_ID"

recovery_status=0
"$reviewed_checkout/deploy/control-plane/deploy.sh" \
  --no-fetch --deploy-lock-wait 0 \
  --memory-private-egress-recover "$saved_attempt" || recovery_status=$?
printf 'Permission-recovery exit: %s; deployment is still blocked\n' "$recovery_status"

test -f "$HOME/.local/state/herdr-engineering/deploys/web-egress/pending.json"
test -f "$saved_attempt/web-egress/candidate.json"
test -f "$saved_attempt/web-egress/previous.json"
```

The entry point acquires the existing `rollout.lock` and compatibility deploy lock
before calling the root executor. If another deployment owns them, stop and let
its owner finish: do not remove either lock file, kill that deployment, force a
network disconnect, or bypass the coordinator. The executor validates that the
saved attempt belongs to this protected deployment state and matches the pending
journal before acting. No fleet config is needed for this permission-only phase.

Exit **3 is intentional even when the permission phase finishes**. Exit 3 alone
does not prove deletion succeeded: distinguish the explicit completion line from
an executor failure, and have the operator verify the exact owned rule is absent
before proceeding. Missing sudo/UFW authority or deletion failure leaves the
journal and network intact and must be resolved by the operator. An inactive
firewall is allowed for this cleanup because its persisted rule still matters.

**Blocked state after SIGKILL remains:** the pending journal is retained; the
network/container membership and actual image/config/profile may still be mixed;
ordinary deploy preflight refuses to proceed. Do not unlink the journal to make a
new rollout pass, and do not treat successful permission revocation as restoration.
Preserve `metadata.json`, `rollback-images.json`, the previous/candidate fleet
snapshots, both memory-profile archives, and both egress receipts. The parent must
reconstruct the saved attempt's normal coordinator rollback with the matching
previous runtime/profile. If temporary host-file backups are missing, that full
restoration is a manual recovery gate; this slice intentionally provides no
fictional one-command full restore or second recovery coordinator.

The dashboard and Memory view retain the operator-approved trusted private
location/open-operator mode. No sign-in or Cognito requirement is introduced;
backend credential/scoping boundaries and public exposure remain unchanged.
Existing `collab-egress.py` is a per-helper Unix-socket HTTP CONNECT allowlist
proxy, not a UFW/Compose transaction. No parallel proxy, daemon or canonical SDK
Unix transport is added here.

## Newton review checkpoint

Parent reconciliation base for the forward connectivity fix is
`a6f0168e79a1e706879a9654d6508a3055a465f1`, including main `8cebbe17`
with PR620 merged and the newer artifact changes preserved. The original
implementation history below is retained for provenance.

Implementation worktree: `herdr-private-web-egress-20261105`, branch
`feat/private-web-egress-20261105`. Base is PR620 `ff87b581` plus main `49a22333`;
the upstream-only base merge is `2cc8a358249bcf312f5f46d94cbdbd9fb4d95ee4`.
Owned egress changes are committed separately for exact-SHA review. No PR642/Cognito change was
merged, and no production root command, deployment or configuration change was
performed by this slice. Existing foreign deployment progress was preserved.

Final verification: 104 focused policy/transaction/coordinator/profile/Compose
tests passed, plus seven actual deploy-script opt-in/preflight/permission-recovery
tests. Sampled existing forward-failure rollback, immutable image/config restore,
success receipt, deploy-lock exclusion and default dry-run regressions also passed.
Ruff, shell syntax and diff-whitespace checks passed. Root effects in these tests
were stubs only. The entire engineering test suite was not rerun for this bounded
slice. The earlier temporary canaries were cleaned up and were not replaced by a
production activation. The remaining live grant/route canary belongs to the parent.
