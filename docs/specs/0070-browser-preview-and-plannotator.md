# 0070 — Browser Preview and Plannotator

## Goal

Bring generated web applications, browser diagnostics and human review directly into the HerdR workflow.

## Browser integration

Prefer an audited upstream Herdr Browser implementation. Required capabilities:

- open URLs tied to session/worktree/dev service;
- Chromium/CDP-based page inspection when supported;
- screenshot capture as `ArtifactRef`;
- browser console/page error capture as bounded evidence;
- viewport presets for desktop/tablet/phone;
- safe Playwright/CDP automation hooks for test/evidence workflows;
- private debug endpoint handling; never expose CDP publicly.

## Plannotator

Prefer the upstream Herdr-Plannotator/annotation integration where compatible. A review should open from the related session/activity/candidate and preserve review identity, comments, decision and evidence links.

## Artifact integration

Browser screenshots, page captures and review exports use `ArtifactRef` and attach to session/candidate/test records rather than being hidden in plugin-local directories.

## Exit gate

A worktree web app can be opened in the HerdR browser view, console errors and screenshots can be attached as evidence, phone/tablet viewport can be inspected, and a Plannotator review can be initiated/resolved through the adopted integration.
