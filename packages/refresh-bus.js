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
