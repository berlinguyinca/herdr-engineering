// herdr-web-client — live event stream (SSE) (Spec §52, §85).
// Live updates via SSE, not polling. createEventSource is injected so tests
// can supply a fake; the real browser uses the global EventSource.

export function connectEvents({
  url,
  onEvent = () => {},
  onError = () => {},
  createEventSource = (u) => new EventSource(u),
  autoReconnectMs = 3000,
}) {
  let es = null;
  let closed = false;
  let retryTimer = null;

  function open() {
    if (closed) return;
    es = createEventSource(url);
    es.onmessage = (evt) => {
      let data;
      try {
        data = JSON.parse(evt.data);
      } catch {
        return; // ignore malformed frames; never crash the UI
      }
      onEvent(data);
    };
    es.onerror = () => {
      if (closed) return;
      onError(new Error('event stream disconnected'));
      try {
        es.close();
      } catch { /* noop */ }
      if (autoReconnectMs > 0) {
        retryTimer = setTimeout(open, autoReconnectMs);
      }
    };
  }

  open();

  return function disconnect() {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (es) {
      try { es.close(); } catch { /* noop */ }
    }
  };
}
