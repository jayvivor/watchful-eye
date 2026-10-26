from __future__ import annotations

import random

from .cards import Card, CardKind
from .config import DeckConfig


class Deck:
    def __init__(self, cfg: DeckConfig, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.draw_pile: list[Card] = []
        self.discard_pile: list[Card] = []
        self._next_id = 1
        self.initial_counts = cfg.counts()  # (kind, variant) -> n
        for (kind, variant), n in self.initial_counts.items():
            for _ in range(n):
                self.draw_pile.append(self._new(kind, variant))
        rng.shuffle(self.draw_pile)

    def _new(self, kind: CardKind, variant: str | None = None) -> Card:
        printed: dict = {}
        if kind is CardKind.SAFETY:
            printed = {"rounds": int(variant or 1)}
        elif kind is CardKind.PLEDGE:
            printed = {"category": variant or "nominate"}
        card = Card(id=f"c{self._next_id:03d}", kind=kind, printed=printed)
        self._next_id += 1
        return card

    def __len__(self) -> int:
        return len(self.draw_pile)

    def composition(self) -> str:
        parts = []
        for (kind, variant), n in self.initial_counts.items():
            parts.append(f"{kind.value}{f'({variant})' if variant else ''} x{n}")
        return ", ".join(parts)

    def draw(self) -> Card | None:
        if not self.draw_pile and self.cfg.reshuffle_discards and self.discard_pile:
            self.draw_pile, self.discard_pile = self.discard_pile, []
            self.rng.shuffle(self.draw_pile)
        if not self.draw_pile:
            return None
        return self.draw_pile.pop()

    def draw_kind(self, kind: CardKind) -> Card | None:
        """Pull a specific kind out of the draw pile (used for starting Jury Votes)."""
        for i, c in enumerate(self.draw_pile):
            if c.kind is kind:
                return self.draw_pile.pop(i)
        return None

    def mint(self, kind: CardKind) -> Card:
        """Create a card outside the deck (starting Jury Votes when jury_votes_in_deck=false)."""
        return self._new(kind)

    def discard(self, card: Card) -> None:
        # Pass-time terms and any writing are wiped; printed terms are permanent.
        card.params = {}
        card.writing = ""
        card.commitment_id = None
        self.discard_pile.append(card)
