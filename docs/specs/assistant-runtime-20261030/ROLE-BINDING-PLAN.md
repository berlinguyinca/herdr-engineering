# Pi role binding plan

Goal: bind immutable package procedure references to verified host role files and append the
verified role to the fixed no-tools Pi system prompt without granting authority.

1. Add exact role-reference and host-registry validators; reject unknown IDs, versions or digests.
2. Read role files through owner-only, no-follow descriptors, require regular single-link bounded
   UTF-8 content, and verify SHA-256 before copying.
3. Copy verified role text into the activation's private Pi harness with mode 0400. Construct the
   fixed Pi argv with a static `--append-system-prompt` path; never place role text or package data
   in the shell command.
4. Include verified role metadata in host attestation, claim validation and ready receipts. A role
   changes instructions only; room grants and the no-tools boundary remain code-enforced.
5. Test missing, unconfigured, tampered, linked and changed roles, plus the exact fixed argv. Then
   run one harmless registered Secretary task and cancel its runtime.
