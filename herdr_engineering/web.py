"""Private web/mobile control surface (spec 0060).

A small stdlib-based HTTP server that serves a mobile-first responsive UI
(fleet / sessions / activity / tests / CI / dev-services navigation) over the
private tailnet / loopback by default. Reconnect-safe: the API is stateless
over the live adapters and the page re-fetches on reconnect without restarting
work. There is NO public fallback listener (private-by-default).
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import __version__

_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HerdR Engineering</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#0f1420;color:#e6e9f0}
 header{padding:12px 16px;background:#1a2233;display:flex;justify-content:space-between;align-items:center}
 h1{font-size:1.1rem;margin:0}
 nav{display:flex;gap:6px;padding:8px 16px;overflow-x:auto;background:#151d2c}
 nav a{color:#9fb3d1;text-decoration:none;padding:6px 10px;border-radius:6px;font-size:.85rem;white-space:nowrap}
 nav a.active{background:#2b3a55;color:#fff}
 section{padding:12px 16px}
 .card{background:#1a2233;border:1px solid #2a3550;border-radius:8px;padding:12px;margin:8px 0}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}
 .muted{color:#8a93a8;font-size:.85rem}
 .ok{color:#4ade80}.warn{color:#fbbf24}.bad{color:#f87171}
</style></head><body>
<header><h1>HerdR Engineering <span class="muted" id="ver"></span></h1>
<button onclick="refresh()">Refresh</button></header>
<nav>
 <a href="#fleet" class="active" data-s="fleet">Fleet</a>
 <a href="#sessions" data-s="sessions">Sessions</a>
 <a href="#activity" data-s="activity">Activity</a>
 <a href="#dev" data-s="dev">Dev Services</a>
 <a href="#tests" data-s="tests">Tests</a>
 <a href="#ci" data-s="ci">CI</a>
 <a href="#attention" data-s="attention">Attention</a>
</nav>
<main id="main"></main>
<script>
const API={fleet:'/api/fleet',sessions:'/api/sessions',activity:'/api/activity',
 dev:'/api/dev',tests:'/api/tests',ci:'/api/ci',attention:'/api/attention'};
let active='fleet';
function esc(s){return (s||'').replace(/[<>&]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));}
function card(t,body,cls){return '<div class="card '+cls+'"><div>'+esc(t)+'</div><div class="muted">'+body+'</div></div>';}
async function load(){
 document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('active',a.dataset.s===active));
 try{
  const r=await fetch(API[active]); const d=await r.json();
  render(d);
 }catch(e){ document.getElementById('main').innerHTML='<div class="card bad">reconnect needed: '+esc(e)+'</div>'; }
}
function render(d){
 const main=document.getElementById('main');
 if(active==='fleet'){ main.innerHTML='<div class="grid">'+(d.machines||[]).map(m=>card(m.label, m.reachable?'reachable':'unknown', m.reachable?'ok':'warn')).join('')||'<div class="card muted">no machines</div>'+'</div>'; }
 else if(active==='sessions'){ main.innerHTML='<div class="grid">'+(d.sessions||[]).map(s=>card(s.agent,s.status+' · '+esc(s.cwd),'')).join('')+'</div>'; }
 else if(active==='activity'){ main.innerHTML=(d.events||[]).map(e=>card(e.summary, esc(e.occurred_at)+' · '+esc(e.source))).join('')||'<div class="card muted">no activity</div>'; }
 else if(active==='dev'){ main.innerHTML='<div class="grid">'+(d.leases||[]).map(l=>card(l.label||l.id, esc(l.protocol)+' :'+l.external_port+' → '+esc(l.target_host)+':'+l.target_port,'')).join('')+'</div>'; }
 else if(active==='tests'){ main.innerHTML='<div class="card muted">adapters: '+esc((d.adapters||[]).join(', '))+'</div>'; }
 else if(active==='ci'){ main.innerHTML='<div class="card muted">CI provider: '+(d.available===false?'offline (stale view)':'configured')+'</div>'; }
 else if(active==='attention'){ main.innerHTML=(d.items||[]).map(i=>card(i.title, esc(i.summary)+' · '+esc(i.severity),'')).join('')||'<div class="card muted">no attention items</div>'; }
}
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{active=e.target.dataset.s;load();}));
async function refresh(){ await load(); }
document.getElementById('ver').textContent='v'+window.__VER__;
load(); setInterval(load,15000);
</script></body></html>
"""


def _fleet() -> dict[str, Any]:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        machines = [m.to_dict() for m in NativeHerdrAdapter().list_machines()]
    except Exception:
        machines = []
        # degrade gracefully, never block the UI
    return {"machines": machines}


def _sessions() -> dict[str, Any]:
    from .herdr_adapter import NativeHerdrAdapter
    try:
        agents = [a.to_dict() for a in NativeHerdrAdapter().list_agents()]
    except Exception:
        agents = []
    return {"sessions": agents}


def _activity() -> dict[str, Any]:
    from .journal import SessionJournal
    events = [e.to_dict() for e in SessionJournal().all_recent(limit=50)]
    return {"events": events}


def _dev() -> dict[str, Any]:
    from .devfabric import DevServiceRegistry
    leases = [lease.to_dict() for lease in DevServiceRegistry().active_leases()]
    return {"leases": leases}


def _tests() -> dict[str, Any]:
    from .tests import TestExplorer
    return {"adapters": TestExplorer().adapters()}


def _ci() -> dict[str, Any]:
    from .ci import WoodpeckerProvider
    provider = WoodpeckerProvider()
    return {"available": provider.available}


def _attention() -> dict[str, Any]:
    from .attention import AttentionCenter
    return {"items": [i.to_dict() for i in AttentionCenter().open_items()]}


_HANDLERS = {
    "/api/fleet": _fleet,
    "/api/sessions": _sessions,
    "/api/activity": _activity,
    "/api/dev": _dev,
    "/api/tests": _tests,
    "/api/ci": _ci,
    "/api/attention": _attention,
}


def _make_handler() -> type:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            if path == "/":
                html = _INDEX_HTML.replace("window.__VER__", f'"{__version__}"')
                return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            handler = _HANDLERS.get(path)
            if handler is None:
                return self._send(404, b'{"error":"not found"}')
            try:
                payload = json.dumps(handler(), indent=2, sort_keys=True).encode("utf-8")
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8")
            self._send(200, payload)

        def log_message(self, fmt, *args) -> None:
            pass  # private surface: keep quiet

    return Handler


def serve(host: str | None = None, port: int | None = None,
          bind: str | None = None) -> int:
    """Start the private web server. Binds loopback (private) by default."""
    from .config import load_config
    cfg = load_config()
    default_host = cfg.get("web", {}).get("host", "127.0.0.1")
    default_port = int(cfg.get("web", {}).get("port", 8787))
    bind_host = bind or host or default_host
    bind_port = port or default_port
    httpd = ThreadingHTTPServer((bind_host, bind_port), _make_handler())
    print(f"herdr-eng web listening on http://{bind_host}:{bind_port} (private-by-default)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
