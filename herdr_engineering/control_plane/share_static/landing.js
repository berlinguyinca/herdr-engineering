/*
 * Recipient landing helpers for the public share pages: the "Shared by NAME" bar, the sign-in notice and button, the
 * sign-in card and the same-origin return path. Pure helpers plus plain DOM building (textContent only, never HTML).
 *
 * This script makes no network request of its own, and in particular none to record a claim: a claim is recorded by the
 * share server when it serves a share to a visitor whose session it has verified. The button only starts the existing
 * sign-in flow (the caller passes the handler). The return path lives in sessionStorage, never in a URL.
 */
export const RETURN_KEY = "herdr.share.return";
export const FALLBACK_SHARER = "a lab member";
export const NOTICE = "Sign in with your lab account to add this to your library. Signing in does not change who can see it.";
export const BUTTON = "Sign in to keep this";
export const CARD = {
  title: "Sign in to Artifacts",
  line: "Lab members: use your lab account. Opening a shared link doesn’t need an account.",
  button: "Continue with lab sign-in"
};

const DAY = 86400;
const RETURN_PATH = /^\/s\/[A-Za-z0-9_\-.%~/]*$/;

/** "expires in N days" / "expires in 1 day" / "expires today"; empty when there is no usable expiry. */
export function expiresText(expiresAt, now) {
  if (typeof expiresAt !== "number" || typeof now !== "number" || !isFinite(expiresAt) || !isFinite(now)) return "";
  const days = Math.floor((expiresAt - now) / DAY);
  if (days < 1) return "expires today";
  return days === 1 ? "expires in 1 day" : "expires in " + days + " days";
}

/** The bar's parts for one share: sharer (fallback when unknown), optional title and expiry text, signed-in flag. */
export function landingBarModel(info) {
  const sharer = typeof info.sharer === "string" && info.sharer.trim() ? info.sharer.trim() : FALLBACK_SHARER;
  const parts = ["Shared by " + sharer];
  if (typeof info.title === "string" && info.title.trim()) parts.push(info.title.trim());
  const left = expiresText(info.expiresAt, info.now);
  if (left) parts.push(left);
  const signedIn = info.signedIn === true;
  return { text: parts.join(" · "), signedIn, notice: signedIn ? "" : NOTICE, button: signedIn ? "" : BUTTON };
}

/** The bar element (and, signed out, the notice and button). `onSignIn` is the existing sign-in flow; nothing else is called. */
export function renderLandingBar(doc, info, onSignIn) {
  const model = landingBarModel(info);
  const bar = doc.createElement("div");
  bar.className = "herdr-landing";
  bar.setAttribute("role", "note");
  const text = doc.createElement("span");
  text.className = "herdr-landing-text";
  text.textContent = model.text;
  bar.appendChild(text);
  if (!model.signedIn) {
    const button = doc.createElement("button");
    button.type = "button";
    button.className = "herdr-landing-signin";
    button.textContent = model.button;
    button.addEventListener("click", () => { if (typeof onSignIn === "function") onSignIn(); });
    bar.appendChild(button);
    const notice = doc.createElement("p");
    notice.className = "herdr-landing-notice";
    notice.textContent = model.notice;
    bar.appendChild(notice);
  }
  return bar;
}

/** The centred sign-in card: heading, one line, one button. */
export function renderSigninCard(doc, onContinue) {
  const card = doc.createElement("section");
  card.className = "card herdr-signin-card";
  const h1 = doc.createElement("h1");
  h1.textContent = CARD.title;
  const line = doc.createElement("p");
  line.textContent = CARD.line;
  const button = doc.createElement("button");
  button.type = "button";
  button.className = "herdr-signin-button";
  button.textContent = CARD.button;
  button.addEventListener("click", () => { if (typeof onContinue === "function") onContinue(); });
  card.appendChild(h1);
  card.appendChild(line);
  card.appendChild(button);
  return card;
}

/** A same-origin path under /s/ and nothing else: no scheme, no `//`, no backslash, no control or space, no dot segment. */
export function safeReturnPath(value) {
  if (typeof value !== "string" || value.length > 512 || value.indexOf("/s/") !== 0) return null;
  if (!RETURN_PATH.test(value) || value.indexOf("//") !== -1 || value.indexOf("\\") !== -1) return null;
  if (/(?:^|\/)(?:\.|\.\.|%2e|\.%2e|%2e\.|%2e%2e)(?:\/|$)/i.test(value) || /%(?:2f|5c|00|3f|23)/i.test(value)) return null;
  if (value === "/s/callback" || value.indexOf("/s/static/") === 0) return null;
  return value;
}

export function rememberReturn(store, path) {
  const safe = safeReturnPath(path);
  if (!store || !safe) return false;
  try {
    store.setItem(RETURN_KEY, safe);
    return true;
  } catch (e) {
    return false;
  }
}

/** Reads and clears the remembered return path; null unless it still passes `safeReturnPath`. */
export function takeReturn(store) {
  if (!store) return null;
  try {
    const raw = store.getItem(RETURN_KEY);
    store.removeItem(RETURN_KEY);
    return safeReturnPath(raw);
  } catch (e) {
    return null;
  }
}
