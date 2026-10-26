"""Card definitions. Card *behaviour* lives in the engine; this module is the catalogue."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CardKind(str, Enum):
    # Actions
    TRAIN = "Train"
    DECLARE = "Declare"
    MIST = "Mist"
    SHUFFLE = "Shuffle"
    EXCHANGE = "Exchange"
    WILD = "Wild"
    # Messages
    TEA = "Tea"
    MANIFESTO = "Manifesto"
    # Gifts
    CHIT_CHAT = "Chit-Chat"
    THROW = "Throw"
    JURY_VOTE = "Jury Vote"
    # Deals
    SAFETY = "Safety"
    PLEDGE = "Pledge"


class Category(str, Enum):
    ACTION = "action"
    MESSAGE = "message"
    GIFT = "gift"
    DEAL = "deal"


CATEGORY: dict[CardKind, Category] = {
    CardKind.TRAIN: Category.ACTION,
    CardKind.DECLARE: Category.ACTION,
    CardKind.MIST: Category.ACTION,
    CardKind.SHUFFLE: Category.ACTION,
    CardKind.EXCHANGE: Category.ACTION,
    CardKind.WILD: Category.ACTION,
    CardKind.TEA: Category.MESSAGE,
    CardKind.MANIFESTO: Category.MESSAGE,
    CardKind.CHIT_CHAT: Category.GIFT,
    CardKind.THROW: Category.GIFT,
    CardKind.JURY_VOTE: Category.GIFT,
    CardKind.SAFETY: Category.DEAL,
    CardKind.PLEDGE: Category.DEAL,
}

ACTION_KINDS = [k for k, c in CATEGORY.items() if c is Category.ACTION]
# Kinds a Wild may substitute for (everything playable except Wild itself).
WILD_TARGETS = [k for k in ACTION_KINDS if k is not CardKind.WILD]

# One-line rules text per card, used in prompts and in the README.
CARD_TEXT: dict[CardKind, str] = {
    CardKind.TRAIN: "Claim an eliminated player's space on the spinner for the next Competition.",
    CardKind.DECLARE: (
        "Publicly and bindingly state your plans: either who you will nominate the next time you are "
        "Watcher, or who you will vote against between a named pair of nominees (current or future). "
        "Betrayal penalty: your entire hand is shown."
    ),
    CardKind.MIST: (
        "Activate Mist for yourself: every deal card you pass must be accepted. Lasts until another "
        "Mist is played or the next eviction, whichever comes first."
    ),
    CardKind.SHUFFLE: "Change the move order for all future turns.",
    CardKind.EXCHANGE: (
        "Replace your entire hand with 5 new cards. For existing deals this counts as discarding them."
    ),
    CardKind.WILD: "Substitute for any action card.",
    CardKind.TEA: "Message card: write up to 3 words total on it before delivery. Words cannot be erased.",
    CardKind.MANIFESTO: "Message card: write a message of any length on it. Words cannot be erased. Rare.",
    CardKind.CHIT_CHAT: "Gift: does nothing. Cannot be declined.",
    CardKind.THROW: (
        "Gift: the gifter writes a spinner slot they have reserved. At the next Competition, if the "
        "spinner lands on that slot the win goes to the giftee instead. Cannot be declined."
    ),
    CardKind.JURY_VOTE: (
        "Gift: if the holder is in the final 2, this counts as one vote for them. No effect before then. "
        "Cannot be declined."
    ),
    CardKind.SAFETY: (
        "Deal: the recipient may not nominate the passer for the next N Nomination phases (N is printed on "
        "the card). Betrayal penalty: the betrayer may not play Train for N rounds."
    ),
    CardKind.PLEDGE: (
        "Deal: the recipient must make a Declare of the printed category (nominate/vote) before the current "
        "phase ends. At the moment of accepting, the recipient may instead play the Pledge itself as that "
        "Declare, out of turn — the only way a Pledge can ever be played. Betrayal penalty: every message "
        "card in the betrayer's hand is instantly played (revealed and discarded)."
    ),
}


@dataclass
class Card:
    id: str
    kind: CardKind
    # Pre-printed on the card and permanent: Safety -> {"rounds": N}, Pledge -> {"category": "nominate"|"vote"}
    printed: dict[str, Any] = field(default_factory=dict)
    writing: str = ""  # message text; wiped when the card is discarded
    params: dict[str, Any] = field(default_factory=dict)  # written at pass time (Throw's slot)
    # id of the Commitment this card currently backs (active deal held by the recipient)
    commitment_id: str | None = None

    @property
    def category(self) -> Category:
        return CATEGORY[self.kind]

    @property
    def variant(self) -> str | None:
        if self.kind is CardKind.SAFETY:
            return str(self.printed["rounds"])
        if self.kind is CardKind.PLEDGE:
            return str(self.printed["category"])
        return None

    def label(self) -> str:
        """Kind plus printed terms, e.g. 'Safety (2 rounds)', 'Pledge (vote)'."""
        if self.kind is CardKind.SAFETY:
            return f"Safety ({self.printed['rounds']} round{'s' if self.printed['rounds'] != 1 else ''})"
        if self.kind is CardKind.PLEDGE:
            return f"Pledge ({self.printed['category']})"
        return self.kind.value

    def public(self) -> str:
        """How the table sees a revealed card: printed face and writing, no id, no deal bookkeeping."""
        s = self.label()
        if self.category is Category.MESSAGE:
            s += f' reading "{self.writing}"' if self.writing else " (blank)"
        if self.kind is CardKind.THROW and self.params.get("slot"):
            s += f" naming {self.params['slot']}'s slot"
        return s

    def describe(self) -> str:
        s = f"{self.id}: {self.label()}"
        if self.category is Category.MESSAGE:
            s += f' [writing: "{self.writing}"]' if self.writing else " [blank]"
        if self.params:
            s += " " + ", ".join(f"{k}={v}" for k, v in self.params.items())
        if self.commitment_id:
            s += f" (active deal {self.commitment_id})"
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "label": self.label(),
            "category": self.category.value,
            "printed": dict(self.printed),
            "writing": self.writing,
            "params": dict(self.params),
            "commitment_id": self.commitment_id,
        }
