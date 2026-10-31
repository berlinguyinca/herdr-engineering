# Shared supervision dashboard evidence

These screenshots use synthetic browser-test fixtures, not live agents or a deployed
runtime. They demonstrate the same stable collaboration agent IDs across Agents,
Personal Assistants and Collaboration Flow.

- `supervision-dashboard-desktop.png`: existing dashboard shell and Agents job trees.
- `supervision-dashboard-mobile.png`: Personal Assistants at 390px, one room at a time.
- `supervision-tree-desktop.png`: explicit reviewer role, responsibilities, source
  evidence, budget and separate unknown/unadopted provenance.
- `supervision-tree-mobile.png`: the same detail contained at 390px.

The shared component uses existing tokens and semantic disclosures. Jobs and deeper
branches collapse; search reveals ancestors; refresh preserves focus/expansion;
read/auth failures clear the previous private tree. Evidence references remain
escaped text rather than arbitrary executable links. Fixture `healthy` labels are
input data; production recorded bindings are explicitly `recorded_unconfirmed`.

The mobile dashboard screenshot is a viewport capture after responsive layout settles.
Full-page Chromium capture stitched a sticky header twice despite exactly one app
shell/sidebar in the DOM. The browser test asserts those counts and document width.

Visual review against DESIGN.md: existing light surfaces, neutral borders, restrained
accent, readable labels and mobile single-column containment are preserved. This is
layout/accessibility evidence, not a pixel-matching claim against an external image.

Final visual verdict: pass (93/100) for consistency with the canonical dashboard
layout and component reference, not external pixel fidelity. The final mobile PNG
SHA-256 is `3fa2f9e1ebae9e933db4223f72ec1abe04dd725749272e088cd827ad5fb76e57`.
