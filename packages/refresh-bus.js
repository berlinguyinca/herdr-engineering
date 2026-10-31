// Shared refresh bus: routes SSE events (as hints) to registered views through
// one trailing throttle per view. The first event after idle refreshes at once
// (next tick); further events in the window coalesce into one refresh at its end.
export function createRefreshBus({
  throttleMs = 5000,
  now = () => Date.now(),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (h) => clearTimeout(h),
} = {}) {
  const entries = new Set();
  let paused = false;

  function run(entry, hint) {
    entry.timer = null;
    entry.last = now();
    entry.hint = undefined;
    try { entry.view.refresh(hint); } catch { /* one broken view must not stop the rest */ }
  }

  function cancel(entry) {
    if (entry.timer != null) clearTimer(entry.timer);
    entry.timer = null;
    entry.hint = undefined;
  }

  function schedule(entry, hint) {
    entry.hint = hint;
    if (entry.timer != null) return;
    const wait = Math.max(0, entry.last + throttleMs - now());
    entry.timer = setTimer(() => run(entry, entry.hint), wait);
  }

  return {
    // fresh: the view just loaded its data, so the first event waits out one window.
    register(view, { fresh = false } = {}) {
      const entry = { view, timer: null, last: fresh ? now() : -Infinity, hint: undefined };
      entries.add(entry);
      return () => { cancel(entry); entries.delete(entry); };
    },
    dispatch(event) {
      if (paused || !event) return;
      for (const entry of entries) {
        const interests = entry.view.interests;
        if (Array.isArray(interests) && !interests.includes(event.event_type)) continue;
        schedule(entry, event);
      }
    },
    pause() {
      paused = true;
      for (const entry of entries) cancel(entry);
    },
    resume() {
      if (!paused) return;
      paused = false;
      for (const entry of entries) {
        cancel(entry);
        entry.timer = setTimer(() => run(entry, undefined), 0);
      }
    },
  };
}

// Timed refresh for a view whose data changes without any SSE event (routine host heartbeats are
// memory-only on the server, and a dead host never emits anything): calls fn every ms while the
// tab is visible. Returns the stop function.
export function pollWhileVisible(fn, ms, { setTimer = setInterval, clearTimer = clearInterval, hidden = () => typeof document !== 'undefined' && document.hidden } = {}) {
  const handle = setTimer(() => { if (!hidden()) fn(); }, ms);
  return () => clearTimer(handle);
}

// Independent resource polling with a shared connection budget. A stalled or
// failing panel cannot block healthy panels, or bypass a server retry deadline.
export function createResourcePoller(loaders, {
  apply = () => {}, onError = () => {}, concurrency = 4, intervalMs = 5000,
  timeoutMs = 15000, maxBackoffMs = 60000, now = Date.now, random = Math.random,
  setTimer = setTimeout, clearTimer = clearTimeout,
} = {}) {
  const entries = new Map(Object.keys(loaders).map(key => [key, { key, timer: null, nextAt: 0, failures: 0 }]));
  const queued = new Set(), running = new Map();
  let paused = false, disposed = false;
  function schedule(entry, delay) {
    clearTimer(entry.timer);
    entry.nextAt = now() + delay;
    entry.timer = setTimer(() => { entry.timer = null; enqueue(entry); }, delay);
  }
  function enqueue(entry) {
    if (paused || disposed || running.has(entry.key) || queued.has(entry.key)) return;
    if (entry.nextAt > now()) { schedule(entry, entry.nextAt - now()); return; }
    clearTimer(entry.timer); entry.timer = null;
    queued.add(entry.key); pump();
  }
  function pump() {
    while (!paused && !disposed && running.size < concurrency && queued.size) {
      const key = queued.values().next().value; queued.delete(key);
      void run(entries.get(key));
    }
  }
  async function run(entry) {
    const controller = new AbortController(), startedAt = now();
    const request = { controller, deadline: null };
    running.set(entry.key, request);
    let abort;
    const interrupted = new Promise((_, reject) => {
      abort = () => reject(controller.signal.reason);
      controller.signal.addEventListener('abort', abort, { once: true });
    });
    request.deadline = setTimer(() => controller.abort(new DOMException('Request timed out', 'TimeoutError')), timeoutMs);
    const current = () => !paused && !disposed && running.get(entry.key) === request;
    try {
      const value = await Promise.race([loaders[entry.key](controller.signal), interrupted]);
      if (!current()) return;
      apply(entry.key, value); entry.failures = 0;
      schedule(entry, intervalMs);
    } catch (error) {
      if (!current()) return;
      entry.failures++;
      if (error && typeof error === 'object' && error.elapsedMs == null) error.elapsedMs = now() - startedAt;
      onError(entry.key, error);
      const backoff = Math.min(maxBackoffMs, intervalMs * 2 ** Math.min(entry.failures - 1, 16));
      const delay = Math.round(Math.min(maxBackoffMs, backoff * (.8 + .4 * random())));
      const retryAfter = Number(error?.retryAfterMs);
      schedule(entry, Math.max(delay, Number.isFinite(retryAfter) ? retryAfter : 0));
    } finally {
      clearTimer(request.deadline); controller.signal.removeEventListener('abort', abort);
      if (running.get(entry.key) === request) running.delete(entry.key);
      pump();
    }
  }
  function pause() {
    paused = true; queued.clear();
    for (const entry of entries.values()) {
      clearTimer(entry.timer); entry.timer = null;
      if (!entry.failures) entry.nextAt = 0;
    }
    for (const { controller, deadline } of running.values()) { clearTimer(deadline); controller.abort(); }
    running.clear();
  }
  return {
    refresh() { for (const entry of entries.values()) enqueue(entry); },
    pause,
    resume() { if (disposed) return; paused = false; this.refresh(); },
    dispose() { disposed = true; pause(); },
  };
}

// One mounted view's refresh: fetch with load(), then apply(data) unless the view was disposed
// meanwhile. A refresh requested while one is in flight is not swallowed: it runs once more after
// the current one finishes (any number of mid-flight requests coalesce into that one rerun).
// A failed refresh keeps the page and marks the view's <herdr-freshness> chip (if any) red.
export function liveRefresher(load, apply, chip = () => null) {
  let disposed = false;
  let inflight = null;
  let again = null;
  let nextHint;
  const touch = (ok, message) => { const el = chip(); if (el && typeof el.touch === 'function') el.touch(ok, message); };
  const run = (hint) => {
    inflight = (async () => {
      try {
        const data = await load(hint);
        if (disposed) return;
        apply(data);
        touch(true);
      } catch (error) {
        if (!disposed) touch(false, error?.message || 'refresh failed');
      } finally { inflight = null; }
    })();
    return inflight;
  };
  return {
    applied(data) { apply(data); touch(true); },
    refresh(hint) {
      if (disposed) return Promise.resolve();
      if (!inflight) return run(hint);
      nextHint = hint;
      if (!again) again = inflight.then(() => { again = null; return disposed ? undefined : run(nextHint); });
      return again;
    },
    dispose() { disposed = true; },
    get disposed() { return disposed; },
  };
}
