// In-page panels that depend on an optional service ask the server whether it is enabled: /api/v1/health?probe=1 advertises
// a capability only while the service's routes work. A panel is retired only when the probe answered WITHOUT its capability;
// a failed probe says nothing, so a transient error never hides a working panel (the panel reports its own errors).
const PROBE_TTL_MS = 30000;
const probes = new WeakMap();

function probe(client) {
  const cached = probes.get(client);
  if (cached && Date.now() - cached.at < PROBE_TTL_MS) return cached.promise;
  const promise = Promise.resolve()
    .then(() => client.health({ probe: true }))
    .then((health) => (Array.isArray(health?.capabilities) ? health.capabilities : null), () => null);
  probes.set(client, { at: Date.now(), promise });
  return promise;
}

export async function serviceOff(client, capability) {
  const capabilities = await probe(client);
  return capabilities !== null && !capabilities.includes(capability);
}

export async function retireWhenOff(client, capability, ...elements) {
  const off = await serviceOff(client, capability);
  if (off) for (const element of elements) element?.remove();
  return off;
}
