# Lineage viewport: measured cost at 5,000 and 10,000 nodes

Measured 2026-10-29 on branch `feat/workbench-ui-20261029` (commit after "regenerate lineage and workbench baselines"), host bender: AMD Ryzen Threadripper PRO 3945WX, 24 threads, 504 GB RAM, Linux 6.8.0-147, Chromium 156.0.8078.4 (headless, Playwright). The host is shared: load average was 9.8 to 11.4 during the scale runs (the `ui-gates-perf` calibration loop measured a slowdown factor of 1.0, i.e. no slower than the reference). Every number is **one run**, not a distribution across runs. Frame times are quantised by the display (about 16 ms per frame), so differences under about 30 ms are not meaningful.

There are two different measurements. They answer different questions and must not be mixed.

## A. End to end through the real server (`ui-gates-perf`, part of `npm run test:ui-gates`)

N nodes are written through the real ingest service, then the page pages through them with "Load more". **The server's offset paging stops at 3,000 nodes of one room (scan budget), so for the 5k and 10k runs the browser only ever holds 3,000 nodes.** The page says so ("The server stopped reading here"), and the gate asserts it. These are therefore not 5k/10k-node client runs.

| N written | nodes the page loaded | first render (ms) | load all pages (ms) | Load more p95 (ms) | table switch (ms) | filter key-to-frame p50 / p95 (ms) | long tasks (count, max ms) | JS heap (MB) |
|---|---|---|---|---|---|---|---|---|
| 2,000 | 2,009 | 157 | 923 | 110 | 28 | 25 / 27 | 1, 160 | 12 |
| 5,000 | 3,000 | 223 | 2,073 | 193 | 30 | 26 / 28 | 1, 136 | 10 |
| 10,000 | 3,000 | 303 | 2,988 | 287 | 23 | 25 / 33 | 1, 197 | 14 |

Same gate on the base commit (before this round's changes, `3d09c13`): first render 137 / 195 / 329 ms, Load more p95 117 / 179 / 298 ms, key-to-frame p95 28 / 31 / 33 ms. The differences are within what one run on a shared host can produce; this round (filters disclosure, minimap) shows no regression in these figures, but one run each cannot prove the absence of a small one.

## B. The client with 5,000 and 10,000 nodes fully loaded (`tests/perf/lineage-scale.mjs`)

To get past the server's 3,000-node cap, the script answers the page's own `GET /graph/lineage?...` requests from a synthetic graph, 200 nodes per page exactly as the server pages, in the real assembled page (real `real_server.py`, real JS). Everything is measured in the page: each step is dispatched inside the page and timed to the second animation frame after it, so a figure includes the handler, the cull and the redraw. The synthetic graph is `task:` nodes joined by `dependency` links, in two shapes: `tree` (a binary tree, depth about 14, very wide) and `chains` (50-long chains under one root). Real lineage will differ (more node kinds, richer labels), so treat the absolute numbers as indicative.

Run by hand: `node --test --test-concurrency=1 tests/perf/lineage-scale.mjs` (not part of any npm script or gate; about 35 s).

DOM bound at every size and shape: the viewport held at most **240 nodes and 480 links** (the limits), and at most 2,405 elements inside the viewport at the farthest zoom-out. At the fitted view it drew 43 to 71 nodes.

| shape | N | load all (ms) | drawn at fit / far out | zoom in p50 / p95 | zoom out p50 / p95 | pan p50 / p95 | minimap click p50 / p95 | select p50 / p95 | long tasks (count, max ms) | JS heap (MB) |
|---|---|---|---|---|---|---|---|---|---|---|
| tree | 5,000 | 1,205 | 43 / 240 | 32 / 54 | 32 / 65 | 32 / 34 | 32 / 41 | 32 / 39 | 3, 59 | 13 |
| tree | 10,000 | 3,571 | 43 / 240 | 59 / 81 | 65 / 84 | 57 / 66 | 34 / 63 | 65 / 121 | 24, 116 | 23 |
| chains | 5,000 | 1,596 | 71 / 240 | 45 / 64 | 47 / 80 | 32 / 48 | 38 / 72 | 32 / 42 | 7, 73 | 33 |
| chains | 10,000 | 4,060 | 71 / 240 | 66 / 86 | 65 / 79 | 66 / 66 | 66 / 75 | 65 / 69 | 60, 105 | 12 |

All frame times in milliseconds. "Load all" is 25 to 51 pages of 200 nodes including the redraw after each. Long tasks (over 50 ms) are counted over the whole run including paging; at 10,000 nodes most of them are the merge and layout of the growing graph while paging, not the interactions. Zoom, pan, select and minimap clicks at 10,000 nodes cost about two to four frames each.

## What this does not show

- Nothing was measured on a phone or a throttled CPU; only desktop Chromium on this host.
- The server cannot serve more than 3,000 nodes of a room to the page today, so A is the real ceiling in production and B is what the client could do if that cap were lifted.
- The synthetic graph has one node kind; layout cost with many kinds and long labels was not measured.
- JS heap is `performance.memory.usedJSHeapSize` at the end of one run (garbage collection timing makes it noisy: the 10,000-chain run reports less than the 5,000-chain run).
- Single runs; no confidence interval.
