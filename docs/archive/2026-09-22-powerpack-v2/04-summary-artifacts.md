# Incremental Summary and Artifact Linking

Maintain structured:
- Started with
- Completed
- Current
- Next

Do not repeatedly summarize full transcripts. Use event-driven incremental updates:
1. structured events
2. immediate deterministic CURRENT update
3. summarize after meaningful milestones/phase changes/blocking/idle or event threshold
4. summarize previous summary + new events only
5. avoid LLM calls when deterministic formatting suffices

If an LLM is needed, use existing Pi/InferWeave routing rather than embedding another provider.

Inputs may include HerdR lifecycle events, Pi Engineering semantic events, Git metadata, tests/builds, browser QA, Plannotator reviews, artifact creation and user intervention.

Artifacts are first-class references, not blobs in the journal:
- shared/local file
- screenshot
- HTML/site/report
- test output
- Planator/Plannotator output
- Git commit/diff/PR
- browser session/URL
- video/log where applicable

Clicking should route through the appropriate existing file/browser/Git/review capability.
