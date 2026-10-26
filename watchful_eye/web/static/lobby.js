import { token, getName, setName, cleanName, wsUrl } from "/static/identity.js";

const nameInput = document.getElementById("name");
const nameNote = document.getElementById("name-note");
const queueNote = document.getElementById("queue-note");
const customNote = document.getElementById("custom-note");
nameInput.value = getName();
nameInput.addEventListener("change", () => { nameInput.value = cleanName(nameInput.value); setName(nameInput.value); });

let joined = null;
const ws = new WebSocket(wsUrl("/ws/lobby"));
const send = (m) => ws.readyState === 1 && ws.send(JSON.stringify({ ...m, token: token() }));

function requireName() {
  const n = cleanName(nameInput.value);
  if (!n) { nameNote.textContent = "Pick a name first."; nameInput.focus(); return null; }
  nameNote.textContent = "";
  setName(n);
  return n;
}

document.querySelectorAll(".join").forEach(btn => {
  btn.addEventListener("click", () => {
    const rs = btn.dataset.ruleset;
    if (joined === rs) { send({ type: "leave" }); return; }
    const n = requireName();
    if (!n) return;
    send({ type: "queue", ruleset: rs, name: n });
  });
});

function render() {
  document.querySelectorAll("#queues .row").forEach(row => {
    const rs = row.dataset.ruleset;
    row.classList.toggle("joined", joined === rs);
    row.querySelector(".join").textContent = joined === rs ? "Leave" : "Join";
  });
  queueNote.textContent = joined ? `You're in the ${joined} queue. Keep this tab open; the game opens here when it fills.` : "";
  queueNote.style.color = "var(--iris)";
}

ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.type === "queues") {
    for (const [rs, n] of Object.entries(m.counts)) {
      const el = document.querySelector(`#queues .row[data-ruleset="${rs}"] .n`);
      if (el) el.textContent = n;
    }
  } else if (m.type === "queued") {
    joined = m.ruleset; render();
  } else if (m.type === "start" || m.type === "created") {
    location.href = `/play/${m.room}`;
  } else if (m.type === "error") {
    customNote.textContent = m.text;
  }
});
ws.addEventListener("close", () => { queueNote.textContent = "Connection lost — reload the page."; queueNote.style.color = "var(--red)"; });

const form = document.getElementById("custom");
const orderSel = form.elements.move_order;
const chaosField = document.getElementById("chaos-field");
orderSel.addEventListener("change", () => { chaosField.hidden = orderSel.value !== "none"; });
form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (!requireName()) return;
  const f = form.elements;
  send({ type: "create", options: {
    cast: +f.cast.value, deck_size: +f.deck_size.value, move_wait_s: +f.move_wait_s.value,
    move_order: f.move_order.value, public_chat: f.public_chat.checked, chaos_minutes: +f.chaos_minutes.value,
  }});
});
