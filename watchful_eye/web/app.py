"""Watchful Eye — play it in a browser.  Run:  uvicorn watchful_eye.web.app:app --reload"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect

# try:  # Starlette raises a second class on some disconnect paths
#     from starlette.websockets import WebSocketDisconnected  # type: ignore
# except ImportError:  # pragma: no cover
#     WebSocketDisconnected = WebSocketDisconnect  # type: ignore
WebSocketDisconnected = WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .room import Room, clean_name, new_room_id
from .rulesets import DEFAULT_CAST, MAX_CAST, MIN_CAST, RULESET_BLURBS, RULESET_LABELS, PRESET_NAMES, RoomOptions, preset

HERE = Path(__file__).parent
app = FastAPI(title="Watchful Eye")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

rooms: dict[str, Room] = {}
# ruleset -> {token: (name, websocket)}
queues: dict[str, dict[str, tuple[str, WebSocket]]] = {k: {} for k in PRESET_NAMES}
lobby_sockets: set[WebSocket] = set()


# ------------------------------------------------------------------ pages


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse("/play")


@app.get("/play", response_class=HTMLResponse)
async def lobby_page(request: Request) -> HTMLResponse:
    rulesets = [{"key": k, "label": RULESET_LABELS[k], "blurb": RULESET_BLURBS[k], "cast": DEFAULT_CAST} for k in queues]
    return templates.TemplateResponse(request, "lobby.html", {
        "rulesets": rulesets, "min_cast": MIN_CAST, "max_cast": MAX_CAST, "default_cast": DEFAULT_CAST,
    })


@app.get("/play/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: str) -> HTMLResponse:
    room = rooms.get(room_id)
    if room is None:
        return templates.TemplateResponse(request, "missing.html", {"room_id": room_id}, status_code=404)
    return templates.TemplateResponse(request, "game.html", {"room_id": room_id, "label": room.options.label})


# ------------------------------------------------------------------ lobby ws


def queue_counts() -> dict[str, Any]:
    return {"type": "queues", "counts": {k: len(v) for k, v in queues.items()}, "cast": DEFAULT_CAST}


async def lobby_broadcast(msg: dict[str, Any]) -> None:
    dead = []
    for ws in lobby_sockets:
        try:
            await ws.send_json(msg)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        lobby_sockets.discard(ws)


def remove_from_queues(token: str) -> None:
    for q in queues.values():
        q.pop(token, None)


@app.websocket("/ws/lobby")
async def lobby_ws(ws: WebSocket) -> None:
    await ws.accept()
    lobby_sockets.add(ws)
    token: str | None = None
    await ws.send_json(queue_counts())
    try:
        while True:
            msg: dict = await ws.receive_json()
            t = msg.get("type")
            token = str(msg.get("token") or token or "")
            if t == "queue":
                ruleset = msg.get("ruleset")
                if ruleset not in queues or not token:
                    continue
                remove_from_queues(token)
                queues[ruleset][token] = (clean_name(msg.get("name", "")), ws)
                await ws.send_json({"type": "queued", "ruleset": ruleset})
                await lobby_broadcast(queue_counts())
                await maybe_start_queue(ruleset)
            elif t == "leave":
                remove_from_queues(token)
                await ws.send_json({"type": "queued", "ruleset": None})
                await lobby_broadcast(queue_counts())
            elif t == "create":
                try:
                    options = RoomOptions(ruleset="custom", **(msg.get("options") or {}))
                except Exception as e:  # noqa: BLE001
                    await ws.send_json({"type": "error", "text": f"Those options don't work: {e}"})
                    continue
                room_id = new_room_id()
                rooms[room_id] = Room(room_id, options, asyncio.get_running_loop(), creator_token=token)
                await ws.send_json({"type": "created", "room": room_id})
    except (WebSocketDisconnect, WebSocketDisconnected, RuntimeError):
        pass
    finally:
        lobby_sockets.discard(ws)
        if token:
            remove_from_queues(token)
            await lobby_broadcast(queue_counts())


async def maybe_start_queue(ruleset: str) -> None:
    q = queues[ruleset]
    if len(q) < DEFAULT_CAST:
        return
    members = list(q.items())[:DEFAULT_CAST]
    for tok, _ in members:
        q.pop(tok, None)
    room_id = new_room_id()
    room = Room(room_id, preset(ruleset), asyncio.get_running_loop())
    rooms[room_id] = room
    for tok, (name, _ws) in members:
        room.join(tok, name)
    for tok, (_name, sock) in members:
        try:
            await sock.send_json({"type": "start", "room": room_id})
        except Exception:  # noqa: BLE001
            pass
    room.start()
    await lobby_broadcast(queue_counts())


# ------------------------------------------------------------------ room ws


@app.websocket("/ws/room/{room_id}")
async def room_ws(ws: WebSocket, room_id: str) -> None:
    await ws.accept()
    room = rooms.get(room_id)
    if room is None:
        await ws.send_json({"type": "error", "text": "That room does not exist."})
        await ws.close()
        return
    hello = await ws.receive_json()
    token = str(hello.get("token") or "")
    role, name = room.join(token, clean_name(hello.get("name", "")))
    room.connect(role, name, ws)
    await ws.send_json({"type": "hello", "role": role, "name": name, "creator": token == room.creator_token})
    await ws.send_json(room.room_info())
    for entry in room.chat_history_for(role, name):
        await ws.send_json(entry)
    if room.game is not None:
        snap = room.game.snapshot(name if role == "player" else None)
        await ws.send_json({"type": "state", **snap})
        # replay the feed this person is allowed to see
        for e in list(room.game.events):
            priv = e.private.get(name) if role == "player" else None
            if e.public or priv:
                await ws.send_json({"type": "event", "seq": e.seq, "round": e.round, "phase": e.phase, "turn": e.turn,
                                    "kind": e.type, "text": e.public, "private": priv})
    else:
        room.broadcast(room.room_info())
    try:
        while True:
            msg = await ws.receive_json()
            await asyncio.to_thread(room.handle, role, name, token, msg)
    except (WebSocketDisconnect, WebSocketDisconnected, RuntimeError):
        pass
    finally:
        room.disconnect(role, name, ws)
        if room.state == "waiting" and role == "player" and not room.sockets.get(name):
            room.leave_waiting(token)
            room.broadcast(room.room_info())
