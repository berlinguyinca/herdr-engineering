# Session drift graph in the /summary panes — design

Date 2026-11-04. Status: draft for review. Builds on the rendered summary pane (PR #597: `md_render.py`, `herdr-eng summary view`).

## Intent and why

A long agent session drifts. The person starts on one task ("make a USB stick") and, many prompts later, is designing a web page that should have been its own session. Nothing in the summary shows that. They want one look to answer: what did I start on, where did the topic change, where am I now, and where should I have opened a new session.

The summary becomes two panes: the rendered report (exists), and directly below it a **drift graph** drawn as a real node-and-arrow graph in the terminal.

## Goals

1. A graph pane under the summary pane showing the session as a chain of topic segments, each with start time and the number of the person's prompts it took, and a visible marker where the topic changed and a new session would have been right.
2. Works identically for the four harnesses (Claude, Pi, Codex, OpenCode). Nothing in the graph path may read a harness transcript directly; it uses only the normalized history from `session_history.py`.
3. Correct under resize: both panes reflow to the new width, never overflow it, and never leave a stale or garbled screen.
4. Closing either pane closes both. No orphan panes.
5. Failure is soft: if the graph cannot be built or drawn, the summary is published exactly as today.

## Non-goals

- Mermaid output, raster images, or any terminal graphics protocol. A Herdr pane is not known to pass graphics through, no image tools are installed, and the viewing device often differs from the execution host.
- A dashboard version of the graph. The dashboard keeps showing the report text.
- Drawing the agent tree. Only Claude reports child-agent detail today; Pi and OpenCode report none, Codex a count. Agents appear as a small badge on a segment only where the history reports them.
- Detecting topics without a model. The summarizer that already writes the report proposes the segments.
- Old summaries. Only reports produced after this ships carry a graph.

## Anti-goals (a failure even if it technically works)

- Invented numbers: a time, count or prompt number in the graph that the code did not compute from the history.
- A graph that breaks, overflows or garbles when the pane is narrow or resized.
- Orphan panes, or a new pane stealing focus.
- A confident "you drifted" verdict presented as fact. It is a judgement and is labelled as one.

## How it works

**Data (harness-neutral).** `session_history.read_history` gains `prompts`: every real user prompt as `{n, ts, text}`, kept regardless of the 400-event trim. Today the helper drops the middle of long sessions (this session: 1,100 of 1,500 events), which is exactly where drift happens.
- *What counts as a real prompt.* None of the four readers marks injected content: all emit task notifications, system reminders, hook output and pasted context as ordinary `user` events. A single classifier `is_human_prompt(text)` in `session_history.py` decides, the same for every harness: it rejects empty text, text that begins with a system-style wrapper tag (`<task-notification`, `<system-reminder`, `<environment_context`, `<command-`, `<local-command`) and compaction summaries. It is tested against fixtures from all four harnesses. Prompt numbers `n` are positions among accepted prompts, 1-based.
- *Timestamps.* `ts` is whatever the harness recorded: an ISO string for Claude, Pi and Codex, epoch milliseconds for OpenCode, and it can be missing. One normaliser turns each into a UTC datetime or `None`. A segment whose first prompt has no time is drawn without a time, never with a guessed one.
- *Large sessions.* Every prompt always keeps its `n` and `ts`. Only the text is sampled: above 400 prompts the model is shown the text of the first 50, the last 50 and evenly spaced samples, plus the true total. Segment boundaries may be any `n` from 1 to the true total, and times and counts come from the full list.

**Segments (model proposes, code verifies).** The summary skill, which all four harnesses share, adds one fenced block to the report, `drift-graph`, containing JSON: ordered segments `{label, from_prompt, to_prompt}` plus optional `drift_at` (a prompt number) and `drift_note`. Labels are short (about 40 characters). The model groups and labels; it never supplies times or counts. `herdr-eng summary publish` parses the block and validates it: segments are contiguous, start at prompt 1, stay within the prompt count, 2 to 8 segments, `drift_at` falls on a segment start. Times come from the prompt timestamps and counts from the prompt numbers, both computed by code. A segment's size is `to_prompt - from_prompt + 1`. `drift_at` must be the first prompt of a segment other than the first; a marker that fails this is dropped and the segments are still shown. Any other validation failure drops the graph and publishes the report unchanged.
- *Where the data lives.* `publish` does the parsing, validation and computation once, strips the `drift-graph` block from the report before it is stored, rendered or sent to the dashboard (nobody sees raw JSON), and writes the validated graph, with times and counts attached, to a sidecar `<report>.graph.json` next to the report file. `herdr-eng summary graph` reads only that sidecar; it never touches session history, which keeps Goal 2 true and makes the viewer a pure draw loop.

**Drawing.** `graph_render.py`, standard library only, a pure function `render_graph(graph, width) -> lines`. At wide widths segments sit in one row joined by arrows. When the row would not fit in `width` the chain wraps to a vertical stack with the same arrows turned downward. Box text wraps, then truncates with an ellipsis, never overflows. Width has a floor (about 30 columns); below it the graph degrades to a plain numbered list. The drift marker is a distinct glyph and line under the segment where the topic changed, with the note beneath. `NO_COLOR` is honoured and a plain-ASCII fallback is used when the locale is not UTF-8.

**Panes.** After the summary pane opens, `publish` splits a second pane **down** from it (about 30% height) and runs `herdr-eng summary graph FILE --close-pane ID --peer ID`. The summary viewer gets `--peer` too. When either viewer exits (quit, Ctrl-C, error) it closes its own pane and then the peer, in a `finally`. A re-publish replaces both panes. The graph viewer is a small full-screen loop: draw, wait for a key, `q` closes.

**Resize.** Both viewers handle SIGWINCH.
- Graph: re-read the terminal size, clear, redraw. No other state.
- Summary: the report is pre-wrapped at the width it was opened with, and `less` does not reflow, so today a resize leaves stale wrapping. On SIGWINCH the viewer stops the pager, re-renders at the new width and restarts it at the same relative position (percentage into the document).
- Tests render at widths 30, 40, 60, 80, 110 and 200 and assert no line exceeds the width, the structure survives, and the output is stable. A pty test (standard library `pty` plus `TIOCSWINSZ` and SIGWINCH) runs both viewers, resizes the terminal, and asserts the redraw fits the new width; it runs in CI. A manual check in a real Herdr pane (`herdr pane resize`, `herdr pane read`) is documented as a script, because CI has no Herdr.

## Constraints

- Standard library only, as `md_render.py`.
- Must not slow publishing: the graph is built after the summary pane is open and a failure there is invisible except for a note in the publish JSON.
- The `/summary` skill text is shared by all four harnesses; the new block must be optional so an older skill or a harness that omits it still works.
- No change to what the dashboard stores beyond carrying the same report text.

## Decided by the person

- A real graph, in its own pane directly under the summary pane.
- Drawn with line characters; no images, no Mermaid in the terminal.
- Drift is the most important thing shown; agents and branches are secondary.
- Must be harness-neutral and must resize correctly.

## Herdr primitives this relies on (all exist in herdr 0.9.1, used in the summary-pane work)

`herdr pane split --pane ID --direction down --ratio R --no-focus --cwd PATH`, `herdr pane run`, `herdr pane get` (liveness), `herdr pane close`, `herdr pane send-keys`, `herdr pane read`. `--no-focus` keeps focus on the person's pane. `publish` records both pane ids in sidecar files so a re-publish can find and replace them.

## Left to the builder

- Exact glyphs, colours and box proportions; the narrow-width breakpoints.
- The wording of the skill instruction that asks for the block, within the contract above.
- Exact pane height ratio.
- How the percentage position is restored after a summary reflow (for example `less +N%`).

## Open points I decided (change if wrong)

- The `is_human_prompt` rules above are my call and are the likeliest place for a wrong guess: a missed wrapper tag shifts prompt numbers. The fixtures from four harnesses are the safeguard.
- The graph viewer reads a sidecar written by `publish`, not session history.
- A marker on the first segment is rejected, not shown.
- Segment cap of 8, label length 40, prompt text clip of 120.
- A session with fewer than 5 real prompts gets no graph.
- If the model marks no drift, the graph still shows the segments and says "no drift found".
