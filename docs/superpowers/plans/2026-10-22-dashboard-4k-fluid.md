# Dashboard 4K Fluid Layout Plan

**Goal:** Use the full 4K operational canvas without stretching prose or regressing smaller viewports.

**Architecture:** Remove the shell's fixed width ceiling, then let individual operational components opt into additional columns at wide breakpoints. Preserve exact CI identity outside the compact visual label.

### Task 1: Fluid shell and 4K Overview

- [x] Add failing browser assertions for 3840px content width, Overview columns, prose width, and overflow.
- [x] Implement the fluid shell and four-column wide Overview.
- [x] Verify 3840px, 1920px, 768px, and 390px layouts.

### Task 2: Wide operational catalogs and inspectors

- [x] Add failing browser assertions for two-column Agent groups and expanded Projects composition.
- [x] Implement wide catalog and inspector rules without changing record ordering or state.
- [x] Verify focus, refresh, and mobile behavior.

### Task 3: Compact CI identity and whole-dashboard audit

- [x] Add a failing unit test proving the visible heatmap label is compact and full identity remains accessible.
- [x] Implement the compact label.
- [x] Run the full JavaScript/browser suite and inspect live-style 4K screenshots.
- [x] Request independent code and visual review and resolve material findings. Merge, deploy, and live verification follow from the reviewed commit.
