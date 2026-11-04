# Pi Host Runner Activation Plan

Goal: claim authenticated package activations and run one fixed, digest-pinned Pi runtime inside
`task-restricted-v1`, without accepting commands, paths, environment variables, mounts, or
credentials from the control plane.

1. Define exact attestation, claim, heartbeat, cancellation and terminal-result documents with
   strict field/size validation and server-derived identity.
2. Add a host-side runner that stages an existing verified Pi installation into a private,
   content-addressed runtime directory and calculates a deterministic artifact digest.
3. Launch only the fixed Pi argv in the strict sandbox. Create private empty Pi configuration with
   a broker service model, keep gateway credentials in the host broker, and bind the staged runtime
   read-only at `/opt/herdr-pi`.
4. Check cancellation between bounded lifecycle steps, terminate the process group on revocation,
   and report hashed stdout/checkpoints plus a bounded result.
5. Unit-test document validation, artifact integrity, fixed argv, cancellation and failure paths;
   then perform one harmless isolated run on a controlled Linux host and record kernel/profile/model
   evidence without secrets.

No package mutation, arbitrary shell execution, host installation, operator-home overlay, or
Docker/Apptainer activation belongs to this slice.
