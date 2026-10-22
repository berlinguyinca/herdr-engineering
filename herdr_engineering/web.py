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
 .tree{list-style:none;padding:0;margin:0;font-family:ui-monospace,monospace;font-size:.85rem}
 .tree ul{list-style:none;padding-left:0;margin:0}
 .kids{display:none}
 .node.open>.kids{display:block}
 .nrow{display:flex;gap:8px;align-items:center;padding:4px 8px;border-radius:6px;cursor:pointer}
 .nrow:hover{background:#1f2b42}
 .car{display:inline-block;width:12px;color:#5a6b8c}
 .car.empty{width:12px}
 .leaf .lbl{color:#9fb3d1}
 .lbl{color:#e6e9f0}
 .link{color:#7aa2f7;text-decoration:none}
 .dt{border-collapse:collapse;width:100%;font-size:.85rem}
 .dt td{border:1px solid #2a3550;padding:4px 8px;vertical-align:top}
 .dt td:first-child{color:#8a93a8;width:30%}
 .dt h4{margin:.5rem 0}
</style></head><body>
<header><h1>HerdR Engineering <span class="muted" id="ver"></span></h1>
<button onclick="refresh()">Refresh</button></header>
<nav>
 <a href="#tree" class="active" data-s="tree">Tree</a>
 <a href="#fleet" data-s="fleet">Fleet</a>
 <a href="#sessions" data-s="sessions">Sessions</a>
 <a href="#activity" data-s="activity">Activity</a>
 <a href="#dev" data-s="dev">Dev Services</a>
 <a href="#tests" data-s="tests">Tests</a>
 <a href="#ci" data-s="ci">CI</a>
 <a href="#attention" data-s="attention">Attention</a>
 <a href="#agentmine" data-s="agentmine">Agent Mine</a>
</nav>
<main id="main"></main>
<script>
const API={tree:'/api/tree',fleet:'/api/fleet',sessions:'/api/sessions',activity:'/api/activity',
 dev:'/api/dev',tests:'/api/tests',ci:'/api/ci',attention:'/api/attention',agentmine:'/api/agent-mine'};
let active='tree';
function treeHtml(node,depth){
 if(!node)return ''; depth=depth||0;
 const kids=(node.children||[]).map(c=>treeHtml(c,depth+1)).join('');
 const hasKids=!!(node.children&&node.children.length);
 const leaf=hasKids?'':' leaf';
 const dot=(node.status==='running')?'<span class="ok">\u25cf</span> ':(node.status?'<span class="warn">\u25cb</span> ':'');
 const url=node.url?'<a href="'+esc(node.url)+'" target="_blank" rel="noopener" class="link">open</a> ':'';
 return '<li class="node'+leaf+'" data-id="'+esc(node.id)+'">'+
  '<div class="nrow"><span style="padding-left:'+(depth*14)+'px">'+
  (hasKids?'<span class="car">\u25b8</span>':'<span class="car empty"></span>')+
  dot+'<span class="lbl">'+esc(node.label)+'</span></span>'+
  (node.subtitle?'<span class="sub muted">'+esc(node.subtitle)+'</span>':'')+
  url+'</div>'+(kids?'<ul class="kids">'+kids+'</ul>':'')+'</li>';
}
function renderTree(d){
 const root=d.root||{};
 const html='<div class="card muted">generated '+esc(d.generated_at||'')+' · <b>'+d.counts.machines+'</b> machines · <b>'+d.counts.workspaces+'</b> workspaces · <b>'+d.counts.dev_services+'</b> dev services · <b>'+d.counts.processes+'</b> live processes</div>'
  +'<div id="detail" class="card" hidden></div>'
  +'<ul class="tree" id="tree">'+treeHtml(root)+'</ul>';
 document.getElementById('main').innerHTML=html;
 document.querySelectorAll('.has-kids>.nrow').forEach(r=>r.addEventListener('click',e=>{
  const li=r.parentElement; const kids=li.querySelector('.kids');
  const open=li.classList.toggle('open');
  kids.style.display=open?'':'none';
  r.querySelector('.car').textContent=open?'\u25be':'\u25b8';
  e.stopPropagation();
 }));
 document.querySelectorAll('.leaf>.nrow').forEach(r=>r.addEventListener('click',async e=>{
  const id=r.parentElement.dataset.id;
  try{
   const rr=await fetch('/api/node?id='+encodeURIComponent(id));
   showDetail(await rr.json());
  }catch(err){ showDetail({error:String(err)}); }
  e.stopPropagation();
 }));
}
function showDetail(dd){
 const el=document.getElementById('detail');
 if(!el)return;
 el.hidden=false;
 const rows=Object.entries(dd||{}).filter(([k])=>!['id','kind','activity','dev','children','subtitle'].includes(k))
  .map(([k,v])=>'<tr><td class="muted">'+esc(k)+'</td><td>'+esc(typeof v==='object'?JSON.stringify(v):v)+'</td></tr>').join('');
 let extra='';
 if(dd.activity&&dd.activity.length){ extra+='<h4>Activity</h4><ul>'+(dd.activity.map(a=>'<li><span class="muted">'+esc(a.occurred_at||'')+'</span> '+esc(a.summary||'')+'</li>').join(''))+'</ul>'; }
 if(dd.dev&&dd.dev.length){ extra+='<h4>Dev services</h4><ul>'+(dd.dev.map(l=>'<li><a class="link" href="'+esc(l.url||'#')+'" target="_blank">'+esc(l.label||l.id)+'</a> · '+esc(l.target||'')+'</li>').join(''))+'</ul>'; }
 el.innerHTML='<h4>'+esc(dd.label||dd.id||'detail')+'</h4><table class="dt">'+rows+'</table>'+extra+'<div class="muted">'+esc(dd.error||'')+'</div>';
}
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
 if(active==='tree'){ return renderTree(d); }
 if(active==='fleet'){ main.innerHTML='<div class="grid">'+(d.machines||[]).map(m=>card(m.label, m.reachable?'reachable':'unknown', m.reachable?'ok':'warn')).join('')||'<div class="card muted">no machines</div>'+'</div>'; }
 else if(active==='sessions'){ main.innerHTML='<div class="grid">'+(d.sessions||[]).map(s=>card(s.agent,s.status+' · '+esc(s.cwd),'')).join('')+'</div>'; }
 else if(active==='activity'){ main.innerHTML=(d.events||[]).map(e=>card(e.summary, esc(e.occurred_at)+' · '+esc(e.source))).join('')||'<div class="card muted">no activity</div>'; }
 else if(active==='dev'){ main.innerHTML='<div class="grid">'+(d.leases||[]).map(l=>{ const sub=esc(l.protocol)+' :'+l.external_port+' → '+esc(l.target_host)+':'+l.target_port+(l.url?'<br><a href="'+esc(l.url)+'" target="_blank" rel="noopener">'+esc(l.url)+'</a>':''); return card(l.label||l.id, sub,''); }).join('')+'</div>'; }
 else if(active==='tests'){ main.innerHTML='<div class="card muted">adapters: '+esc((d.adapters||[]).join(', '))+'</div>'; }
 else if(active==='ci'){ main.innerHTML='<div class="card muted">CI provider: '+(d.available===false?'offline (stale view)':'configured')+'</div>'; }
 else if(active==='attention'){ main.innerHTML=(d.items||[]).map(i=>card(i.title, esc(i.summary)+' · '+esc(i.severity),'')).join('')||'<div class="card muted">no attention items</div>'; }
 else if(active==='agentmine'){
   const f=d.fleet||{};
   const siteCards=(d.sites||[]).map(s=>{
     const h=s.hpc_ssh||{};
     const sub='adoption since install <b>'+(h.adoption_since_pct??0)+'%</b> · '+(h.hpc_ssh_since||0)+' hpc / '+(h.hand_ssh_hpc_since||0)+' hand<br>45d window: '+(h.adoption_pct??0)+'% · '+(h.hpc_ssh||0)+' hpc / '+(h.hand_ssh_hpc||0)+' hand · '+(((s.totals&&s.totals.events)||0))+' events';
     const a=h.adoption_since_pct??0; return card(s.site||'?', sub, a>=50?'ok':(a>=25?'warn':'bad'));
   }).join('');
   main.innerHTML='<div class="card">'+(f.sites||0)+' sites · '+(f.events||0)+' events · fleet adoption since install <b>'+(f.adoption_since_pct??0)+'%</b> ('+(f.hpc_ssh_since||0)+' hpc / '+(f.hand_ssh_hpc_since||0)+' hand) · generated '+esc(d.generated_at||'')+'</div>'+(siteCards||'<div class="card muted">no per-site summaries yet — run ansible playbooks/agent-mine.yml</div>');
 }
}
document.querySelectorAll('nav a').forEach(a=>a.addEventListener('click',e=>{active=e.target.dataset.s;load();}));
async function refresh(){ await load(); }
document.getElementById('ver').textContent='v'+window.__VER__;
load(); setInterval(load,15000);
</script></body></html>
"""


def _tree() -> dict[str, Any]:
    from .tree import build_tree
    try:
        return build_tree()
    except Exception as exc:
        return {"error": str(exc)}


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
    from .config import InvalidError, load_config
    from .devfabric import DevServiceRegistry
    from .errors import HerdrEngineeringError
    from .fabric import FabricClient, gateway_url_from_config
    try:
        cfg = load_config()
    except InvalidError:
        cfg = {}
    leases = [lease.to_dict() for lease in DevServiceRegistry().active_leases()]
    # Annotate each lease with its stable dev:<port> URL so the UI shows a
    # clickable front-door link. url_base comes from the LIVE gateway
    # (/healthz is the single source of truth); fall back to config when no
    # gateway is reachable (local mode).
    url_base = ((cfg.get("fabric") or {}).get("url_base") or "").strip()
    base = gateway_url_from_config(cfg)
    if base:
        try:
            live = FabricClient(base).healthz().get("url_base")
            if live:
                url_base = live
        except (HerdrEngineeringError, OSError):
            pass
    if url_base:
        for lease in leases:
            if lease.get("external_port") is not None:
                lease["url"] = f"http://{url_base}:{lease['external_port']}"
    return {"leases": leases}


def _tests() -> dict[str, Any]:
    from .tests import TestExplorer
    return {"adapters": TestExplorer().adapters()}


def _ci() -> dict[str, Any]:
    from .ci import WoodpeckerProvider
    provider = WoodpeckerProvider()
    return {"available": provider.available}


def _agent_mine() -> dict[str, Any]:
    """Fleet agent-transcript mining roll-up (HPC-ssh adoption dashboard).

    Reads the per-site REDACTED JSON summaries that the ansible fan-out playbook
    (playbooks/agent-mine.yml) writes to ~/.cache/agent-mine/fleet/<host>.json
    and rolls them into a fleet-wide view. Only aggregate counts are present;
    transcripts never leave a site.
    """
    import glob
    import os
    import time
    from .agent_mine import fleet_rollup
    fleet_dir = os.path.join(os.path.expanduser("~"), ".cache", "agent-mine", "fleet")
    sites = []
    for path in sorted(glob.glob(os.path.join(fleet_dir, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data.setdefault("site", os.path.basename(path)[:-len(".json")])
            sites.append(data)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fleet": fleet_rollup(sites),
        "sites": sites,
    }


def _attention() -> dict[str, Any]:
    from .attention import AttentionCenter
    return {"items": [i.to_dict() for i in AttentionCenter().open_items()]}


_HANDLERS = {
    "/api/tree": _tree,
    "/api/fleet": _fleet,
    "/api/sessions": _sessions,
    "/api/activity": _activity,
    "/api/dev": _dev,
    "/api/tests": _tests,
    "/api/ci": _ci,
    "/api/attention": _attention,
    "/api/agent-mine": _agent_mine,
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
            raw = self.path
            path = raw.split("?")[0]
            if path == "/api/node":
                import urllib.parse

                from .tree import node_detail
                q = urllib.parse.parse_qs(raw.split("?", 1)[1]) if "?" in raw else {}
                try:
                    payload = json.dumps(node_detail(q.get("id", [""])[0]), indent=2, sort_keys=True).encode("utf-8")
                except Exception as exc:
                    payload = json.dumps({"error": str(exc)}).encode("utf-8")
                return self._send(200, payload)
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
