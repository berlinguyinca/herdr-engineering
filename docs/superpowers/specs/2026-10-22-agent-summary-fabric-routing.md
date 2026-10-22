# Fabric-routed Agent summaries

User requirement: use the existing fabric LLM and its load balancing, hosting the
small model only on an available empty GPU. The user explicitly permits temporary
CPU fallback when qualified idle GPU capacity is unavailable.

The summary service uses an authenticated, registered project alias through the
fabric proxy, with per-conversation admission identity and no credential-bearing
redirects. The proxy prefers exact qualified hot/warm small-model capacity from
the existing catalog; it never substitutes a larger/default model or triggers
cold provisioning. CPU fallback is the registered pinned local model. Catalog
failures and pre-response capacity failures fall back safely; application errors
and started streams are not replayed.

Custom policy and key streams remain operator-owned; generic ingestion cannot
forge those records. Tests must fail if registration, scope, context/slot/group
checks, redirect protection or credential separation are removed.

Automatic empty-GPU hosting is not currently proven in the deployed Go root.
InferWeave spec64 dynamic-node ownership and OPEN_GATES qualification are required;
this integration does not bypass that boundary or claim to implement placement.
The operations guide records the concrete handoff and CPU fallback behavior.
