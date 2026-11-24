# Fabric agent: which local ports are published

`herdr-eng devfabric agent` scans this host's listening ports, registers each
web service it finds with the fabric gateway, and puts a tailnet-bound
forwarder in front of loopback-only ones. The forwarder rewrites a `Host`
header naming the fabric to `127.0.0.1:<port>` so the application's own
DNS-rebinding guard accepts it. That makes adoption a security decision:
whatever is adopted becomes reachable from the tailnet.

## What is never adopted

A port is refused when the checks below match. Ports that were never candidates
(the port denylist, exclusions, privileged and ephemeral ports, the gateway's own
range, ignored and denied processes) are skipped silently. A candidate refused by
the allowlist or an identity probe gets one log line naming the reason
(`refusing to publish port N: ...`, once per port and reason).

The table:

| Check | How it is detected |
|---|---|
| Debugger, database, admin console | Port is in the built-in denylist (`DEFAULT_DENY_PORTS` in `herdr_engineering/fabric_agent.py`): Chrome DevTools 9222/9223, Node inspector 9229/9230/5858, chromedriver 9515, Selenium 4444, Docker API 2375/2376, etcd 2379, Consul 8500, Vault 8200, Kibana 5601, RabbitMQ management 15672, CouchDB 5984, Neo4j 7474, InfluxDB 8086, plus the earlier infrastructure ports. |
| Chrome DevTools Protocol on any port | `GET /json/version` returns JSON with `webSocketDebuggerUrl`. |
| Node inspector on any port | `GET /json/version` returns a `Browser` starting with `node`. |
| Jupyter (server, notebook, lab) on any port (heuristic) | `GET /api` returns JSON whose only key is `version`, and either the `Server` header names Jupyter or Tornado, or `GET /api/status` returns JSON with `started` or `last_activity`. An ordinary app's `/api` version endpoint is not refused. |
| Operator exclusion | Port is in `fabric.agent_exclude_ports`. |
| Not allowed (allowlist mode only) | See below. |

The identity probes run once per stable listener (cached 30 s) with a timeout of at
most 0.5 s, after the port already answered `GET /` as HTTP. A probe that cannot reach the port (timeout, connection error) is
not a pass: the port is not adopted, nothing is cached, one INFO line says so, and
the next scan asks again. A 404 or non-JSON reply from a live HTTP server is an
answer and counts as "not a debugger". A refused port gets
no lease, no forwarder and no `Host` rewrite. A port listening on the host but
unpublished for one of these reasons is not repeated as a "no fabric lease" warning.
An existing lease whose port becomes denied, excluded or not allowed is released on
the next scan. The identity probes also cover stored leases, including those adopted
by an older agent: every scan re-asks for each leased port, cached for 30 seconds per
listener identity (addresses, process, pid), so a replaced listener or a service that
starts answering as a debugger loses its lease and forwarder within one scan. An
inconclusive probe never releases a lease; it is retried next scan. After an agent
restart, no forwarder for a stored loopback lease is started until the first scan has
probed that port and found it not a debugger; a conclusive refusal releases the lease
instead.

## Settings (`config.yaml`, `fabric:` section)

```yaml
fabric:
  agent_exclude_ports: [8897]        # extra denied ports (always wins)
  agent_adopt_mode: denylist         # default; "allowlist" = opt-in
  agent_allow_ports: [3000, 5173]    # allowlist mode only
  agent_allow_processes: [vite]      # word-start match on the process name
```

The default is `denylist`: adopt every web port that is not refused above. To
publish only what you name, set one line, `agent_adopt_mode: allowlist`, and list
the ports or processes in `agent_allow_ports` / `agent_allow_processes`. With
nothing listed, nothing is adopted. Allowing a port does not override the
denylist, the exclusion list or the debugger probes. Restart the agent after
changing these settings.

`agent_allow_processes` does two things. In allowlist mode it names the processes to
publish. In both modes it overrides the built-in process denylist
(`DEFAULT_DENY_PROCESSES`: vite, vitest, jest, playwright, next-server, ...) for the
processes you name, because naming one is explicit intent: the example above
publishes a `vite` dev server that would otherwise be skipped as a test runner.
The match is on the start of a word, so `vite` also frees `vitest`; name the full
process (`vitest`) to be exact. It never overrides the port denylist, `agent_exclude_ports` or the
debugger probes, and it does not touch a list you set yourself in
`agent_deny_processes`.
