import { token, getName, cleanName, wsUrl } from "/static/identity.js";

const $ = (id) => document.getElementById(id);
const roomId = $("game").dataset.room;

const S = {
  me: null, role: "spectator", creator: false,
  room: null, game: null, you: null,
  decisions: [],          // pending {id, kind, legal, deadline, error}
  sel: null,              // selected card id
  mode: null,             // what the prompt strip is doing: pass | discard | draw_discard | train | declare | shuffle | write | kind | nominate | throw
  build: {},              // scratch for builders
  writingPlayers: [],
  feedAtBottom: true,
};

// ------------------------------------------------------------------ socket
let ws;
function connect() {
  ws = new WebSocket(wsUrl(`/ws/room/${roomId}`));
  ws.addEventListener("open", () => ws.send(JSON.stringify({ token: token(), name: cleanName(getName()) || "Player" })));
  ws.addEventListener("message", (ev) => onMessage(JSON.parse(ev.data)));
  ws.addEventListener("close", () => { toast("Connection lost. Reconnecting…"); setTimeout(connect, 1500); });
}
const send = (m) => ws && ws.readyState === 1 && ws.send(JSON.stringify(m));

function onMessage(m) {
  switch (m.type) {
    case "hello": S.me = m.name; S.role = m.role; S.creator = m.creator; break;
    case "room": S.room = m; renderRoom(); break;
    case "state": S.game = m.game; S.you = m.you; renderAll(); break;
    case "event": addFeed(m); break;
    case "decision": S.decisions.push(m); S.mode = null; S.build = {}; renderPrompt(); renderHand(); break;
    case "decision_error": { const d = S.decisions.find(x => x.id === m.id); if (d) { d.error = m.error; renderPrompt(); } break; }
    case "decision_done": S.decisions = S.decisions.filter(x => x.id !== m.id); S.mode = null; S.build = {}; if (m.timeout) toast("Time ran out — the host made the default move."); renderPrompt(); renderHand(); renderHouse(); break;
    case "chat": addChat(m); break;
    case "writing": S.writingPlayers = m.players; renderHouse(); break;
    case "notice": toast(m.text); break;
    case "error": toast(m.text); break;
  }
}

// ------------------------------------------------------------------ helpers
const PHASE_NAMES = { setup: "Setup", competition: "Competition", open1: "Open I", nomination: "Nomination", open2: "Open II", vote: "Vote", finale: "Finale" };
const phaseName = (p) => PHASE_NAMES[p] || p;
const spectating = () => S.role !== "player" || (S.you && !S.you.alive);
const active = () => S.decisions.find(d => d.kind === "respond_pass") || S.decisions[0] || null;
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const btn = (label, cls, fn) => { const b = el("button", `btn ${cls || ""}`, label); b.type = "button"; b.addEventListener("click", fn); return b; };
const chip = (label, on, fn) => { const c = el("button", `chip select${on ? " on" : ""}`, label); c.type = "button"; c.addEventListener("click", fn); return c; };
let toastTimer;
function toast(text) { const t = $("toast"); t.textContent = text; t.classList.add("show"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200); }
const fmt = (secs) => { secs = Math.max(0, Math.ceil(secs)); return `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`; };

function cardFace(c, opts = {}) {
  const d = el("div", "card");
  d.dataset.id = c.id;
  d.appendChild(el("div", "kind", c.label || c.kind));
  const terms = [];
  if (c.kind === "Safety") terms.push("do not nominate the passer");
  if (c.kind === "Pledge") terms.push("declare, or be exposed");
  if (c.kind === "Throw" && c.params && c.params.slot) terms.push(`slot: ${c.params.slot}`);
  if (terms.length) d.appendChild(el("div", "terms", terms.join(" · ")));
  if (c.category === "message") {
    d.appendChild(el("div", `body ${c.writing ? "writing" : "blank"}`, c.writing ? `“${c.writing}”` : "blank"));
  } else {
    d.appendChild(el("div", "body", c.deal || ""));
  }
  const foot = el("div", "foot");
  if (c.commitment_id) foot.appendChild(el("span", "deal-on", "binding"));
  else if (c.category === "deal" && c.deal === undefined && opts.fulfilled) foot.appendChild(el("span", "deal-ok", "fulfilled"));
  foot.appendChild(el("span", "", c.category));
  d.appendChild(foot);
  if (c.kind === "Jury Vote") d.appendChild(el("span", "eye"));
  return d;
}

