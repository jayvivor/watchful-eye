"""A room: a set of connected people, one game thread, and the bridge between them."""

from __future__ import annotations

import asyncio
import json
import queue
import random
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from ..agents.base import AgentResponse, DecisionRequest
from ..agents.random_agent import RandomAgent
from ..engine import Game
from ..prompts import rules_sheet
from ..state import Event
from .rulesets import MIN_CAST, RoomOptions, game_config

NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def clean_name(raw: str) -> str:
    name = NAME_RE.sub("", (raw or "").strip().replace(" ", "_"))[:16]
    return name or "Player"


class HumanAgent:
    """Implements the engine's Agent protocol by asking a person over their websocket."""

    is_human = True

    def __init__(self, room: "Room", name: str):
        self.room = room
        self.name = name
        self.inboxes: dict[str, queue.Queue] = {}
        self.random = RandomAgent(random.Random())

    # -- Agent protocol -------------------------------------------------------

    def decide(self, request: DecisionRequest) -> AgentResponse:
        req_id = uuid.uuid4().hex[:8]
        box: queue.Queue = queue.Queue()
        self.inboxes[req_id] = box
        deadline = request.deadline or (time.time() + 30)
        self.room.send(self.name, {"type": "decision", "id": req_id, "kind": request.kind,
                                   "legal": request.legal, "deadline": deadline})
        self.room.push_state()  # so everyone sees who is being asked, and the clock
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                try:
                    msg = box.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    continue
                data = msg.get("data") or {}
                err = request.validator(data) if request.validator else None
                if err:
                    self.room.send(self.name, {"type": "decision_error", "id": req_id, "error": err})
                    continue
                self.room.send(self.name, {"type": "decision_done", "id": req_id})
                return AgentResponse(data=data, raw="human")
        finally:
            self.inboxes.pop(req_id, None)
        self.room.send(self.name, {"type": "decision_done", "id": req_id, "timeout": True})
        return AgentResponse(data=self.default(request), raw="timeout")

    def default(self, request: DecisionRequest) -> dict[str, Any]:
        kind, legal = request.kind, request.legal
        if kind == "open_turn":
            return {"action": "skip"}
        if kind == "respond_pass":
            return {"accept": not legal.get("can_decline", True)}
        if kind == "distribute":
            return {"transfers": []}
        return self.random.decide(request).data or {}

    def observe(self, text: str) -> None:
        self.room.send(self.name, {"type": "notice", "text": text})

    def commit(self, history_text: str, keep_reply: bool) -> None:
        pass

    # -- from the socket ------------------------------------------------------

    def deliver(self, msg: dict[str, Any]) -> bool:
        box = self.inboxes.get(msg.get("id", ""))
        if box is None:
            return False
        box.put(msg)
        return True


