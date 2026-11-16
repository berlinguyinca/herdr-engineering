/*
 * Lab share viewer: signs the person in with the lab Cognito pool (authorization code + PKCE S256, public client),
 * exchanges the verified tokens for a short-lived session path for this one share, then navigates to the entry.
 *
 * Rules: tokens live only in this origin's sessionStorage, and only until the session is minted; they never appear in
 * a URL, the console or the page. The share path travels in sessionStorage keyed to a random `state`, never to Cognito.
 * Any failure shows one neutral message and never says why. No HTML is ever built from data (textContent only).
 */
import { sessionStore, validConfig, beginPkce, exchangeCode } from "/s/static/lab-signin.js";
import { renderLandingBar, renderSigninCard, rememberReturn, takeReturn } from "/s/static/landing.js";

{
  "use strict";

  var SHARE_PATH = /^\/s\/[A-Za-z0-9_-]{43}\/$/;
  var SIGNIN_PATH = /^(\/s\/[A-Za-z0-9_-]{43}\/)signin$/;
  // /s/<token>/k/<session>/<f|v>/<entry>: the entry is one or more plain path segments (no backslash, query, fragment,
  // control or space character, no empty segment) so the browser cannot resolve it anywhere but under the share.
  var LANDING_PATH = /^\/s\/[A-Za-z0-9_-]{43}\/k\/[A-Za-z0-9_.-]{1,768}\/[fv]\/[^\/\\?#\x00-\x20\x7f]+(?:\/[^\/\\?#\x00-\x20\x7f]+)*$/;
  var DOT_SEGMENT = /(?:^|\/)(?:\.|\.\.|%2e|\.%2e|%2e\.|%2e%2e)(?:\/|$)/i;
  var ENCODED_SEPARATOR = /%(?:2f|5c|00|3f|23)/i;

  function safeLanding(landing, sharePath) {
    if (landing === sharePath) return true;      // a public link: the share path itself, which redirects to the entry
    return typeof landing === "string" && LANDING_PATH.test(landing) && landing.indexOf(sharePath) === 0
      && !DOT_SEGMENT.test(landing) && !ENCODED_SEPARATOR.test(landing);
  }
  var PENDING_KEY = "herdr-share-pkce";
  var TOKENS_KEY = "herdr-share-tokens";

  var store = sessionStore;

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

  async function loadConfig() {
    var r = await fetch("/s/static/config.json", { cache: "no-store", credentials: "omit" });
    if (!r.ok) throw new Error("config");
    var c = await r.json();
    if (!validConfig(c)) throw new Error("config");
    return c;
  }

  async function signIn(cfg, sharePath) {
    var p = await beginPkce(cfg);
    store().setItem(PENDING_KEY, JSON.stringify({ state: p.state, verifier: p.verifier, share: sharePath }));
    location.assign(p.url);
  }

  async function openShare(sharePath, tokens) {
    // same-origin: the response sets the HttpOnly write cookie that the annotation routes require
    var r = await fetch(sharePath + "session", {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: { "Authorization": "Bearer " + tokens.access, "X-Id-Token": tokens.id }
    });
    forget(); // the session path replaces the tokens; nothing is kept either way
    if (!r.ok) throw new Error("session");
    var got = await r.json();
    if (!got || !safeLanding(got.landing, sharePath)) {
      throw new Error("session");
    }
    // a public link returns to where the visitor was (kept in sessionStorage, validated on read); a lab session
    // goes to its own landing
    var back = got.landing === sharePath ? takeReturn(store()) : null;
    location.replace(back && back.indexOf(sharePath) === 0 ? back : got.landing);
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

  // The sign-in step of a public link: the card, one button. The way back is remembered in sessionStorage only.
  function signinCard(cfg, sharePath) {
    var main = document.querySelector("main");
    var info = null;
    var data = document.getElementById("herdr-landing");
    try { info = data ? JSON.parse(data.textContent) : null; } catch (e) { info = null; }
    var go = function () {
      rememberReturn(store(), sharePath);
      signIn(cfg, sharePath).catch(fail);
    };
    var card = renderSigninCard(document, go);
    if (main && main.parentNode) {
      if (info) {
        info.now = Date.now() / 1000;
        info.signedIn = false;
        var bar = renderLandingBar(document, info, go);
        main.parentNode.insertBefore(bar, main);
      }
      main.parentNode.replaceChild(card, main);
    }
  }

  async function main() {
    if (!store() || !window.crypto || !crypto.subtle) throw new Error("unsupported");
    var path = location.pathname;
    if (path === "/s/callback") return callback();
    var signin = SIGNIN_PATH.exec(path);
    if (signin) return signinCard(await loadConfig(), signin[1]);
    if (SHARE_PATH.test(path)) return signIn(await loadConfig(), path);
    throw new Error("route");
  }

  main().catch(fail);
}