// ------------------------------------------------------------------ rendering
function renderAll() { renderTop(); renderHouse(); renderHand(); renderPrompt(); renderChatVisibility(); }

function renderRoom() {
  const r = S.room;
  const waiting = r.state === "waiting";
  $("waiting").hidden = !waiting;
  $("feed").hidden = waiting;
  if (r.rules_html) $("rules-body").innerHTML = r.rules_html;
  if (waiting) {
    $("w-room").textContent = r.id;
    $("w-label").textContent = `${r.label} · ${r.options.cast} players · ${r.options.move_wait_s}s a move` +
      (r.options.move_order === "none" ? ` · Chaos, ${r.options.chaos_minutes} min per Open phase` : "") + (r.options.public_chat ? " · public chat" : " · no chat");
    $("w-link").textContent = location.href;
    const pl = $("w-players"); pl.innerHTML = "";
    r.players.forEach(p => pl.appendChild(el("span", "chip" + (p === S.me ? " on" : ""), p)));
    const need = r.cast - r.players.length;
    $("w-status").textContent = S.role === "player"
      ? (need > 0 ? `Waiting for ${need} more. The game starts when the room is full${S.creator ? ", or when you start it" : ""}.` : "Room is full.")
      : "This room is full; you'll watch as a spectator.";
    $("w-start").hidden = !(S.creator && r.can_start);
    $("phase").textContent = "Waiting for players";
  } else if (r.state === "finished") {
    $("phase").textContent = "Game over";
  }
  renderChatVisibility();
}
$("w-start").addEventListener("click", () => send({ type: "start" }));

function renderTop() {
  const g = S.game; if (!g) return;
  const ph = $("phase");
  ph.innerHTML = "";
  ph.appendChild(el("b", "", `Round ${g.round}`));
  ph.appendChild(document.createTextNode(` · ${phaseName(g.phase)}`));
  if (g.phase.startsWith("open")) {
    if (g.turn_scope === "chaos") {
      const left = S.you && g.chaos_actions_left[S.me];
      ph.appendChild(document.createTextNode(left != null ? ` · ${left} action${left === 1 ? "" : "s"} left` : ""));
    } else ph.appendChild(document.createTextNode(` · turn ${g.turn} of ${g.open_turns}`));
  }
  if (g.winner) ph.textContent = `${g.winner} wins`;
}

function renderClock() {
  const c = $("clock"); const g = S.game;
  if (!g || g.winner) { c.textContent = ""; c.className = "clock"; return; }
  const now = Date.now() / 1000;
  const mine = active();
  let secs = null, who = "", cls = "clock";
  if (mine) { secs = mine.deadline - now; who = "you"; cls += " mine"; }
  else if (g.phase_deadline) { secs = g.phase_deadline - now; who = "phase clock"; }
  else {
    const entries = Object.entries(g.pending || {});
    if (entries.length) { const [p, v] = entries[0]; secs = v.deadline - now; who = entries.length > 1 ? `${entries.length} players` : p; }
  }
  if (secs == null) { c.textContent = ""; c.className = "clock"; return; }
  if (secs < 10) cls += " urgent";
  c.className = cls;
  c.innerHTML = `${fmt(secs)}<small>${who}</small>`;
}
setInterval(renderClock, 250);

