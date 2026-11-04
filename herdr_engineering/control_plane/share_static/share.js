/*
 * Lab share viewer: signs the person in with the lab Cognito pool (authorization code + PKCE S256, public client),
 * exchanges the verified tokens for a short-lived session path for this one share, then navigates to the entry.
 *
 * Rules: tokens live only in this origin's sessionStorage, and only until the session is minted; they never appear in
 * a URL, the console or the page. The share path travels in sessionStorage keyed to a random `state`, never to Cognito.
 * Any failure shows one neutral message and never says why. No HTML is ever built from data (textContent only).
 */
(function () {
  "use strict";

  var SHARE_PATH = /^\/s\/[A-Za-z0-9_-]{43}\/$/;
  var BASE_PATH = /^\/s\/[A-Za-z0-9_-]{43}\/k\/[A-Za-z0-9_.-]{1,512}\/f\/$/;
  var ORIGIN = /^https:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/;
  var PENDING_KEY = "herdr-share-pkce";
  var TOKENS_KEY = "herdr-share-tokens";

  function store() {
    try {
      return window.sessionStorage;
    } catch (e) {
      return null;
    }
  }

  function forget() {
    var s = store();
    if (!s) return;
    try {
      s.removeItem(PENDING_KEY);
      s.removeItem(TOKENS_KEY);
    } catch (e) { /* nothing to clear */ }
  }

  function fail() {
    forget();
    var el = document.getElementById("status");
    if (el) {
      el.textContent = "This link isn’t available.";
      el.className = "failed";
    }
  }

  function b64url(bytes) {
    var bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function randomString(n) {
    var bytes = new Uint8Array(n);
    crypto.getRandomValues(bytes);
    return b64url(bytes);
  }

  async function loadConfig() {
    var r = await fetch("/s/static/config.json", { cache: "no-store", credentials: "omit" });
    if (!r.ok) throw new Error("config");
    var c = await r.json();
    if (!c || typeof c.cognito_domain !== "string" || !ORIGIN.test(c.cognito_domain)
        || typeof c.client_id !== "string" || !c.client_id
        || typeof c.redirect_uri !== "string" || c.redirect_uri.indexOf("https://") !== 0) {
      throw new Error("config");
    }
    return c;
  }

  async function signIn(cfg, sharePath) {
    var verifier = randomString(32);
    var state = randomString(24);
    var digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    store().setItem(PENDING_KEY, JSON.stringify({ state: state, verifier: verifier, share: sharePath }));
    var query = new URLSearchParams({
      response_type: "code",
      client_id: cfg.client_id,
      redirect_uri: cfg.redirect_uri,
      scope: "openid email",
      state: state,
      code_challenge: b64url(new Uint8Array(digest)),
      code_challenge_method: "S256"
    });
    location.assign(cfg.cognito_domain + "/oauth2/authorize?" + query.toString());
  }

  async function exchangeCode(cfg, code, verifier) {
    var body = new URLSearchParams({
      grant_type: "authorization_code",
      client_id: cfg.client_id,
      code: code,
      redirect_uri: cfg.redirect_uri,
      code_verifier: verifier
    });
    var r = await fetch(cfg.cognito_domain + "/oauth2/token", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString()
    });
    if (!r.ok) throw new Error("token");
    var t = await r.json();
    if (!t || typeof t.access_token !== "string" || typeof t.id_token !== "string") throw new Error("token");
    return { access: t.access_token, id: t.id_token };
  }

  async function openShare(sharePath, tokens) {
    var r = await fetch(sharePath + "session", {
      method: "POST",
      cache: "no-store",
      credentials: "omit",
      headers: { "Authorization": "Bearer " + tokens.access, "X-Id-Token": tokens.id }
    });
    forget(); // the session path replaces the tokens; nothing is kept either way
    if (!r.ok) throw new Error("session");
    var got = await r.json();
    if (!got || typeof got.base !== "string" || typeof got.entry !== "string" || !got.entry
        || !BASE_PATH.test(got.base) || got.base.indexOf(sharePath) !== 0) {
      throw new Error("session");
    }
    var entry = got.entry.split("/").map(encodeURIComponent).join("/");
    location.replace(got.base + entry);
  }

  async function callback() {
    var params = new URLSearchParams(location.search);
    history.replaceState(null, "", "/s/callback"); // the code must not stay in the address bar or history
    var s = store();
    var raw = s.getItem(PENDING_KEY);
    s.removeItem(PENDING_KEY);
    var pending = raw ? JSON.parse(raw) : null;
    var code = params.get("code");
    if (params.get("error") || !pending || typeof pending.state !== "string" || params.get("state") !== pending.state
        || typeof pending.verifier !== "string" || typeof pending.share !== "string"
        || !SHARE_PATH.test(pending.share) || !code) {
      throw new Error("callback");
    }
    var cfg = await loadConfig();
    var tokens = await exchangeCode(cfg, code, pending.verifier);
    s.setItem(TOKENS_KEY, JSON.stringify(tokens));
    await openShare(pending.share, tokens);
  }

  async function main() {
    if (!store() || !window.crypto || !crypto.subtle) throw new Error("unsupported");
    var path = location.pathname;
    if (path === "/s/callback") return callback();
    if (SHARE_PATH.test(path)) return signIn(await loadConfig(), path);
    throw new Error("route");
  }

  main().catch(fail);
})();
