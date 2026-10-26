// Who you are in the browser: a display name and a random token that follows you into rooms.
export function token() {
  let t = localStorage.getItem("we.token");
  if (!t) {
    t = Array.from(crypto.getRandomValues(new Uint8Array(12)), b => b.toString(16).padStart(2, "0")).join("");
    localStorage.setItem("we.token", t);
  }
  return t;
}

export function getName() { return localStorage.getItem("we.name") || ""; }
export function setName(n) { localStorage.setItem("we.name", n); }

export function cleanName(raw) {
  return (raw || "").trim().replace(/\s+/g, "_").replace(/[^A-Za-z0-9_\-]/g, "").slice(0, 16);
}

export function wsUrl(path) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}${path}`;
}