function renderHouse() {
  const g = S.game; if (!g) return;
  const seats = $("seats"); seats.innerHTML = "";
  const byName = Object.fromEntries(g.players.map(p => [p.name, p]));
  const living = g.order.filter(n => byName[n] && byName[n].alive);
  const out = g.eliminated;
  const picking = S.mode === "pass" ? S.build.recipients : S.mode === "nominate" ? S.build.candidates : S.mode === "shuffle" ? S.build.remaining : S.mode === "declare" ? S.build.pickable : null;
  for (const n of [...living, ...out]) {
    const p = byName[n];
    const row = el("div", "seat" + (p.alive ? "" : " out") + (g.active_player === n && g.phase.startsWith("open") && g.turn_scope !== "chaos" ? " active" : ""));
    row.appendChild(el("span", "mark", g.active_player === n && p.alive && g.turn_scope !== "chaos" && g.phase.startsWith("open") ? "▶" : ""));
    const who = el("div", "who");
    who.appendChild(el("span", "n", n));
    if (n === S.me) who.appendChild(el("span", "tag you", "you"));
    if (p.watcher) who.appendChild(el("span", "tag watcher", "Watcher"));
    if (p.nominee) who.appendChild(el("span", "tag nominee", "nominee"));
    if (S.writingPlayers.includes(n) && n !== S.me) who.appendChild(el("span", "tag writing", "writing"));
    if (g.mist === n) who.appendChild(el("span", "tag", "mist"));
    if (!p.alive) who.appendChild(el("span", "tag", `out #${p.eliminated_order}`));
    row.appendChild(who);
    row.appendChild(el("span", "cards", p.alive ? `${p.hand} cards` : ""));
    if (picking && picking.includes(n)) {
      row.classList.add("pick");
      if ((S.build.picked || []).includes(n)) row.classList.add("picked");
      row.addEventListener("click", () => pickName(n));
    }
    seats.appendChild(row);
  }
  const slots = $("slots"); slots.innerHTML = "";
  g.slots.forEach(s => {
    const t = s.status === "empty" ? `${s.slot}: empty` : s.status === "claimed" ? `${s.slot}: ${s.by}` : s.slot;
    slots.appendChild(el("span", `slot ${s.status}`, t));
  });
  // deals
  const you = S.you;
  const deals = $("deals"); deals.innerHTML = "";
  const any = you && (you.deals_held.length || you.deals_given.length);
  $("deals-section").hidden = !any;
  if (any) {
    you.deals_held.forEach(d => {
      const x = el("div", "deal"); x.appendChild(el("div", "", d.text));
      x.appendChild(el("div", `status ${d.status}`, d.status === "active" ? "binding on you — keep the card" : "fulfilled — the card is free"));
      deals.appendChild(x);
    });
    you.deals_given.forEach(d => {
      const x = el("div", "deal"); x.appendChild(el("div", "", d.text));
      const st = el("div", `status ${d.status}`, d.status === "fulfilled" ? "fulfilled" : "held by " + d.holder + " — status unknown to you");
      x.appendChild(st);
      if (you.alive && you.exposable.some(e => e.id === d.id)) x.appendChild(btn("Expose", "small quiet", () => send({ type: "expose", commitment: d.id })));
      deals.appendChild(x);
    });
  }
  const decl = $("declares"); decl.innerHTML = "";
  $("declares-section").hidden = !g.declares.length;
  g.declares.forEach(d => {
    const x = el("div", "deal"); x.appendChild(el("div", "", d.text));
    if (you && you.alive && you.exposable.some(e => e.id === d.id)) x.appendChild(btn("Expose", "small quiet", () => send({ type: "expose", commitment: d.id })));
    decl.appendChild(x);
  });
}

function renderHand() {
  const wrap = $("cards"); wrap.innerHTML = "";
  const you = S.you;
  const a = active();
  if (a && a.kind === "respond_pass") {
    const face = cardFace(a.legal.card); face.classList.add("offered");
    wrap.appendChild(face);
    const label = el("div", "sub", `offered by ${a.legal.from}`); label.style.alignSelf = "center"; label.style.color = "var(--slate)";
    wrap.appendChild(label);
    return;
  }
  if (!you) return;
  const fulfilledIds = new Set(you.deals_held.filter(d => d.status === "fulfilled").map(d => d.card));
  you.hand.forEach(c => {
    const face = cardFace(c, { fulfilled: fulfilledIds.has(c.id) });
    if (S.sel === c.id) face.classList.add("selected");
    if (a && a.kind === "distribute") {
      const sel = el("select", "pick-to");
      sel.appendChild(new Option("discard", ""));
      a.legal.recipients.forEach(r => sel.appendChild(new Option(`to ${r}`, r)));
      sel.value = (S.build.transfers || {})[c.id] || "";
      sel.addEventListener("change", () => { S.build.transfers = S.build.transfers || {}; if (sel.value) S.build.transfers[c.id] = sel.value; else delete S.build.transfers[c.id]; });
      sel.addEventListener("click", e => e.stopPropagation());
      face.appendChild(sel);
    } else {
      const legal = a && a.kind === "open_turn" ? a.legal : null;
      if (S.mode === "discard" || S.mode === "draw_discard") { if (!legal.discardable.includes(c.id)) face.classList.add("dim"); }
      face.addEventListener("click", () => selectCard(c.id));
    }
    wrap.appendChild(face);
  });
}

