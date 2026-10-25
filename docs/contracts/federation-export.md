# Federation export contract (ADR-0008 phase P2)

Status: implemented, **default off**. Read-only. No importer, puller or mirror room exists yet (P3), and no typed
write exists (P4). Standalone Herdr is unchanged when `control_plane.federation` is absent or false.

Source of truth: `control_plane/federation.py` (YAML), `federation_sign.py` (signing), `federation_export.py` (endpoints),
`collab_federation.py` (shares and the export allowlist). Decision numbers refer to
`docs/adr/ADR-0008-independent-server-federation.md`.

## 1. Configuration (YAML only, no environment variable)

```yaml
control_plane:
  federation:
    enabled: false                     # the kill switch; must be a real boolean, else OFF
    server_id: hq.example              # this server's name; the AUDIENCE of every signed request
    signing_key_file: /etc/herdr/federation.key   # absolute path; 64 hex chars (an Ed25519 seed); mode 0600
    request_window_seconds: 120        # 10..600, signature freshness either side of now
    export_validity_seconds: 300       # 30..3600, how long a signed export claims to be valid
    max_export_events: 100             # 1..500 per response
    peers:
      partner-lab:                     # [a-z][a-z0-9-]{0,31}; never "*", ".", ":"
        enabled: true
        public_key: "<64 hex>"         # the peer's raw Ed25519 public key: the PINNED identity
        key_fingerprint: "<64 hex>"    # optional cross-check; must equal sha256(raw public key), all 64 characters
        key_expires_at: 2027-01-24T00:00:00Z   # after this the pinned key stops verifying (rotate ~every 90 days)
        rotate_after: 2027-01-01T00:00:00Z     # advisory only (never blocks)
        next_public_key: "<64 hex>"    # rotation overlap: accepted as soon as it is listed
        export_rooms: [room_...]       # explicit room ids, never "*": the operator side of the share consent
        requests_per_minute: 30        # 1..600
        max_bytes_per_hour: 16777216   # 1 KiB..1 GiB
        mirror_room: room_...          # INBOUND binding (P1/P3), independent of export_rooms
```

The section is re-read on every request. An unreadable file, an invalid section or a non-boolean `enabled` is **OFF**
(a bad edit can only close federation). A peer without `public_key` can be configured but never authenticates.
Generate a key: `python3 -c "import secrets; print(secrets.token_hex(32))" > federation.key; chmod 600 federation.key`;
derive the public key with `federation_sign.public_key_hex(seed)`. Signing uses Ed25519 from `cryptography`, already a
dependency through the `collab` and `review-gate` extras (`PyJWT[crypto]`); it is imported lazily and without it
federation is off (404).

## 2. Request signature (Owner decision D3)

Scheme `herdr-federation-v1`, Ed25519. Five headers, each exactly once (a repeated header refuses the request):

| Header | Value |
|---|---|
| `X-Herdr-Peer` | the requesting peer id |
| `X-Herdr-Key` | the FULL 64-hex fingerprint (sha256 of the raw public key) of the signing key |
| `X-Herdr-Timestamp` | integer epoch seconds, `[0-9]{1,11}` |
| `X-Herdr-Nonce` | `[A-Za-z0-9_-]{16,64}`, single-use |
| `X-Herdr-Signature` | unpadded base64url of the 64-byte signature (canonical encoding only) |

Signed message: the lines `herdr-federation-v1`, `request`, METHOD, RAW-TARGET, TIMESTAMP, NONCE, sha256(body) hex,
PEER-ID, AUDIENCE joined by `\n`. RAW-TARGET is the path and query exactly as sent (no decoding, no re-ordering).
AUDIENCE is the receiver's `server_id`, so a request signed for one server cannot be replayed at another that pins the
same key. Accepted keys are the pinned `public_key` (until `key_expires_at`) and `next_public_key`. The FULL fingerprint
is compared (P1 compared 12 characters; fixed). A nonce is consumed only after the signature verified, per peer, in a
bounded table; a full table refuses.

## 3. Endpoints (main listener only; GET only)

* `GET /api/v1/federation/shares` -> `{"kind":"shares","shares":[{"share_id","granted_at"}]}`: the shares of the
  CALLING peer that are valid right now. No room ids.
* `GET /api/v1/federation/shares/<share_id>/events?cursor=<n>&limit=<n>` -> room chat messages after the share was granted.
  Only `cursor` and `limit` are accepted, once each, as plain digits; anything else is 400 (for an authenticated peer).

