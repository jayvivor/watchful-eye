from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .cards import Card

CommitmentKind = Literal["safety", "pledge", "declare"]
CommitmentStatus = Literal["active", "fulfilled", "violated", "resolved"]


@dataclass
class Commitment:
    """A binding promise the host tracks: a Safety or Pledge deal, or a public Declare."""

    id: str
    kind: CommitmentKind
    holder: str  # the player bound by it
    beneficiary: str | None  # the passer (deals) — the only one who may EXPOSE it
    card_id: str | None
    terms: dict[str, Any]
    created_round: int
    created_phase: str
    status: CommitmentStatus = "active"
    violation: str | None = None
    public: bool = False  # Declares are public; deals are known to both parties + host

    def describe(self) -> str:
        t = self.terms
        if self.kind == "safety":
            core = (
                f"{self.holder} may not nominate {t['protected']} for the next "
                f"{t['nominations_remaining']} Nomination phase(s)"
            )
        elif self.kind == "pledge":
            core = (
                f"{self.holder} must make a '{t['category']}' Declare before the end of "
                f"round {t['due_round']} {t['due_phase']}"
            )
        else:
            if t["category"] == "nominate":
                core = f"{self.holder} will nominate {', '.join(t['targets'])} next time they are Watcher"
            else:
                core = (
                    f"{self.holder} will vote to eliminate {t['target']} if the nominees are "
                    f"{t['pair'][0]} and {t['pair'][1]}"
                )
        return f"{self.id} [{self.kind}] {core}"

    def describe_status(self) -> str:
        s = f"{self.describe()} — status: {self.status}"
        if self.violation:
            s += f" ({self.violation})"
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "holder": self.holder,
            "beneficiary": self.beneficiary,
            "card_id": self.card_id,
            "terms": dict(self.terms),
            "status": self.status,
            "violation": self.violation,
            "created_round": self.created_round,
            "created_phase": self.created_phase,
        }


@dataclass
class PlayerState:
    name: str
    hand: list[Card] = field(default_factory=list)
    alive: bool = True
    eliminated_round: int | None = None
    eliminated_order: int | None = None  # 1 = first out
    train_banned_through_round: int = 0
    last_seen_seq: int = 0  # events up to here have been reported to this player's agent
    llm_calls: int = 0
    llm_fallbacks: int = 0
    llm_cost: float = 0.0
    llm_prompt_tokens: int = 0
    llm_cached_tokens: int = 0

    def card(self, card_id: str) -> Card | None:
        for c in self.hand:
            if c.id == card_id:
                return c
        return None

    def remove(self, card_id: str) -> Card:
        for i, c in enumerate(self.hand):
            if c.id == card_id:
                return self.hand.pop(i)
        raise KeyError(card_id)


@dataclass
class Event:
    seq: int
    round: int
    phase: str
    turn: int
    type: str
    public: str | None = None  # text everyone sees
    private: dict[str, str] = field(default_factory=dict)  # player -> text only they see
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "round": self.round,
            "phase": self.phase,
            "turn": self.turn,
            "type": self.type,
            "public": self.public,
            "private": dict(self.private),
            "data": self.data,
        }