function selectCard(id) {
  const a = active();
  if (a && a.kind === "open_turn" && (S.mode === "discard" || S.mode === "draw_discard")) {
    if (!a.legal.discardable.includes(id)) { toast("That card can't be discarded."); return; }
    decide(a, S.mode === "discard" ? { action: "draw_discard", discard_card: id } : { action: "draw_discard", draw: true, discard_card: id });
    return;
  }
  S.sel = S.sel === id ? null : id;
  S.mode = null; S.build = {};
  renderHand(); renderPrompt(); renderHouse();
}

function decide(d, data) { send({ type: "decision", id: d.id, data }); S.mode = null; S.build = {}; S.sel = null; renderPrompt(); renderHand(); renderHouse(); }

// ------------------------------------------------------------------ the prompt strip
function renderPrompt() {
  const P = $("prompt"); P.innerHTML = "";
  const g = S.game, you = S.you, a = active();
  if (!g) return;
  if (g.winner) { P.appendChild(el("span", "say", `${g.winner} wins Watchful Eye`)); return; }
  if (!you) { P.appendChild(el("span", "sub", "You're watching.")); return; }
  if (!you.alive) { P.appendChild(el("span", "sub", "You're out of the house. You'll be asked back if the jury is needed.")); return; }
  if (a && a.error) P.appendChild(el("span", "err", a.error));

  if (!a) {
    const pending = Object.keys(g.pending || {});
    const text = g.phase.startsWith("open") && g.turn_scope === "chaos" ? "Open phase — act when you're ready."
      : pending.length ? `Waiting for ${pending.length > 2 ? pending.length + " players" : pending.join(" and ")}…` : "Waiting for the host…";
    P.appendChild(el("span", "sub", text));
    freeActions(P);
    return;
  }
  switch (a.kind) {
    case "open_turn": return promptOpen(P, a);
    case "respond_pass": return promptRespond(P, a);
    case "nominate": return promptNominate(P, a);
    case "vote": return promptChoice(P, a, "Vote to eliminate", a.legal.nominees, (n) => ({ vote: n }), "red");
    case "tiebreak": return promptChoice(P, a, "Break the tie — who goes?", a.legal.nominees, (n) => ({ eliminate: n }), "red");
    case "jury_vote": return promptChoice(P, a, "Who should win?", a.legal.finalists, (n) => ({ vote: n }), "iris");
    case "distribute": return promptDistribute(P, a);
  }
}

function freeActions(P) {
  // Things allowed at any time: writing on a selected message card.
  const you = S.you;
  if (!S.sel || !you) return;
  const c = you.hand.find(x => x.id === S.sel); if (!c) return;
  if (you.writable.includes(c.id)) {
    if (S.mode === "write") return writeBuilder(P, c);
    P.appendChild(btn("Write on it", "quiet small", () => { S.mode = "write"; renderPrompt(); }));
  }
}

function writeBuilder(P, c) {
  const b = el("div", "builder");
  const limit = c.kind === "Tea" ? 3 - (c.writing ? c.writing.split(/\s+/).length : 0) : null;
  b.appendChild(el("span", "label", limit != null ? `${c.kind}: ${limit} more word${limit === 1 ? "" : "s"}. Words can't be erased.` : "Manifesto: write as much as you like. Words can't be erased."));
  const ta = el("textarea"); ta.placeholder = "Write…";
  ta.addEventListener("focus", () => send({ type: "writing", on: true }));
  ta.addEventListener("blur", () => setTimeout(() => send({ type: "writing", on: false }), 100));
  b.appendChild(ta);
  b.appendChild(btn("Add words", "iris small", () => { if (ta.value.trim()) send({ type: "write", card: c.id, text: ta.value }); S.mode = null; renderPrompt(); }));
  b.appendChild(btn("Cancel", "quiet small", () => { S.mode = null; renderPrompt(); }));
  P.appendChild(b);
  setTimeout(() => ta.focus(), 0);
}

