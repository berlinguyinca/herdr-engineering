# HerdR Portable Public Gateway

This is an opt-in, delivery-only service definition. It is not included in the
private control-plane compose startup and does not publish a host port by
default. Place an approved HTTPS edge/provider in front of the exposed
container port only after publication policy and human approval are configured.

## Private-only default

Do not run this compose file for private-only installations. The ordinary
control-plane stack remains the supported private deployment and no public
provider is required.

## Opt-in operation

1. Build the current HerdR image containing `control-plane public-gateway`.
2. Ensure the PostgreSQL/RustFS network is the external network named by
   `HERDR_CONTROL_PLANE_NETWORK`.
3. Set `HERDR_CP_FLEET_DIR` to the directory containing the existing central
   fleet config. Credentials remain in that mounted private config and are not
   passed as command-line arguments.
4. Start this compose file on the controlled gateway host.
5. Keep the listener private until the provider adapter and gateway health have
   been verified in an isolated environment.

The gateway only serves `/s/<opaque-token>/...`; `/api/v1`, dashboard assets,
PostgreSQL, and RustFS administration paths do not exist on this listener.
Publication state is checked against PostgreSQL and bytes are fetched from the
private S3-compatible origin only after the publication token, state, expiry,
and pinned output are authorized.

## Stop and rollback

Disable the external provider first, revoke active publications, then stop the
gateway container. Rolling back the application image preserves PostgreSQL,
RustFS objects, and immutable artifact versions.