class Room:
    def __init__(self, room_id: str, options: RoomOptions, loop: asyncio.AbstractEventLoop,
                 run_root: str = "runs/web", creator_token: str | None = None):
        self.id = room_id
        self.options = options
        self.loop = loop
        self.run_root = run_root
        self.creator_token = creator_token
        self.state = "waiting"  # waiting | playing | finished
        self.created_at = time.time()
        self.seats: dict[str, str] = {}  # token -> name, in join order
        self.sockets: dict[str, set[WebSocket]] = {}  # name -> connections
        self.spectators: dict[str, set[WebSocket]] = {}  # name -> connections (non-players who opened the link)
        self.agents: dict[str, HumanAgent] = {}
        self.game: Game | None = None
        self.thread: threading.Thread | None = None
        self.chat: list[dict[str, Any]] = []  # public + spectator channels
        self.writing: set[str] = set()
        self.rules_html = ""
        self.lock = threading.Lock()

    # -- membership -----------------------------------------------------------

    @property
    def names(self) -> list[str]:
        return list(self.seats.values())

    def is_full(self) -> bool:
        return len(self.seats) >= self.options.cast

    def join(self, token: str, name: str) -> tuple[str, str]:
        """Returns (role, name): role is 'player' or 'spectator'."""
        with self.lock:
            if token in self.seats:
                return "player", self.seats[token]
            if self.state != "waiting" or self.is_full():
                return "spectator", self._unique(name, spectator=True)
            name = self._unique(name)
            self.seats[token] = name
            return "player", name

    def _unique(self, name: str, spectator: bool = False) -> str:
        taken = set(self.seats.values()) | set(self.spectators)
        base, n = name, 2
        while name in taken:
            name = f"{base}{n}"
            n += 1
        return name

    def leave_waiting(self, token: str) -> None:
        with self.lock:
            if self.state == "waiting":
                self.seats.pop(token, None)

    def connect(self, role: str, name: str, ws: WebSocket) -> None:
        target = self.sockets if role == "player" else self.spectators
        target.setdefault(name, set()).add(ws)

    def disconnect(self, role: str, name: str, ws: WebSocket) -> None:
        target = self.sockets if role == "player" else self.spectators
        conns = target.get(name)
        if conns:
            conns.discard(ws)
            if not conns and role == "spectator":
                target.pop(name, None)
        if name in self.writing:
            self.writing.discard(name)
            self.broadcast({"type": "writing", "players": sorted(self.writing)})

    # -- game lifecycle -------------------------------------------------------

    def can_start(self) -> bool:
        return self.state == "waiting" and len(self.seats) >= MIN_CAST

    def start(self) -> None:
        with self.lock:
            if self.state != "waiting":
                return
            self.state = "playing"
        names = self.names
        cfg = game_config(self.options, names, seed=None, run_dir_parent=self.run_root)
        self.agents = {n: HumanAgent(self, n) for n in names}
        run_dir = Path(self.run_root) / f"{self.id}-{time.strftime('%Y%m%d-%H%M%S')}"
        self.game = Game(cfg, agents=self.agents, run_dir=run_dir)  # type: ignore[arg-type]
        self.game.on_event = self._on_event
        self.rules_html = md_to_html(rules_sheet(cfg, web=True))
        self.broadcast(self.room_info())
        self.thread = threading.Thread(target=self._run, name=f"room-{self.id}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        assert self.game is not None
        try:
            self.game.run()
        except Exception as e:  # noqa: BLE001
            self.broadcast({"type": "notice", "text": f"The host hit an error and the game stopped: {e}"})
        finally:
            self.state = "finished"
            self.broadcast(self.room_info())
            self.push_state()

    # -- messages out ---------------------------------------------------------

    def _send_ws(self, ws: WebSocket, msg: dict[str, Any]) -> None:
        payload = json.dumps(msg, default=str)
        asyncio.run_coroutine_threadsafe(_safe_send(ws, payload), self.loop)

    def send(self, name: str, msg: dict[str, Any]) -> None:
        for ws in list(self.sockets.get(name, ())):
            self._send_ws(ws, msg)

    def send_spectator(self, name: str, msg: dict[str, Any]) -> None:
        for ws in list(self.spectators.get(name, ())):
            self._send_ws(ws, msg)

    def broadcast(self, msg: dict[str, Any], *, players: bool = True, spectators: bool = True) -> None:
        if players:
            for name in list(self.sockets):
                self.send(name, msg)
        if spectators:
            for name in list(self.spectators):
                self.send_spectator(name, msg)

    def _is_spectating(self, name: str) -> bool:
        """Players who are out of the game join the spectator chat."""
        if name in self.spectators:
            return True
        if self.game is None:
            return False
        ps = self.game.players.get(name)
        return ps is not None and not ps.alive

    def room_info(self) -> dict[str, Any]:
        return {
            "type": "room", "id": self.id, "state": self.state, "options": self.options.model_dump(),
            "label": self.options.label, "players": self.names, "cast": self.options.cast,
            "can_start": self.can_start(), "rules_html": self.rules_html,
        }

    def _on_event(self, e: Event) -> None:
        base = {"type": "event", "seq": e.seq, "round": e.round, "phase": e.phase, "turn": e.turn, "kind": e.type}
        for name in list(self.sockets):
            if e.public or name in e.private:
                self.send(name, {**base, "text": e.public, "private": e.private.get(name)})
        if e.public:
            for name in list(self.spectators):
                self.send_spectator(name, {**base, "text": e.public, "private": None})
        self.push_state()

    def push_state(self) -> None:
        if self.game is None:
            return
        public = None
        for name in list(self.sockets):
            snap = self.game.snapshot(name)
            public = snap["game"]
            self.send(name, {"type": "state", **snap})
        if self.spectators:
            if public is None:
                public = self.game.snapshot(None)["game"]
            for name in list(self.spectators):
                self.send_spectator(name, {"type": "state", "game": public, "you": None})

    # -- messages in ----------------------------------------------------------

    def handle(self, role: str, name: str, token: str, msg: dict[str, Any]) -> None:
        t = msg.get("type")
        if t == "start":
            if token == self.creator_token and self.can_start():
                self.start()
            return
        if t == "chat":
            self._chat(role, name, str(msg.get("text", ""))[:500])
            return
        if role != "player":
            return
        agent = self.agents.get(name)
        game = self.game
        if t == "decision" and agent:
            if not agent.deliver(msg):
                self.send(name, {"type": "notice", "text": "That decision is no longer open."})
        elif t == "write" and game:
            err = game.write(name, str(msg.get("card", "")), str(msg.get("text", ""))[:2000])
            if err:
                self.send(name, {"type": "notice", "text": err})
            self._set_writing(name, False)
        elif t == "writing":
            self._set_writing(name, bool(msg.get("on")))
        elif t == "expose" and game:
            err = game.expose_now(name, str(msg.get("commitment", "")))
            if err:
                self.send(name, {"type": "notice", "text": err})

    def _set_writing(self, name: str, on: bool) -> None:
        before = set(self.writing)
        (self.writing.add if on else self.writing.discard)(name)
        if self.writing != before:
            self.broadcast({"type": "writing", "players": sorted(self.writing)})

    def _chat(self, role: str, name: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        spectating = role == "spectator" or self._is_spectating(name)
        channel = "spectator" if spectating else "public"
        if channel == "public" and not self.options.public_chat:
            self.send(name, {"type": "notice", "text": "This game has no public chat. Use the cards."})
            return
        entry = {"type": "chat", "channel": channel, "from": name, "text": text, "t": time.time()}
        self.chat.append(entry)
        if channel == "public":
            # living players + everyone spectating (spectators can read the house)
            self.broadcast(entry)
        else:
            for n in list(self.sockets):
                if self._is_spectating(n):
                    self.send(n, entry)
            self.broadcast(entry, players=False)

    def chat_history_for(self, role: str, name: str) -> list[dict[str, Any]]:
        spectating = role == "spectator" or self._is_spectating(name)
        return [c for c in self.chat if c["channel"] == "public" or spectating]


async def _safe_send(ws: WebSocket, payload: str) -> None:
    try:
        await ws.send_text(payload)
    except Exception:  # noqa: BLE001 — closed socket; the disconnect handler cleans up
        pass


def new_room_id() -> str:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(6))


def md_to_html(md: str) -> str:
    """Just enough Markdown for the rules sheet: headings, bullets, paragraphs, backticks, bold."""
    import html

    out, in_list = [], False
    for raw in md.splitlines():
        line = raw.strip()
        if line.startswith("- "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(html.escape(line[2:]))}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        if line.startswith("### "):
            out.append(f"<h4>{html.escape(line[4:])}</h4>")
        elif line.startswith("## "):
            out.append(f"<h3>{html.escape(line[3:])}</h3>")
        elif line.startswith("# "):
            out.append(f"<h2>{html.escape(line[2:])}</h2>")
        elif re.match(r"^\d+\. ", line):
            out.append(f"<p class='step'>{_inline(html.escape(line))}</p>")
        else:
            out.append(f"<p>{_inline(html.escape(line))}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _inline(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s
