# Acceptance Tests

At minimum test:
1. clean install
2. install over existing config without loss
3. idempotent reconcile
4. dependency lock
5. browser test page
6. local generated-site inspection
7. mobile private binding/reconnect
8. shared read/write across representative hosts
9. concurrent shared-file behavior
10. shared FS disconnect/reconnect
11. shared failure cannot damage local project work
12. file viewer opens shared files
13. agent session auto-creates journal
14. session-start context
15. semantic events newest-first
16. CURRENT freshness
17. incremental summary without full-history resummarization
18. no hidden chain-of-thought persisted
19. cross-host artifact linked and accessible
20. Git/browser/Plannotator refs open correctly
21. journal survives UI reconnect/restart
22. retention/compaction
23. swarm/worktree safety
24. GitHub auth/no-auth
25. optional failure isolation
26. incompatible update blocked
27. rollback
28. uninstall preserves unrelated plugins/data
29. doctor human + machine output
30. Linux/macOS/headless behavior

Definition of done: one Powerpack install provides the verified curated bundle, shared workspace, session panel/timeline and artifact integrations without unnecessarily recreating upstream functionality.
