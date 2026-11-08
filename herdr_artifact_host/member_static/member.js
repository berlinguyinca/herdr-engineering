import { beginPkce, exchangeCode, sessionStore, validConfig } from "/my/static/lab-signin.js";

const $ = (id) => document.getElementById(id);
const CHUNK = 524288;
let cfg = null;
let session = null;
let current = null;
const store = sessionStore();

function say(text) { $("msg").textContent = text; }
function el(tag, text, cls) {
  const e = document.createElement(tag);
  if (text !== undefined) e.textContent = text;
  if (cls) e.className = cls;
  return e;
}
async function api(method, path, body, headers) {
  const h = Object.assign({}, headers || {});
  if (session) h["Authorization"] = "Member " + session;
  let payload;
  if (body !== undefined && !(body instanceof Uint8Array)) { h["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  else if (body !== undefined) payload = body;
  const r = await fetch("/api/member" + path, { method, headers: h, body: payload, cache: "no-store", credentials: "omit" });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { session = null; if (store) store.removeItem("member"); show(); throw new Error("sign in again"); }
  if (!r.ok) throw new Error(data.message || data.error || "failed");
  return data;
}
async function hex(bytes) {
  const d = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(d, (b) => b.toString(16).padStart(2, "0")).join("");
}
function show() {
  $("signin").hidden = !!session;
  $("app").hidden = !session;
  $("signout").hidden = !session;
}
async function load() {
  const me = await api("GET", "/me");
  $("who").textContent = me.email || me.member_id;
  $("usage").textContent = "Using " + me.usage.bytes_used + " of " + me.usage.max_member_bytes + " bytes";
  const q = $("q").value ? "?q=" + encodeURIComponent($("q").value) : "";
  const lib = await api("GET", "/library" + q);
  const ul = $("items");
  ul.replaceChildren();
  for (const it of lib.items) {
    const li = el("li");
    li.append(el("span", it.title, "grow"), el("span", it.source, "tag"));
    if (it.source === "mine") { const b = el("button", "Open"); b.onclick = () => open(it.artifact_id); li.append(b); }
    if (it.status === "pending") {
      const a = el("button", "Accept"); a.onclick = async () => { await api("POST", "/pending/" + it.artifact_id + "/accept"); load(); };
      const d = el("button", "Discard"); d.onclick = async () => { await api("POST", "/pending/" + it.artifact_id + "/discard"); load(); };
      li.append(a, d);
    }
    ul.append(li);
  }
}
async function open(id) {
  current = id;
  const head = await api("GET", "/artifacts/" + id);
  $("detail").hidden = false;
  $("d-title").textContent = head.title;
  $("d-files").textContent = head.files.map((f) => f.name).join(", ") || "No files yet";
  const links = await api("GET", "/artifacts/" + id + "/shares");
  const ul = $("links");
  ul.replaceChildren();
  for (const l of links.links) {
    const li = el("li");
    li.append(el("span", l.audience + " " + l.status, "grow"));
    if (l.state === "pending") { const b = el("button", "Approve"); b.onclick = async () => { await api("POST", "/artifacts/" + id + "/shares/" + l.share_id + "/approve", { confirm: true }); open(id); }; li.append(b); }
    if (l.state !== "revoked") { const b = el("button", "Revoke"); b.onclick = async () => { await api("POST", "/artifacts/" + id + "/shares/" + l.share_id + "/revoke"); open(id); }; li.append(b); }
    ul.append(li);
  }
}
async function publish() {
  const files = Array.from($("up").files);
  if (!files.length || !current) return;
  const head = await api("GET", "/artifacts/" + current);
  const specs = [];
  for (const f of files) {
    const bytes = new Uint8Array(await f.arrayBuffer());
    const fileSha = await hex(bytes);
    const spec = { name: f.name, size_bytes: bytes.length, file_sha256: fileSha };
    specs.push(spec);
    const st = await api("POST", "/artifacts/" + current + "/uploads/status", spec);
    const uid = st.upload_id;
    for (let i = 0; i * CHUNK < bytes.length; i++) {
      if (!st.missing.some((r) => r[0] <= i && i <= r[1])) continue;
      const part = bytes.slice(i * CHUNK, (i + 1) * CHUNK);
      await api("PUT", "/artifacts/" + current + "/uploads/" + uid + "/runs/" + i, part, {
        "X-Name": encodeURIComponent(f.name), "X-Size-Bytes": String(bytes.length),
        "X-File-SHA256": fileSha, "X-Run-SHA256": await hex(part), "Content-Type": "application/octet-stream"
      });
    }
  }
  await api("POST", "/artifacts/" + current + "/publish", { base_version_id: head.current_version_id, files: specs });
  say("Published.");
  open(current);
}
async function start() {
  cfg = await (await fetch("/api/member/config", { cache: "no-store", credentials: "omit" })).json();
  if (!validConfig(cfg)) { say("Sign-in is not configured."); return; }
  const url = new URL(location.href);
  if (url.pathname === "/my/callback") {
    const rec = store && JSON.parse(store.getItem("pkce") || "null");
    if (store) store.removeItem("pkce");
    const code = url.searchParams.get("code");
    if (rec && code && url.searchParams.get("state") === rec.state) {
      try {
        const t = await exchangeCode(cfg, code, rec.verifier);
        const r = await fetch("/api/member/session", { method: "POST", cache: "no-store", credentials: "omit", headers: { Authorization: "Bearer " + t.access, "X-Id-Token": t.id } });
        const s = await r.json();
        if (r.ok) { session = s.session; if (store) store.setItem("member", session); }
      } catch (e) { say("Sign-in failed."); }
    }
    history.replaceState(null, "", "/");
  } else if (store) session = store.getItem("member");
  show();
  if (session) { try { await load(); } catch (e) { say(e.message); } }
}
$("signin-btn").onclick = async () => {
  const p = await beginPkce(cfg);
  if (store) store.setItem("pkce", JSON.stringify({ state: p.state, verifier: p.verifier }));
  location.assign(p.url);
};
$("signout").onclick = () => { session = null; if (store) store.removeItem("member"); show(); };
$("q").oninput = () => load().catch((e) => say(e.message));
$("new").onclick = async () => {
  const title = prompt("Title");
  if (!title) return;
  const r = await api("POST", "/artifacts", { title, filename: "index.html" });
  await load(); open(r.artifact_id);
};
$("publish").onclick = () => publish().catch((e) => say(e.message));
$("mk").onclick = async () => {
  const head = await api("GET", "/artifacts/" + current);
  const body = { version_id: head.current_version_id, audience: $("aud").value, expires_in_days: Number($("days").value) };
  if (body.audience === "lab") body.allow_emails = $("allow").value.split(",").map((s) => s.trim()).filter(Boolean);
  try {
    const r = await api("POST", "/artifacts/" + current + "/shares", body);
    say("Link (shown once): " + r.url);
    open(current);
  } catch (e) { say(e.message); }
};
start();
