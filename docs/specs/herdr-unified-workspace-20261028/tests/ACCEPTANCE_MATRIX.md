# End-to-end acceptance matrix

| ID | Scenario | Expected |
|---|---|---|
| A01 | Create room in WebUI; Pi joins | same room and messages; no duplicate stores |
| A02 | Run two agents + reviewer | tasks tied to correct team; distinct reviewer |
| A03 | Restart Pi or host mid-task | safe recovery; no duplicate irreversible action |
| A04 | Reattach from another host | same history and cursor; detached worker continues |
| A05 | 15 artifacts in 3 formats | all discoverable from project/task/issue; versions stable |
| A06 | Modify artifact v2 after review | v1 immutable; review status revisited as policy dictates |
| A07 | Close room and agents | historical graph/layout and artifacts remain |
| A08 | User with only room read | cannot invoke skills, attach shell or read private artifact |
| A09 | Guest graph traversal | no identity/metadata leaks through neighbors, counts, search |
| A10 | Rakazo drafts/send email once | exact approved recipients and attachment, one provider-side send |
| A11 | Inject instructions into attachment | no secret disclosure / unauthorized tool invocation |
| A12 | Dropped, reordered, repeated events | gap repair and idempotent graph; no duplicate tasks |
| A13 | Novice first-time visit | can ask, track, inspect results without docs or commands |
| A14 | 1000-node graph and older project | responsive collapsed view, layout stable, text fallback |
| A15 | HCN offline | standalone Pi remains usable; explicit reconnect state |
| A16 | External link public share | opt-in, expiry, revoke and artifact authorization verified |
| A17 | CI failure to PR | authorized diagnostic, gated fix, review, correct evidence links |
| A18 | Audit rollback feature flag | old frontend still works with additive migrations |
