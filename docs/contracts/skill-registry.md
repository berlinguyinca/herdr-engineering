# Skill registry (flag `control_plane.skill_registry.enabled`, default off)

Skills are **registry artifacts** (`artifact_type = 'skill'`), not a second store. The artifact id is the skill id; each
published skill version is one immutable artifact version; the signed package (`package.json`, canonical JSON with base64
files) is one content-addressed RustFS object listed in the version's `output_manifest`. Signature, key id, package
SHA-256 and the manifest live in the version `provenance.skill`. No table or migration is added.

Extracted from the collaboration data platform (#366): package format, Ed25519 verification, content validation, size caps
and the harness-side installer are kept. Left behind: its `collab_skill_*` tables, S3 layout, drafts, room scope, and
presigned transfer. Skills are untrusted data; the control plane never imports, installs or executes them.

## Config (YAML only; no environment variables)
```yaml
control_plane:
  skill_registry:
    enabled: false              # off: routes answer 404, nothing is constructed, no capability advertised
    signing_key_file: /etc/herdr/skills.key   # 64 hex chars (Ed25519 seed), mode 0600
    key_id: skills-1
    trusted_keys: {}            # optional extra key_id -> public key hex; the signing key's public half is always trusted
```
Requires the versioned artifact registry (Postgres) and RustFS. When on, `/api/v1/health` advertises `skill_registry_v1`.

## Routes (`/api/v1/skills`)
| Method and path | Purpose |
|---|---|
| `GET /skills?q=` | skills the caller may read (ACL applied in SQL before the limit) |
| `GET /skills/{id}/versions/{ver}` | verified manifest |
| `POST /skills` | publish one version (`files` base64, `request_key`, optional `skill_id`, `share_with`) |
| `POST /skills/{id}/versions/{ver}/transfer` | 60 s single-use capability `{url, sha256, transport_sha256, size_bytes, ...}` |
| `GET /skills/download/{token}` | stream the package (control plane; no presigned URL, no CSP/browser origin) |
| `POST /skills/{id}/versions/{ver}/installations` | harness receipt (a claim); audited as a fabric event |

## Authorization and scope
Authorization is the artifact registry's: owner (`created_by`) or a live `artifact_acl` grant. Sharing a skill is a `read`
grant (`share_with`); publishing a new version needs `edit`. A skill the caller cannot read is indistinguishable from a
missing one (404). There is no separate scope concept; room-derived access is a later decision. Transfer capabilities are
bound to the issuing principal, single use, and re-authorized when redeemed. POST routes need the operator role.
