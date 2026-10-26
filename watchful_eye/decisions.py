"""Structured decision schemas. These are what agents must return (as JSON)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .cards import CardKind


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")
    thinking: str = Field(default="", description="Private reasoning. Never shown to other players.")


class DeclareTerms(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: Literal["nominate", "vote"]
    targets: list[str] | None = Field(
        default=None, description="nominate: 1-2 players you will nominate next time you are Watcher"
    )
    pair: list[str] | None = Field(default=None, description="vote: the two nominees this applies to")
    target: str | None = Field(default=None, description="vote: which of the pair you will vote against")


class ExposeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    commitment_id: str


OpenAction = Literal["draw_discard", "write", "play", "pass", "skip"]


class OpenTurnDecision(Decision):
    expose: ExposeRequest | None = Field(
        default=None, description="Free action: accuse a player of betraying a deal/declare you may expose"
    )
    action: OpenAction
    # draw_discard
    draw: bool = Field(default=False, description="draw_discard: draw one card")
    discard_card: str | None = Field(default=None, description="draw_discard: id of one card to discard")
    # write / play / pass
    card: str | None = Field(default=None, description="write/play/pass: card id from your hand")
    text: str | None = Field(default=None, description="write: words to add to the message card")
    to: str | None = Field(default=None, description="pass: recipient")
    play_as: CardKind | None = Field(default=None, description="play Wild: which action it becomes")
    # action parameters
    train_slot: str | None = Field(default=None, description="Train: eliminated player whose slot you claim")
    declare: DeclareTerms | None = Field(default=None, description="Declare: the binding terms")
    new_order: list[str] | None = Field(default=None, description="Shuffle: new move order (all living players)")
    throw_slot: str | None = Field(default=None, description="pass Throw: a spinner slot you reserve")


class RespondPassDecision(Decision):
    accept: bool = Field(description="Accept the card (ignored when the card cannot be declined)")
    declare: DeclareTerms | None = Field(
        default=None,
        description="Pledge only: accept AND immediately play the Pledge as a Declare of its printed category "
        "(out of turn). This is the only way a Pledge can be played.",
    )


class NominateDecision(Decision):
    nominees: list[str] = Field(description="Exactly two living players other than yourself")


class VoteDecision(Decision):
    vote: str = Field(description="The nominee you vote to ELIMINATE")


class TiebreakDecision(Decision):
    eliminate: str


class Transfer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    card: str
    to: str


class DistributeDecision(Decision):
    transfers: list[Transfer] = Field(
        default_factory=list, description="Cards to hand to survivors; everything else is discarded"
    )


class JuryVoteDecision(Decision):
    vote: str = Field(description="The finalist you want to WIN")


SCHEMAS: dict[str, type[Decision]] = {
    "open_turn": OpenTurnDecision,
    "respond_pass": RespondPassDecision,
    "nominate": NominateDecision,
    "vote": VoteDecision,
    "tiebreak": TiebreakDecision,
    "distribute": DistributeDecision,
    "jury_vote": JuryVoteDecision,
}


def json_schema(model: type[BaseModel], strict: bool) -> dict[str, Any]:
    """Pydantic JSON schema, optionally massaged into OpenAI-style strict form."""
    schema = model.model_json_schema()
    if strict:
        _make_strict(schema)
        for d in schema.get("$defs", {}).values():
            _make_strict(d)
    return schema


def _make_strict(obj: dict[str, Any]) -> None:
    if obj.get("type") == "object" and "properties" in obj:
        obj["additionalProperties"] = False
        obj["required"] = list(obj["properties"].keys())
        for p in obj["properties"].values():
            p.pop("default", None)
            _make_strict(p)
    for key in ("anyOf", "oneOf", "allOf"):
        for sub in obj.get(key, []):
            _make_strict(sub)
    if "items" in obj and isinstance(obj["items"], dict):
        _make_strict(obj["items"])
