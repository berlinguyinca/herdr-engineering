// Browser notifications for the attention inbox (design 2026-10-24, "Notifications").
// Policy comes from the server's YAML (`notify` in the inbox answer); this module only applies it:
//  - open items at or above min_severity, one notification per (key, fingerprint), so a changed
//    condition notifies again and an acknowledge elsewhere stops it on the next poll;
//  - quiet hours (this browser's local clock) hold notifications back; held items notify when the hours end
//    if they are still open;
//  - items already open when notifications are switched on, or when a tab starts, are the baseline and do
//    not notify, so opening the dashboard never produces a burst;
//  - the payload is the item title and a click that opens #/attention. No detail text, no log text.
const RANK = { critical: 0, warning: 1, info: 2 };
const SEEN_KEY = 'herdr.attention.notified';
const ENABLED_KEY = 'herdr.attention.notify';
const SEEN_LIMIT = 500;
const GROUP_OVER = 3;

export const itemId = (item) => `${item.key}|${item.fingerprint}`;

/** True while `date`'s local hour is inside quiet hours; start > end wraps midnight, start === end is never quiet. */
export function quietNow(quiet, date = new Date()) {
  if (!quiet || !Number.isInteger(quiet.start) || !Number.isInteger(quiet.end) || quiet.start === quiet.end) return false;
  const hour = date.getHours();
  return quiet.start < quiet.end ? hour >= quiet.start && hour < quiet.end : hour >= quiet.start || hour < quiet.end;
}

/**
 * Pure decision for one inbox answer.
 * Returns {notifications: [item], seenAdd: [id]}: items to announce now and ids to remember as handled.
 */
export function planNotifications({ items, notify, seen, date = new Date(), baseline = false }) {
  const floor = RANK[notify?.min_severity] ?? RANK.critical;
  const eligible = (Array.isArray(items) ? items : []).filter((item) => item && item.state === 'open'
    && typeof item.key === 'string' && typeof item.fingerprint === 'string'
    && (RANK[item.severity] ?? RANK.info) <= floor && !seen.has(itemId(item)));
  if (baseline) return { notifications: [], seenAdd: eligible.map(itemId) };
  if (quietNow(notify?.quiet_hours, date)) return { notifications: [], seenAdd: [] };
  return { notifications: eligible, seenAdd: eligible.map(itemId) };
}

function readSeen(storage) {
  try {
    const parsed = JSON.parse(storage?.getItem(SEEN_KEY) || '[]');
    return new Set(Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : []);
  } catch { return new Set(); }
}

function writeSeen(storage, seen) {
  try { storage?.setItem(SEEN_KEY, JSON.stringify([...seen].slice(-SEEN_LIMIT))); } catch { /* Dedupe then lasts for this page only. */ }
}

/**
 * The browser side: remembers the operator's choice, asks for permission on a click, and announces.
 * `NotificationImpl` and `storage` are injected so the behaviour is testable without a browser.
 */
export function createAttentionNotifier({ storage, NotificationImpl, onClick = () => {}, now = () => new Date() } = {}) {
  let baselined = false;
  let serverAllows = true;
  let on = false;
  try { on = storage?.getItem(ENABLED_KEY) === 'on'; } catch { on = false; }
  const supported = () => typeof NotificationImpl === 'function';
  const permission = () => (supported() ? NotificationImpl.permission : 'denied');

  return {
    supported,
    /** 'unsupported' | 'server-off' | 'blocked' | 'off' | 'on' */
    status() {
      if (!supported()) return 'unsupported';
      if (!serverAllows) return 'server-off';
      if (permission() === 'denied') return 'blocked';
      return on && permission() === 'granted' ? 'on' : 'off';
    },
    /** True when a poll should keep running in a hidden tab so a notification can still fire. */
    active() { return supported() && serverAllows && on && permission() === 'granted'; },
    async enable() {
      if (!supported() || !serverAllows) return this.status();
      if (permission() === 'default') {
        try { await NotificationImpl.requestPermission(); } catch { /* Treated as not granted below. */ }
      }
      if (permission() === 'granted') {
        on = true; baselined = false;
        try { storage?.setItem(ENABLED_KEY, 'on'); } catch { /* This page only. */ }
      }
      return this.status();
    },
    disable() {
      on = false;
      try { storage?.setItem(ENABLED_KEY, 'off'); } catch { /* This page only. */ }
      return this.status();
    },
    /** Apply one inbox answer; returns the number of notifications shown. */
    handle(body) {
      if (body?.notify && typeof body.notify.browser === 'boolean') serverAllows = body.notify.browser;
      if (!this.active() || !Array.isArray(body?.items)) return 0;
      const seen = readSeen(storage);      // re-read: another tab of this browser may have announced already
      const plan = planNotifications({ items: body.items, notify: body.notify, seen, date: now(), baseline: !baselined });
      baselined = true;
      for (const id of plan.seenAdd) seen.add(id);
      if (plan.seenAdd.length) writeSeen(storage, seen);
      const show = (title, tag) => {
        try {
          const n = new NotificationImpl(title, { body: '', tag, data: { href: '#/attention' } });
          n.onclick = () => { try { n.close?.(); } catch { /* Already closed. */ } onClick('#/attention'); };
        } catch { /* The browser refused; the inbox badge still shows the item. */ }
      };
      if (plan.notifications.length > GROUP_OVER) show(`${plan.notifications.length} new attention items`, 'attention-group');
      else for (const item of plan.notifications) show(item.title, itemId(item));
      return plan.notifications.length > GROUP_OVER ? 1 : plan.notifications.length;
    },
  };
}