Every refusal before authentication, and every refusal of an unshared or unavailable share, is the **same 404** with a
byte-identical body: federation off or `server_id`/signing key missing or unreadable (group/world-readable file, a
symlink, malformed) or the signing library missing, a degraded collab log, a non-GET verb, a bad/expired/replayed
signature, an unknown or disabled peer, an unknown, revoked, unlisted or another peer's share. A peer over its
request or byte quota gets 429 with `Retry-After`. The surface is also 404 on the operator and public listeners and
when `collab_require_https` is on without the TLS-proxy header. A GET carrying a body is refused.

## 4. Export document and signature

The body is canonical JSON (sorted keys, no spaces) serialized **once**; the signature covers those exact bytes behind
the prefix `herdr-federation-v1\nexport\n` (so a request signature can never be an export signature). Headers:
`X-Herdr-Export-Signature`, `X-Herdr-Export-Key` (the exporter's full fingerprint). Fields: `scheme`, `kind`
(`shares`|`events`), `exporter` (`server_id`), `audience` (the peer), `issued_at`, `expires_at` (epoch seconds,
`export_validity_seconds` apart; a consumer must refuse a document outside its window), and for events `share_id`,
`cursor`, `next_cursor`, `more`, `events`.

## 5. Field allowlist (Decision 12)

An allowlist per event type (`collab_federation.EXPORT_FIELDS`); a type not listed is never exported, and each row is
built by picking allowed keys, never by deleting from a copy. Then every row goes through `redact.redact_payload`.
Only the `message` type exists in P2:

`event_type`, `origin_event_id`, `origin_sequence`, `occurred_at`, `sender_kind` (`person`|`agent`|`external`),
`sender_ref`, `text` (at most 8000 characters), `in_reply_to`, `trust` (always `untrusted`).

`sender_ref` is a pseudonym, `p-` + 12 hex of sha256(share id, sender id): stable inside one share, different across
shares and people. NOT exported: user ids, emails, display names, owner ids, sender keys, mentions, context references,
idempotency keys, room, project and workspace ids, grants, fingerprints, any secret, member rosters, terminal bytes,
reasoning, usage. `in_reply_to` is set only when the target is itself exported (same room, after the grant).
Scope: only `room` scope messages leave; `org`, `private`, `mission` and every non-message feed event never do.
History from before the grant is never exported (the cursor floor is the grant's log sequence).

## 6. Consent: when is a room exported to a peer?

ALL of these, re-checked on every request (Decision 3):

1. `control_plane.federation.enabled` is true and the peer is `enabled` with a pinned key;
2. the operator YAML lists the room in that peer's `export_rooms`;
3. a `federation_share_granted` event for `{peer, room}` is active (accepted through
   `POST /api/v1/collab/rooms/<room>/federation/shares` `{"peer": "<id>"}` by a current OWNER: a signed-in person, not
   an agent, not an external agent), and not `federation_share_revoked`;
4. the owner who accepted is still an owner of the room, and the room exists.

Losing any one closes the export on the very next request. Any current owner revokes with
`POST .../federation/shares/<share_id>/revoke`; revoking is never blocked by the kill switch. Any member who can read the
room sees its shares: `GET .../federation/shares` (their chat leaves the server, so they are told).

## 7. Collab events

| Event | Class | Meaning |
|---|---|---|
| `federation_share_granted` | `REVOCABLE_CREATE` (`fedshare`) | an owner accepted the share |
| `federation_share_revoked` | `SAFETY_EVENTS` | its pre-paid revocation (an owner, or the system when the YAML stopped allowing it: `peer_disabled`, `unlisted`, `owner_lapsed`) |
| `federation_peer_enabled` | `REVOCABLE_CREATE` (`fedpeer`) | audit record: the YAML enabled the peer (written by `maintain()`, never by a request) |
| `federation_peer_disabled` | `SAFETY_EVENTS` | its pre-paid revocation: the peer was disabled or removed from the YAML |

None are in the room feed. They are additive: a service that does not know them ignores them on replay. A re-enabled peer
does NOT revive a share revoked by the system; an owner must grant it again. An unreadable or invalid YAML never
rewrites the log. Mirrored data never enters the collab log (Decision 5).

## 8. Limits and non-goals of P2

No puller, importer, mirror room or freshness UI (P3). No typed write (P4). Lineage graph nodes and edges are not exported
yet (a new row in `EXPORT_FIELDS` and a reviewed reader are needed). Quotas and replay nonces are in memory (a restart resets
them; the signature window still bounds replay). Display names are not exported. Rollback: revert the PR; the kill switch is
`control_plane.federation.enabled: false`.
