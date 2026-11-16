/*
 * Lab sign-in helpers shared by the share viewer and the member "My artifacts" page: authorization code + PKCE S256
 * against the lab Cognito pool (public client). Pure helpers: they hold no state of their own; the caller decides
 * where the pending record and the tokens live. No HTML is ever built here.
 */
export const ORIGIN = /^https:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?$/;

export function sessionStore() {
  try {
    return window.sessionStorage;
  } catch (e) {
    return null;
  }
}

export function b64url(bytes) {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomString(n) {
  const bytes = new Uint8Array(n);
  crypto.getRandomValues(bytes);
  return b64url(bytes);
}

/** True when the cognito config has the shape both pages need (https origin, client id, https redirect). */
export function validConfig(c) {
  return !!c && typeof c.cognito_domain === "string" && ORIGIN.test(c.cognito_domain)
    && typeof c.client_id === "string" && !!c.client_id
    && typeof c.redirect_uri === "string" && c.redirect_uri.indexOf("https://") === 0;
}

/** Fresh PKCE material: the caller stores {state, verifier, nonce} and sends the user to `url`.  The nonce comes back
 *  inside the ID token; a page that sends it with the session exchange (X-Nonce) proves the token is the one it asked for. */
export async function beginPkce(cfg) {
  const verifier = randomString(32);
  const state = randomString(24);
  const nonce = randomString(24);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  const query = new URLSearchParams({
    response_type: "code",
    client_id: cfg.client_id,
    redirect_uri: cfg.redirect_uri,
    scope: "openid email",
    state: state,
    nonce: nonce,
    code_challenge: b64url(new Uint8Array(digest)),
    code_challenge_method: "S256"
  });
  return { state, verifier, nonce, url: cfg.cognito_domain + "/oauth2/authorize?" + query.toString() };
}

export async function exchangeCode(cfg, code, verifier) {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: cfg.client_id,
    code: code,
    redirect_uri: cfg.redirect_uri,
    code_verifier: verifier
  });
  const r = await fetch(cfg.cognito_domain + "/oauth2/token", {
    method: "POST",
    cache: "no-store",
    credentials: "omit",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString()
  });
  if (!r.ok) throw new Error("token");
  const t = await r.json();
  if (!t || typeof t.access_token !== "string" || typeof t.id_token !== "string") throw new Error("token");
  return { access: t.access_token, id: t.id_token };
}
