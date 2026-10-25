// Optional features advertise a capability in /api/v1/health; their nav entry
// carries `capability` and stays hidden until the server advertises it.
export function visibleNav(nav, capabilities) {
  const caps = new Set(Array.isArray(capabilities) ? capabilities : []);
  return nav.filter((entry) => !entry.capability || caps.has(entry.capability))
    .map(({ capability, ...entry }) => entry);
}
