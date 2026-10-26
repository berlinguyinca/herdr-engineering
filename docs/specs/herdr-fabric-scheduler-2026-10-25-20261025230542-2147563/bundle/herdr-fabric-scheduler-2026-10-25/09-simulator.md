# Phase 9 — history replay, adaptive resource estimation and capacity planning
Use persisted sanitized placement/load/activity history to replay decision points with candidate weights/quotas. Model reserved and actual resource curves, locality and WAN transfer time, dependencies, unsupported telemetry and uncertainty. Outputs: admission wait distribution, overload time %, fairness, completed jobs/hour, p95 time to launch, cache hit %, churn count, migration failure rate and whether owner interactive work was delayed. Show accuracy limitations of counterfactual forecasts; do not claim real performance gains from simulation alone.

## Learner
Per workload family/project/repo/test command, estimate resource envelope using p50/p90 measurements with cold-start defaults, aging and outlier protection. Only propose changes initially; changes require config flag and bounded hard caps. Never learn/relax blacklist, ACL, owner priorities or safety thresholds automatically. Explain why estimates changed.

## Pilot success criteria
Compared with baseline round robin on repeatable workload, improve p95 admission and cut severe PSI overload duration without raising job failures, Git conflicts, restarts or human intervention. Publish measured evidence, not arbitrary target claims. For migration require zero duplicate execution in the fault suite. Capacity forecast uses observed demand + confidence intervals, no procurement auto-action.
