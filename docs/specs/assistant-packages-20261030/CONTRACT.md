# Assistant package manifest v1

An assistant package is an immutable definition request. Registration stores a definition;
it does not create an agent, start a runtime, attach knowledge, or grant a capability.
Identity comes from the authenticated room owner and is absent from the manifest.

## Manifest

The JSON object has these exact fields:

```json
{
  "schema_version": 1,
  "name": "venue-coordinator",
  "version": "1.2.0",
  "charter": {
    "purpose": "Research and compare event venues.",
    "scope": ["event.venue.research"],
    "rules": ["Never contact a venue without an active task grant."],
    "delegation_requests": ["web.research", "email.draft"]
  },
  "capabilities": ["event.coordinate", "email.draft"],
  "runtime": {
    "kind": "pi",
    "artifact": {
      "type": "trusted-pi-runtime",
      "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  },
  "sandbox": {
    "profile": "task-restricted-v1",
    "network": "broker-only",
    "filesystem": "task-only",
    "limits": {
      "cpu_quota_percent": 100,
      "memory_bytes": 1073741824,
      "process_count": 128,
      "wall_time_seconds": 3600
    }
  },
  "model_policy": {
    "allowed": ["qwen3-32b", "llama-4-scout"],
    "default": "qwen3-32b"
  },
  "references": {
    "procedures": [{
      "id": "procedure:venue-research",
      "version": "2.1.0",
      "digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }],
    "knowledge": [{
      "id": "knowledge:event-venue-baseline",
      "version": "1.0.0",
      "digest": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    }]
  }
}
```

Names use lowercase slugs; versions are three-part numeric versions. Scope, delegation,
and capability names are bounded identifiers. Procedures and knowledge are immutable
references, not embedded live user memory or paths. The package can request a capability;
only a separate current grant can authorize its use.

Runtime artifacts are immutable:

- Pi uses `{type: trusted-pi-runtime, digest: sha256:…}`. This is a trusted runtime
  artifact hash, not an OCI image reference.
- Docker uses `{type: oci-image, reference: registry/path@sha256:…}`.
- Apptainer uses `{type: apptainer-sif, reference: oras://registry/path@sha256:…,
  digest: sha256:…}`. The reference pins the source; the second digest pins the SIF bytes.

Every runtime currently reports `runtime_launch_supported: false`. Docker and Apptainer
definitions can be reviewed and registered, but no adapter may launch them from this
contract. Pi also remains definition-only until the strict profile is bound and attested.

The only sandbox request is `task-restricted-v1`: task-only filesystem, broker-only
network, no manifest-supplied mounts/environment/SSH/privilege switches, and bounded CPU,
memory, process, and wall-time limits. Profile implementation must fail closed when its
isolation or limits cannot be enforced. Registering the request is not proof it is active.

Unknown fields are rejected at every level. Owner/user IDs, credentials, grants, payment
authority, arbitrary commands, host paths, mounts, environment variables, and privileged
flags are not manifest fields. The canonical JSON is capped at 64 KiB.

## Identity and immutability

Normalization sorts object keys for hashing while preserving list order. The package
digest is SHA-256 over canonical normalized JSON. A room owner may register a given
name/version once: replaying identical content is idempotent; different content under the
same name/version is a conflict. Package records are private to that authenticated owner.
The API derives ownership and returns status `definition-only`.

## CLI

```sh
herdr-eng assistant packages validate --file package.json --json
herdr-eng assistant packages register --room ROOM_ID --file package.json --json
herdr-eng assistant packages list --room ROOM_ID --json
herdr-eng assistant packages inspect PACKAGE_ID --room ROOM_ID --json
```

Validation is local and needs no credential. Register/list/inspect use the existing
assistant user credential. Agent credentials cannot administer the package registry.

## Deferred work

Runtime launch, package activation, strict-profile attestation, Docker/Apptainer adapters,
agent creation, task assignment, grants, secrets, snapshots, cloning, reference retrieval,
knowledge promotion, and dashboard management are separate slices. Existing helper/spawn
flows are not migrated by this contract.
