// { [mission_id]: title } so activity and session text can show a mission's name instead of its id. Best-effort: a failed
// load is never cached (the next call retries, and the last good titles keep being served), and a good load is reused for
// ttlMs so missions created later in a long-lived dashboard tab still resolve.
export function createMissionTitles(list, { ttlMs = 60000, now = Date.now } = {}) {
  let cache = null;
  let loadedAt = 0;
  let inflight = null;
  const load = async () => {
    try {
      const { missions } = await list();
      cache = {};
      for (const m of missions) cache[m.mission_id] = m.title;
      loadedAt = now();
    } catch { /* keep the last good titles; loadedAt stays so the next call retries */ }
    return cache || {};
  };
  return async function missionTitles() {
    if (cache && now() - loadedAt < ttlMs) return cache;
    inflight ||= load().finally(() => { inflight = null; });
    return inflight;
  };
}
