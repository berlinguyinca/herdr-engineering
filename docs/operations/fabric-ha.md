# Fabric gateway failover (active / standby)

One gateway used to be a single point of failure for every `https://dev.lan:<port>` link. Two gateways can now run as a
pair; there is no shared disk or database.

```bash
# host A (either order): starts active when no peer is serving
herdr-eng devfabric gateway --node-id bender --peer http://<host-b-tailnet-ip>:29999 ...
# host B: sees A active and starts as standby
herdr-eng devfabric gateway --node-id beast  --peer http://<host-a-tailnet-ip>:29999 ...
```

* **Standby** mirrors the active gateway's lease table every probe cycle, binds no ports, and answers `503 {"error":"standby"}`
  to register/heartbeat/close. `/healthz` reports `role`, `epoch` and `node_id`.
* **Promotion:** after `--promote-after` (default 3) consecutive missed peer checks the standby bumps the epoch, renews every
  mirrored lease (fresh TTL), and binds the ports. Agents re-register on their next heartbeat, so the mirror only closes the
  gap; correctness does not depend on it.
* **No split brain:** a restarted old primary sees the promoted peer active and rejoins as *standby*. If two gateways are ever
  active at once the higher epoch keeps serving, ties go to the smaller `node_id`, and the loser unbinds its ports.
* **DNS:** set `GATEWAY_PEER_TAILNET_IP` when running `scripts/fabric-stack.sh` to publish both addresses for `dev.lan` and
  `*.dev.lan` (CoreDNS `loadbalance round_robin`). A client tries both; the standby has no listener and refuses, so it falls
  through to the active one.

## What is and is not proven
Automated (in-process, loopback): initial roles, mirroring, write refusal on the standby, promotion only after repeated
misses, a flapping peer resetting the counter, serving a mirrored lease after the active dies, old-primary rejoin, and
double-active resolution.
**Not verified on real hosts:** DNS behaviour when a host is powered off (a dead tailnet address *times out* rather than
refuses, so how fast a client falls through depends on its happy-eyeballs implementation; browsers are quick, `curl` is
slower); `FabricClient` caches its resolved control IP and only re-resolves after an error, so agents may need a couple of
cycles to find the new active; the control-plane dashboard still reads from one configured `fabric.gateway_url`.
The CA, certificate and `--allow-*` policy must be configured identically on both gateways.
