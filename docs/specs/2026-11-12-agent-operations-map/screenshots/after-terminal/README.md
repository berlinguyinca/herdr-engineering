# After: terminal Agent Operations map

Real captures of `herdr-eng agent-ops` rendering the snapshot of a real control-plane server (`agent_ops` enabled,
5 agents seeded through the real ingest path, including context metrics, drift events, and handoffs interrupted by a disconnect (stalled) and a session end (failed); see `capture.py`). Not mock-ups.

- `terminal-120x40.{txt,ansi,png}` full layout; `terminal-80x24.{txt,ansi,png}` compact; `terminal-120x40-replay.*`
  replay scrubbed to event 9 of 19 (`REPLAY`, marker on the timeline, snapshot folded up to that cursor).
- `.txt` is plain text, `.ansi` the raw colored output, `.png` the ANSI drawn in headless Chromium (Menlo 14px) —
  an approximation of a terminal, since glyph widths depend on your font.

Regenerate: `PYTHONPATH=. python capture.py .` then `CHROMIUM=<chrome-headless-shell> node shoot.cjs .`
(Playwright from `/opt/homebrew/lib/node_modules`). Shows the hosts line, `!`/`!!` context badges (amber >= 70%, red >= 90%), `ctx unknown`, `~drift?` (heuristic) tags and STALLED/FAILED handoffs. Pulses are drawn at tick 3 (animated mode).
