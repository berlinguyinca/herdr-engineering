import { escapeHtml as esc } from "./herdr-web-components/util.js";
import { formatTimestamp } from "./herdr-web-components/format.js";
const object = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const statuses = {
  queued: "Delivery unconfirmed",
  delivered: "Delivered",
  failed: "Failed",
  unknown: "Outcome unknown",
};
export function parsePage(body) {
  if (
    !object(body) ||
    !Array.isArray(body.communications) ||
    typeof body.has_more !== "boolean" ||
    (body.next_cursor !== null && typeof body.next_cursor !== "string")
  )
    throw new Error("Invalid communication history");
  const communications = body.communications.map((row) => {
    if (
      !object(row) ||
      typeof row.operation_id !== "string" ||
      typeof row.text !== "string" ||
      typeof row.created_at !== "string" ||
      typeof row.status !== "string" ||
      !Object.hasOwn(statuses, row.status) ||
      !object(row.sender) ||
      !object(row.recipient) ||
      typeof row.truncated !== "boolean" ||
      !["host_observation", "server_receipt"].includes(
        String(row.evidence_kind),
      )
    )
      throw new Error("Invalid communication record");
    for (const party of [row.sender, row.recipient]) {
      for (const field of ["name", "agent", "pane_id", "session_id"]) {
        if (party[field] !== undefined && typeof party[field] !== "string")
          throw new Error("Invalid session identity");
      }
    }
    if (
      row.reported_at !== undefined &&
      (typeof row.reported_at !== "number" ||
        !Number.isFinite(row.reported_at) ||
        row.reported_at < 0 ||
        row.reported_at > 253402300799)
    )
      throw new Error("Invalid reported time");
    if (row.recorded_at !== undefined && typeof row.recorded_at !== "string")
      throw new Error("Invalid record time");
    return row;
  });
  return {
    communications,
    has_more: body.has_more,
    next_cursor: body.next_cursor,
  };
}
function party(value, fallback) {
  const label = esc(value.name || value.agent || value.pane_id || fallback);
  return value.session_id
    ? `<a href="#/herd/session/${esc(encodeURIComponent(value.session_id))}">${label}</a>`
    : label;
}
export function renderCommunications(rows) {
  if (!rows.length)
    return '<p class="hd-empty">No recorded communications yet.</p>';
  return rows
    .map((row) => {
      const reported =
        row.reported_at === undefined
          ? row.created_at
          : new Date(row.reported_at * 1000).toISOString();
      return `<article class="hd-card" data-communication="${esc(row.operation_id)}">
    <p>${party(row.sender, "Unknown sender")} → ${party(row.recipient, "Unknown recipient")} · <strong>${statuses[row.status]}</strong></p>
    <p class="comm-meta">Reported <time datetime="${esc(reported)}">${esc(formatTimestamp(reported))}</time> · ${row.evidence_kind === "server_receipt" ? "Control-plane receipt" : "Host observation"}</p>
    <pre class="comm-message">${esc(row.text)}</pre>${row.truncated ? "<p>Message truncated at the capture limit.</p>" : ""}
    <details><summary>Message reference</summary><code>${esc(row.operation_id)}</code><p>Recorded ${esc(formatTimestamp(row.recorded_at || row.created_at))}</p></details>
  </article>`;
    })
    .join("");
}
export function mountCommunications(container, client) {
  let disposed = false;
  let request = null;
  let cursor = null;
  let hasMore = false;
  let browsingOlder = false;
  let rows = [];
  let signature = "";
  container.innerHTML = `<style>.comm-message{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;max-height:24em;overflow:auto}.comm-meta{color:var(--text-secondary);overflow-wrap:anywhere}.comm-actions{display:flex;gap:12px;margin:12px 0}.comm-history{display:grid;gap:12px}</style>
    <h1>Agent communications</h1>
    <p>Messages routed through HerdR. Delivered means accepted by the native transport; it does not mean read or completed. Harness-private channels are not included.</p>
    <div class="comm-actions"><button class="hd-btn" data-comm-latest>Latest</button><button class="hd-btn" data-comm-older hidden>Older messages</button></div>
    <p data-comm-status role="status" aria-live="polite">Loading communications…</p><section class="comm-history" aria-label="Recorded communications"></section>`;
  const status = container.querySelector("[data-comm-status]");
  const history = container.querySelector(".comm-history");
  const older = container.querySelector("[data-comm-older]");
  async function load(append = false) {
    if (disposed || request || document.hidden) return;
    const controller = new AbortController();
    request = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    older.disabled = true;
    try {
      const query = new URLSearchParams({ limit: "50" });
      if (append && cursor) query.set("before", cursor);
      const page = parsePage(
        await client.request(`/api/v1/communications?${query}`, {
          signal: controller.signal,
        }),
      );
      if (disposed || controller.signal.aborted) return;
      rows = append
        ? [
            ...new Map(
              [...rows, ...page.communications].map((row) => [
                row.operation_id,
                row,
              ]),
            ).values(),
          ]
        : page.communications;
      cursor = page.next_cursor;
      hasMore = page.has_more;
      browsingOlder = append;
      const next = JSON.stringify(rows);
      if (next !== signature) {
        history.innerHTML = renderCommunications(rows);
        signature = next;
      }
      status.textContent = `${rows.length} recorded communication${rows.length === 1 ? "" : "s"}.`;
      older.hidden = !hasMore;
    } catch (error) {
      if (!disposed)
        status.textContent = `Communication history unavailable. ${error instanceof Error ? error.message : "Please retry."}`;
    } finally {
      clearTimeout(timeout);
      request = null;
      older.disabled = false;
    }
  }
  const latest = () => {
    void load();
  };
  const previous = () => {
    if (hasMore) void load(true);
  };
  container
    .querySelector("[data-comm-latest]")
    .addEventListener("click", latest);
  older.addEventListener("click", previous);
  void load();
  return {
    async refresh() {
      if (!browsingOlder) await load();
    },
    dispose() {
      disposed = true;
      request?.abort();
      container
        .querySelector("[data-comm-latest]")
        ?.removeEventListener("click", latest);
      older.removeEventListener("click", previous);
    },
  };
}
