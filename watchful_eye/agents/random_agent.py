"""A legal-but-mindless player. Used for dry runs and as the fallback when an LLM misbehaves."""

from __future__ import annotations

import random
from typing import Any

from ..cards import CardKind
from .base import AgentResponse, DecisionRequest

WORDS = ["trust", "me", "vote", "out", "safe", "deal", "no", "yes", "watch", "them", "later", "now"]


class RandomAgent:
    name = "random"

    def __init__(self, rng: random.Random):
        self.rng = rng

    def observe(self, text: str) -> None:
        pass

    def commit(self, history_text: str, keep_reply: bool) -> None:
        pass

    def decide(self, request: DecisionRequest) -> AgentResponse:
        fn = getattr(self, f"_{request.kind}")
        return AgentResponse(data=fn(request.legal), raw="<random>")

    # ------------------------------------------------------------------ kinds

    def _open_turn(self, legal: dict[str, Any]) -> dict[str, Any]:
        r = self.rng
        options: list[dict[str, Any]] = []
        if legal["can_draw"]:
            options += [{"action": "draw_discard", "draw": True}] * 3
        if legal["discardable"]:
            options.append({"action": "draw_discard", "discard_card": r.choice(legal["discardable"])})
        if legal["writable"]:
            cid = r.choice(legal["writable"])
            n = 1 if legal["card_kind"][cid] == CardKind.TEA.value else r.randint(2, 8)
            options.append({"action": "write", "card": cid, "text": " ".join(r.choice(WORDS) for _ in range(n))})
        for cid, kinds in legal["playable"].items():
            kind = r.choice(kinds)
            d: dict[str, Any] = {"action": "play", "card": cid}
            if legal["card_kind"][cid] == CardKind.WILD.value:
                d["play_as"] = kind
            params = self._action_params(kind, legal)
            if params is None:
                continue
            d.update(params)
            options.append(d)
        for cid in legal["passable"]:
            if not legal["recipients"]:
                break
            d = {"action": "pass", "card": cid, "to": r.choice(legal["recipients"])}
            if legal["card_kind"][cid] == CardKind.THROW.value:
                if not legal["throwable_slots"]:
                    continue
                d["throw_slot"] = r.choice(legal["throwable_slots"])
            options.append(d)
        if not options:
            options.append({"action": "skip"})
        d = dict(r.choice(options))
        if legal.get("exposable") and r.random() < 0.3:
            d["expose"] = {"commitment_id": r.choice(legal["exposable"])}
        return d

    def _declare_terms(self, legal: dict[str, Any], category: str | None = None) -> dict[str, Any] | None:
        r = self.rng
        others = legal["others"]
        if len(others) < 2:
            return None
        if category is None:
            category = r.choice(["nominate", "vote"])
        if category == "nominate":
            return {"category": "nominate", "targets": r.sample(others, r.randint(1, 2))}
        pair = legal.get("current_nominees") or []
        if len(pair) != 2 or any(p not in others for p in pair):
            pair = r.sample(others, 2)
        return {"category": "vote", "pair": list(pair), "target": r.choice(pair)}

    def _action_params(self, kind: str, legal: dict[str, Any]) -> dict[str, Any] | None:
        r = self.rng
        if kind == CardKind.TRAIN.value:
            if not legal["train_slots"]:
                return None
            return {"train_slot": r.choice(legal["train_slots"])}
        if kind == CardKind.DECLARE.value:
            terms = self._declare_terms(legal)
            return None if terms is None else {"declare": terms}
        if kind == CardKind.SHUFFLE.value:
            order = list(legal["living"])
            r.shuffle(order)
            return {"new_order": order}
        return {}

    def _respond_pass(self, legal: dict[str, Any]) -> dict[str, Any]:
        d: dict[str, Any] = {"accept": (not legal["can_decline"]) or self.rng.random() < 0.7}
        if legal.get("pledge_category") and d["accept"] and self.rng.random() < 0.5:
            terms = self._declare_terms(legal, legal["pledge_category"])
            if terms:
                d["declare"] = terms
        return d

    def _nominate(self, legal: dict[str, Any]) -> dict[str, Any]:
        return {"nominees": self.rng.sample(legal["candidates"], 2)}

    def _vote(self, legal: dict[str, Any]) -> dict[str, Any]:
        return {"vote": self.rng.choice(legal["nominees"])}

    def _tiebreak(self, legal: dict[str, Any]) -> dict[str, Any]:
        return {"eliminate": self.rng.choice(legal["nominees"])}

    def _distribute(self, legal: dict[str, Any]) -> dict[str, Any]:
        transfers = []
        for cid in legal["hand_ids"]:
            if legal["recipients"] and self.rng.random() < 0.6:
                transfers.append({"card": cid, "to": self.rng.choice(legal["recipients"])})
        return {"transfers": transfers}

    def _jury_vote(self, legal: dict[str, Any]) -> dict[str, Any]:
        return {"vote": self.rng.choice(legal["finalists"])}
