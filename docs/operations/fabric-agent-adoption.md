# Fabric agent: which local ports are published

`herdr-eng devfabric agent` scans this host's listening ports, registers each
web service it finds with the fabric gateway, and puts a tailnet-bound
forwarder in front of loopback-only ones. The forwarder rewrites a `Host`
header naming the fabric to `127.0.0.1:<port>` so the application's own
DNS-rebinding guard accepts it. That makes adoption a security decision:
whatever is adopted becomes reachable from the tailnet.

## What is never adopted

A port is refused, with one log line naming the reason
(`refusing to publish port N: ...`, logged once per port and reason), when:

| Check | How it is detected |
|---|---|
| Debugger, database, admin console | Port is in the built-in denylist (`DEFAULT_DENY_PORTS` in `herdr_engineering/fabric_agent.py`): Chrome DevTools 9222/9223, Node inspector 9229/9230/5858, chromedriver 9515, Selenium 4444, Docker API 2375/2376, etcd 2379, Consul 8500, Vault 8200, Kibana 5601, RabbitMQ management 15672, CouchDB 5984, Neo4j 7474, InfluxDB 8086, plus the earlier infrastructure ports. |
| Chrome DevTools Protocol on any port | `GET /json/version` returns JSON with `webSocketDebuggerUrl`. |
| Node inspector on any port | `GET /json/version` returns a `Browser` starting with `node`. |
| Jupyter (server, notebook, lab) on any port | `GET /api` returns JSON whose only key is `version`. |
| Operator exclusion | Port is in `fabric.agent_exclude_ports`. |
| Not allowed (allowlist mode only) | See below. |

The identity probes run once per stable listener (cached 30 s) with a timeout of at
most 0.5 s, after the port already answered `GET /` as HTTP. A refused port gets
no lease, no forwarder and no `Host` rewrite. A port listening on the host but
unpublished for one of these reasons is not repeated as a "no fabric lease" warning.
An existing lease whose port becomes denied, excluded or not allowed is released on
the next scan. The identity probes are made only when a port is first considered,
so a service that starts answering as a debugger later keeps its lease until the
listener changes or the agent restarts.

## Settings (`config.yaml`, `fabric:` section)

```yaml
fabric:
  agent_exclude_ports: [8897]        # extra denied ports (always wins)
  agent_adopt_mode: denylist         # default; "allowlist" = opt-in
  agent_allow_ports: [3000, 5173]    # allowlist mode only
  agent_allow_processes: [vite]      # allowlist mode only; word-start match on the process name
```

The default is `denylist`: adopt every web port that is not refused above. To
publish only what you name, set one line, `agent_adopt_mode: allowlist`, and list
the ports or processes in `agent_allow_ports` / `agent_allow_processes`. With
nothing listed, nothing is adopted. Allowing a port does not override the
denylist, the exclusion list or the debugger probes. Restart the agent after
changing these settings.