function promptOpen(P, a) {
  const L = a.legal, you = S.you;
  P.appendChild(el("span", "say mine", "Your move"));
  const c = S.sel ? you.hand.find(x => x.id === S.sel) : null;
  if (S.mode === "write" && c) return writeBuilder(P, c);
  if (S.mode === "discard") { P.appendChild(el("span", "sub", "Pick a card to discard.")); P.appendChild(btn("Cancel", "quiet small", resetMode)); return; }
  if (S.mode === "draw_discard") { P.appendChild(el("span", "sub", "Draw, and pick a card to discard.")); P.appendChild(btn("Cancel", "quiet small", resetMode)); return; }
  if (S.mode === "pass" && c) {
    if (c.kind === "Throw" && !S.build.throw_slot) {
      const b = el("div", "builder"); b.appendChild(el("span", "label", "Which of your slots does the Throw name?"));
      L.throwable_slots.forEach(s => b.appendChild(chip(s, false, () => { S.build.throw_slot = s; renderPrompt(); })));
      b.appendChild(btn("Cancel", "quiet small", resetMode)); P.appendChild(b); return;
    }
    P.appendChild(el("span", "sub", `Pass ${c.label} to… pick a name in the house.`)); P.appendChild(btn("Cancel", "quiet small", resetMode)); return;
  }
  if (S.mode === "kind" && c) {
    const b = el("div", "builder"); b.appendChild(el("span", "label", "Play the Wild as"));
    L.playable[c.id].forEach(k => b.appendChild(chip(k, false, () => startPlay(a, c, k))));
    b.appendChild(btn("Cancel", "quiet small", resetMode)); P.appendChild(b); return;
  }
  if (S.mode === "train" && c) {
    const b = el("div", "builder"); b.appendChild(el("span", "label", "Claim which empty slot?"));
    L.train_slots.forEach(s => b.appendChild(chip(s, false, () => decide(a, { action: "play", card: c.id, play_as: S.build.as, train_slot: s }))));
    b.appendChild(btn("Cancel", "quiet small", resetMode)); P.appendChild(b); return;
  }
  if (S.mode === "shuffle" && c) {
    const b = el("div", "builder"); b.appendChild(el("span", "label", "New order — click names in the house in sequence:"));
    b.appendChild(el("span", "", (S.build.picked || []).join(" › ") || "…"));
    if ((S.build.picked || []).length === L.living.length) b.appendChild(btn("Set this order", "iris small", () => decide(a, { action: "play", card: c.id, play_as: S.build.as, new_order: S.build.picked })));
    b.appendChild(btn("Start over", "quiet small", () => { S.build.picked = []; S.build.remaining = [...L.living]; renderPrompt(); renderHouse(); }));
    b.appendChild(btn("Cancel", "quiet small", resetMode)); P.appendChild(b); return;
  }
  if (S.mode === "declare" && c) {
    return declareBuilder(P, L.others, null, (terms) => decide(a, { action: "play", card: c.id, play_as: S.build.as, declare: terms }));
  }
  // default: top-level actions + selected-card actions
  if (L.can_draw) P.appendChild(btn("Draw", "", () => decide(a, { action: "draw_discard", draw: true })));
  if (L.can_draw && L.discardable.length) P.appendChild(btn("Draw & discard", "quiet", () => { S.mode = "draw_discard"; renderPrompt(); renderHand(); }));
  if (L.discardable.length) P.appendChild(btn("Discard", "quiet", () => { S.mode = "discard"; renderPrompt(); renderHand(); }));
  P.appendChild(btn("Skip", "quiet", () => decide(a, { action: "skip" })));
  if (c) {
    const sep = el("span", "sub", `· ${c.label}:`); P.appendChild(sep);
    if (L.playable[c.id]) P.appendChild(btn(c.category === "message" ? "Read it aloud" : "Play", "iris", () => {
      const kinds = L.playable[c.id];
      if (kinds.length > 1) { S.mode = "kind"; renderPrompt(); } else startPlay(a, c, kinds[0]);
    }));
    if (L.passable.includes(c.id)) P.appendChild(btn("Pass to…", "iris", () => { S.mode = "pass"; S.build = { recipients: L.recipients }; renderPrompt(); renderHouse(); }));
    if (L.discardable.includes(c.id)) P.appendChild(btn("Discard this", "quiet", () => decide(a, { action: "draw_discard", discard_card: c.id })));
    if (you.writable.includes(c.id)) P.appendChild(btn("Write on it", "quiet", () => { S.mode = "write"; renderPrompt(); }));
    if (c.commitment_id) P.appendChild(el("span", "sub", "an active deal — it must stay in your hand"));
  } else {
    P.appendChild(el("span", "sub", "or pick a card below"));
  }
}

