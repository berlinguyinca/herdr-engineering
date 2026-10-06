# Dev-fabric fleet lifecycle (multi-operator)

How to set up your own dev net, **join** a machine to it, and **leave** it —
so any operator of this repo can stand up a private fleet and add/remove
machines with a couple of commands. Every script here is **idempotent**
(safe to re-run) and **private-by-default** (no public listeners, no secrets
in the repo).

The fleet has two roles:

- **Master / gateway host** — one machine runs the whole front door as a
  Docker Compose stack: CoreDNS (authoritative for the shared `dev.lan`
  domain), the fabric gateway (`dev:<port>` registry + router + probe loop),
  and the private web UI. Only one machine runs this.
- **Fleet hosts** — every other machine (including the master) runs a
  lightweight **auto-registration agent** that watches for web services
  starting locally and registers them with the gateway, so they appear at a
  stable `http://dev.lan:<port>` on every tailnet device with zero manual
  steps. No changes to your apps.

```
 any tailnet device (desktop / phone / iPad)
      │  http://dev.lan:18052
      ▼
 master host:  CoreDNS + fabric gateway + web UI   (deploy/fabric-stack, Docker Compose)
      │  http://dev.lan:29999  (control API)
      ▼
 fleet hosts (incl. master):  auto-registration agent  (systemd on Linux / launchd on macOS)
      └─ detects local web servers -> registers -> gateway proxies to them
```

---

## 0. Prerequisites (on every machine)

- The Tailscale client, logged in to **the same private tailnet**.
- This repository checked out.
- Linux hosts additionally need `systemd` user sessions and, on the master
  host, **Docker with Compose v2**.

---

## 1. Set up your local dev net (once, on the master host)

Pick one machine to be the master. From its checkout:

```bash
# 1) install herdr-engineering + enroll this machine
scripts/install.sh

# 2) bring up the whole front door as a Compose project
scripts/fabric-stack.sh up-replace   # builds image, renders CoreDNS, starts stack
scripts/fabric-stack.sh status       # verify gateway healthz + dev.lan resolution
```

`up-replace` also stops any previously host-run gateway process and the
`herdr-eng-web` systemd unit that would collide on the same ports.

### One external admin step (cannot be automated from the host)

`dev.lan` is not a Tailscale MagicDNS name — it is served by our own CoreDNS,
and Tailscale must be told to forward `dev.lan` queries there. In the
**Tailscale admin console → DNS → Nameservers**:

1. **Add nameserver** → the master host's tailnet IPv4
   (`tailscale ip -4` on the master, e.g. `100.104.39.6`).
2. Enable **"Restrict to domain"** and set it to **`dev.lan`**.
3. Save.

Tailscale then sends only `*.dev.lan` queries to CoreDNS; every other name
keeps using MagicDNS. Devices that accept Tailscale DNS (desktop, macOS, iOS,
Android) resolve `dev.lan` fleet-wide. Verify with `tailscale dns status`
(Split DNS Routes should show `dev.lan -> <master-ip>`).

> Why `dev.lan`? `.local` is the mDNS/Bonjour-reserved namespace (RFC 6762):
> Tailscale MagicDNS will not serve it and the admin console refuses it as a
> custom namespace. `dev.lan` is not reserved, so Tailscale accepts it.

---

## 2. Join a machine (fleet host)

On the machine you want to join, from its checkout, install herdr-engineering
and start the auto-registration agent. The gateway URL defaults to
`http://dev.lan:29999`; override with `--gateway <url>` if your fleet uses a
different master.

### Linux

```bash
scripts/install.sh
scripts/fabric-agent.sh --gateway http://dev.lan:29999   # systemd user unit + linger
```

### macOS

macOS has no systemd and its system Python is 3.9 (herdr-engineering needs
≥ 3.12), so `fabric-agent-macos.sh` installs `uv` (self-contained), a
Python 3.12 venv, the package, the `tailscale` CLI on PATH, and a launchd
LaunchAgent — no sudo / Homebrew required:

```bash
scripts/install.sh
scripts/fabric-agent-macos.sh --gateway http://dev.lan:29999   # launchd LaunchAgent
```

### Verify a joined machine

```bash
# the agent is running
scripts/fabric-agent.sh status                 # Linux:  systemctl --user status ...
scripts/fabric-agent-macos.sh status           # macOS:  launchctl print gui/<uid>/...

# its services appear in the fleet registry
herdr-eng devfabric list --json

# from any device on the tailnet, hit one of its advertised dev:<port> URLs
curl http://dev.lan:<port>
```

The agent logs its registrations (Linux:
`journalctl --user -u herdr-eng-fabric-agent.service`; macOS:
`tail ~/.local/share/herdr-engineering/fabric-agent.log`).

---

## 3. Leave a machine (offboard)

To remove a machine from the fleet cleanly — stops its agent, closes its
leases on the gateway (ports free immediately; anything missed expires via the
gateway's probe loop within ~90s), and drops it from the fleet inventory:

```bash
scripts/fabric-offboard.sh
#   --host <name>      offboard a different host (default: this machine)
#   --leave-tailnet    also `tailscale logout` (removes the device from the tailnet)
#   --purge            also remove machine-local agent config + state
```

Run it **on** the machine being removed (it detects Linux vs macOS). To remove
a machine that is already gone, run it from any checkout with
`--host <that-host>`.

---

## Tear down the whole dev net

To stop the master stack and the agent on every machine:

```bash
# on the master host
scripts/fabric-stack.sh down

# on each fleet host
scripts/fabric-offboard.sh --leave-tailnet --purge
```

The CoreDNS + gateway containers are removed; the `herdr-state` named volume
(leases) can be removed with `docker compose -f deploy/fabric-stack/compose.yaml down -v`.

---

## Reference: what each script does

| Script | Platform | Role |
|---|---|---|
| `scripts/install.sh` | Linux + macOS | Install herdr-engineering, enroll the machine in the fleet inventory, join the tailnet. |
| `scripts/fabric-stack.sh` | master host | Deploy the CoreDNS + gateway + web Compose stack. Subcommands: `up`, `up-replace`, `down`, `status`, `stop-old`. |
| `scripts/fabric-agent.sh` | Linux | Install + start the auto-registration agent as a systemd **user** unit (linger). |
| `scripts/fabric-agent-macos.sh` | macOS | Install + start the auto-registration agent as a **launchd LaunchAgent** (uv + Python 3.12). Subcommands: `install`, `status`, `uninstall [--purge]`. |
| `scripts/fabric-offboard.sh` | Linux + macOS | Stop the agent, close this host's leases, remove it from the fleet inventory. Flags: `--host`, `--gateway`, `--leave-tailnet`, `--purge`. |

## Design notes / invariants

- **Private by default.** The gateway router, control API, agent forwarders,
  and web UI all bind loopback / tailnet interfaces only — never `0.0.0.0`,
  never a public address.
- **No secrets in the repo.** Control-API bearer tokens live only in the
  environment or a machine-local gitignored file. `fabric.env` holds a tailnet
  URL, not a credential.
- **The agent is never containerized.** It must see host listeners via
  `ss`/`lsof`, so it stays a host service on every fleet host.
- **Leases self-heal.** A stopped or crashed host's leases expire via the
  gateway's probe loop and its `dev:<port>` ports are reused; a restarted
  agent re-registers them.
