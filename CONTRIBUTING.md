# Contributing to HerdR Engineering

HerdR Engineering is an integration project. The most important contribution rule is to change the system that actually owns a concern rather than recreating it here.

## Before coding

1. Read `START_HERE.md` and the relevant numbered spec.
2. Check `SPEC_MANIFEST.yaml` dependencies.
3. Inspect `STATUS.md` so two contributors do not unknowingly implement the same phase.
4. Work in an isolated Git branch/worktree.
5. Audit whether native Herdr or an adopted upstream already provides the capability.

## Pull requests

Every implementation PR must identify its phase, ownership boundaries, tests, phase exit-gate evidence, compatibility constraints and rollback notes. Update `STATUS.md` only after the exit gate passes.

## Cross-repository changes

When a contract requires changes in Pi Engineering, AutoSpec, Pileated or another owner, make a focused PR there and reference its version/commit from HerdR Engineering. Do not copy that subsystem here to avoid a dependency.

## Security and privacy

Never commit credentials, tailnet auth keys, provider tokens, private browser profiles or hidden model reasoning. Private-by-default networking is part of the product contract.
