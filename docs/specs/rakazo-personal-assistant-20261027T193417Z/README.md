# Herdr Personal Assistant / Rakazo Integration Specification
Version: 1.0 | 2026-10-27 | Status: implementation specification, audit-first

## Objective
Implement a **native Herdr Personal Assistant** reachable from Settings → Personal Assistant and a global assistant entry point. Reuse/adapt upstream Rakazo capabilities selectively; **do not deploy a second independent Rakazo product, control plane, identity system, orchestration system, model scheduler, or dashboard**.

## Sources and verification
- Upstream: https://github.com/elie222/rakazo (Apache-2.0; review NOTICE/attribution before copying code)
- Herdr Engineering: https://github.com/berlinguyinca/herdr-engineering
- Pi Engineering: https://github.com/berlinguyinca/pi-engineering
- Adjacent original Herdr, InferWeave, OpenViking, and artifact/fabric implementations: discover and audit in local checkout; do not assume specific interfaces.
- Audit snapshot derived from GitHub repo tree and documented README/DESIGN on 2026-10-27. Source may evolve: pin SHAs at implementation time.

## Included specifications
01-product-scope.md, 02-architecture.md, 03-ux-ui.md, 04-data-api-events.md, 05-security-and-privacy.md, 06-rakazo-adaptation.md, 07-workflows.md, 08-phased-plan.md, 09-tests-and-acceptance.md, 10-deployment-operations.md, 11-risk-decisions.md, 12-existing-code-audit.md, START_PROMPT.md, MANIFEST.json.

## Definition of done
Production-shaped native experience, strong isolation, explicit authorization, successful E2E workflows, deployable phased changes, operator/runbooks, observability, rollback, artifact and history retention policies, no duplicate infrastructure.
