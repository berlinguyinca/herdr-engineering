// herdr-web-client — live event stream (SSE) (Spec §52, §85).
// Live updates via SSE, not polling. createEventSource is injected so tests
// can supply a fake; the real browser uses the global EventSource.

const SEEN_LIMIT = 500;
const ENTITY_LIMIT = 2000;

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
  // Page-lifetime dedupe so reconnect replays are not delivered twice.
  const seenIds = new Set();
  const lastSeq = new Map();

  function isDuplicate(data) {
    if (!data || typeof data !== 'object') return false;
    if (data.event_id != null) {
      if (seenIds.has(data.event_id)) return true;
      seenIds.add(data.event_id);
      if (seenIds.size > SEEN_LIMIT) seenIds.delete(seenIds.values().next().value);
    }
    if (data.entity_type != null && data.entity_id != null && Number.isFinite(data.sequence)) {
      const key = `${data.entity_type}:${data.entity_id}`;
      const last = lastSeq.get(key);
      if (last !== undefined && data.sequence <= last) return true;
      lastSeq.delete(key);
      lastSeq.set(key, data.sequence);
      if (lastSeq.size > ENTITY_LIMIT) lastSeq.delete(lastSeq.keys().next().value);
    }
    return false;
  }

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
      if (data && data.event_type === 'StreamConnected') return;
      if (isDuplicate(data)) return;
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