function startPlay(a, c, kind) {
  S.build.as = c.kind === "Wild" ? kind : undefined;
  if (kind === "Train") { S.mode = "train"; }
  else if (kind === "Declare") { S.mode = "declare"; S.build.pickable = a.legal.others; }
  else if (kind === "Shuffle") { S.mode = "shuffle"; S.build.picked = []; S.build.remaining = [...a.legal.living]; }
  else { decide(a, { action: "play", card: c.id, play_as: S.build.as }); return; }
  renderPrompt(); renderHouse();
}

function resetMode() { S.mode = null; S.build = {}; renderPrompt(); renderHand(); renderHouse(); }

function pickName(n) {
  const a = active(); if (!a) return;
  if (S.mode === "pass") {
    const c = S.you.hand.find(x => x.id === S.sel);
    const data = { action: "pass", card: c.id, to: n };
    if (c.kind === "Throw") data.throw_slot = S.build.throw_slot;
    decide(a, data);
  } else if (S.mode === "shuffle") {
    S.build.picked.push(n); S.build.remaining = S.build.remaining.filter(x => x !== n); renderPrompt(); renderHouse();
  } else if (S.mode === "nominate") {
    const p = S.build.picked || [];
    S.build.picked = p.includes(n) ? p.filter(x => x !== n) : [...p, n].slice(-2);
    renderPrompt(); renderHouse();
  } else if (S.mode === "declare") {
    declarePick(n);
  }
}

// Declare terms builder; `fixed` pins the category (Pledge-as-Declare).
function declareBuilder(P, others, fixed, done) {
  const B = S.build;
  B.category = fixed || B.category;
  B.pickable = others; B.picked = B.picked || [];
  const b = el("div", "builder");
  if (!fixed) {
    const g = el("div", "group"); g.appendChild(el("span", "label", "Declare that you will"));
    g.appendChild(chip("nominate…", B.category === "nominate", () => { B.category = "nominate"; B.picked = []; renderPrompt(); renderHouse(); }));
    g.appendChild(chip("vote against…", B.category === "vote", () => { B.category = "vote"; B.picked = []; renderPrompt(); renderHouse(); }));
    b.appendChild(g);
  } else b.appendChild(el("span", "label", `This Pledge calls for a “${fixed}” Declare.`));
  if (B.category === "nominate") {
    b.appendChild(el("span", "label", "Pick one or two names in the house:"));
    b.appendChild(el("span", "", B.picked.join(" and ") || "…"));
    if (B.picked.length) b.appendChild(btn("Declare", "iris small", () => done({ category: "nominate", targets: B.picked })));
  } else if (B.category === "vote") {
    b.appendChild(el("span", "label", "Pick the two nominees this applies to, then who you'll vote against:"));
    b.appendChild(el("span", "", B.picked.join(" and ") || "…"));
    if (B.picked.length === 2) {
      const g = el("div", "group"); g.appendChild(el("span", "label", "vote against"));
      B.picked.forEach(n => g.appendChild(chip(n, B.target === n, () => { B.target = n; renderPrompt(); })));
      b.appendChild(g);
      if (B.target) b.appendChild(btn("Declare", "iris small", () => done({ category: "vote", pair: B.picked, target: B.target })));
    }
  }
  b.appendChild(btn("Cancel", "quiet small", resetMode));
  P.appendChild(b);
  renderHouse();
}
function declarePick(n) {
  const B = S.build; const p = B.picked || [];
  B.picked = p.includes(n) ? p.filter(x => x !== n) : [...p, n].slice(-2);
  if (B.target && !B.picked.includes(B.target)) B.target = null;
  renderPrompt(); renderHouse();
}

function promptRespond(P, a) {
  const L = a.legal;
  P.appendChild(el("span", "say mine", `${L.from} offers you a card`));
  if (S.mode === "declare") {
    return declareBuilder(P, L.others, L.pledge_category, (terms) => decide(a, { accept: true, declare: terms }));
  }
  P.appendChild(btn("Accept", "iris", () => decide(a, { accept: true })));
  if (L.pledge_category) P.appendChild(btn("Accept and declare now", "iris", () => { S.mode = "declare"; S.build = {}; renderPrompt(); }));
  if (L.can_decline) P.appendChild(btn("Decline", "red", () => decide(a, { accept: false })));
  else P.appendChild(el("span", "sub", "It can't be declined."));
}

function promptNominate(P, a) {
  if (S.mode !== "nominate") { S.mode = "nominate"; S.build = { candidates: a.legal.candidates, picked: [] }; renderHouse(); }
  P.appendChild(el("span", "say mine", "Nominate two"));
  P.appendChild(el("span", "sub", (S.build.picked || []).join(" and ") || "Pick two names in the house."));
  if (a.legal.your_binding_declares.length) P.appendChild(el("span", "sub", `Bound by: ${a.legal.your_binding_declares.join("; ")}`));
  if (a.legal.safety_deals_you_hold.length) P.appendChild(el("span", "sub", `Safety deals: ${a.legal.safety_deals_you_hold.join("; ")}`));
  if ((S.build.picked || []).length === 2) P.appendChild(btn("Nominate them", "red", () => decide(a, { nominees: S.build.picked })));
}

function promptChoice(P, a, say, names, mk, cls) {
  P.appendChild(el("span", "say mine", say));
  names.forEach(n => P.appendChild(btn(n, cls, () => decide(a, mk(n)))));
  if (a.legal.your_binding_declares && a.legal.your_binding_declares.length) P.appendChild(el("span", "sub", `Bound by: ${a.legal.your_binding_declares.join("; ")}`));
}

function promptDistribute(P, a) {
  P.appendChild(el("span", "say mine", "You're out"));
  P.appendChild(el("span", "sub", "Hand your cards to survivors, or let them be discarded."));
  P.appendChild(btn("Done", "iris", () => decide(a, { transfers: Object.entries(S.build.transfers || {}).map(([card, to]) => ({ card, to })) })));
}

// ------------------------------------------------------------------ feed & chat
function addFeed(e) {
  const feed = $("feed");
  const line = el("div", "line " + (e.private && !e.text ? "private" : "") + (e.kind === "round" ? " round" : e.kind === "phase" || e.kind === "chaos" ? " phase" : ""));
  if (["penalty", "betrayal", "expose", "eliminated", "tiebreak"].includes(e.kind)) line.classList.add("alarm");
  if (["waiting", "fallback", "spin", "deck"].includes(e.kind)) line.classList.add("notice");
  if (e.kind !== "round") line.appendChild(el("span", "stamp", `R${e.round} ${phaseName(e.phase)}`));
  line.appendChild(document.createTextNode(e.text || e.private || ""));
  if (e.text && e.private) { const p = el("div", "line private"); p.appendChild(el("span", "stamp", `R${e.round} ${phaseName(e.phase)}`)); p.appendChild(document.createTextNode(e.private)); feed.appendChild(line); feed.appendChild(p); }
  else feed.appendChild(line);
  if (S.feedAtBottom) feed.scrollTop = feed.scrollHeight;
}
$("feed").addEventListener("scroll", () => { const f = $("feed"); S.feedAtBottom = f.scrollHeight - f.scrollTop - f.clientHeight < 40; });

function renderChatVisibility() {
  const r = S.room; if (!r) return;
  const show = (r.options && r.options.public_chat) || spectating();
  $("game").classList.toggle("no-chat", !show);
  $("chat-title").textContent = spectating() ? "Spectators" : "Chat";
  $("chat-hint").textContent = spectating()
    ? (r.options.public_chat ? "You can read the house. What you say here reaches only other spectators." : "Only other spectators can read this.")
    : "Everyone still in the house can read this.";
}
function addChat(m) {
  const log = $("chat-log");
  const d = el("div", `msg ${m.channel}`); d.appendChild(el("span", "from", m.from)); d.appendChild(document.createTextNode(m.text));
  log.appendChild(d); log.scrollTop = log.scrollHeight;
}
$("chat-form").addEventListener("submit", (e) => { e.preventDefault(); const i = $("chat-input"); if (i.value.trim()) send({ type: "chat", text: i.value }); i.value = ""; });

$("rules-btn").addEventListener("click", () => { $("rules").classList.add("open"); $("rules").setAttribute("aria-hidden", "false"); });
$("rules-close").addEventListener("click", () => { $("rules").classList.remove("open"); $("rules").setAttribute("aria-hidden", "true"); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") { $("rules").classList.remove("open"); if (S.mode) resetMode(); } });

connect();
