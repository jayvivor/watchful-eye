"""YAML → pydantic configuration for a simulated game."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .cards import CardKind


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- LLM


class ModelConfig(StrictModel):
    """One OpenRouter model + sampling settings."""

    model: str = Field(description="OpenRouter model id, e.g. 'anthropic/claude-sonnet-4.5'")
    temperature: float | None = 0.8
    max_tokens: int | None = 2000
    reasoning: dict[str, Any] | None = Field(
        default=None, description="OpenRouter reasoning config, e.g. {effort: low}"
    )
    provider: dict[str, Any] | None = Field(
        default=None, description="OpenRouter provider preferences, e.g. {order: [...], allow_fallbacks: true}"
    )
    extra: dict[str, Any] = Field(
        default_factory=dict, description="Any other kwargs passed straight to chat.send()"
    )


class LLMConfig(StrictModel):
    api_key: str | None = Field(default=None, description="Literal key; prefer api_key_env.")
    api_key_env: str = "OPENROUTER_API_KEY"
    default_model: ModelConfig = Field(default_factory=lambda: ModelConfig(model="openai/gpt-4o-mini"))
    response_format: Literal["json_schema", "json_object", "none"] = Field(
        default="json_schema",
        description="How to ask for JSON. Falls back to 'none' automatically if the provider rejects it.",
    )
    strict_json: bool = Field(default=False, description="strict=true on the json_schema response format")
    max_retries: int = Field(default=2, description="Re-prompts after an invalid/illegal decision")
    fallback_to_random: bool = Field(
        default=True, description="If all retries fail, make a random legal move instead of aborting"
    )
    cache_control: bool = Field(
        default=True,
        description="Send OpenRouter's top-level cache_control so the growing per-player transcript is "
        "prompt-cached (Anthropic needs this; OpenAI/Gemini/DeepSeek cache automatically).",
    )
    max_history_messages: int | None = Field(
        default=None,
        description="Each player keeps their full conversation (every state update and every reply). "
        "Set a number to keep only the most recent N messages after the system prompt.",
    )
    app_title: str = "Watchful Eye Simulator"
    request_timeout_s: float = 120.0
    log_raw_responses: bool = Field(default=True, description="Write every LLM reply to llm_calls.jsonl")
    log_prompts: bool = Field(default=False, description="Also write the full prompts (large) to llm_calls.jsonl")

    def resolve_api_key(self) -> str | None:
        return self.api_key or os.environ.get(self.api_key_env)


# ----------------------------------------------------------------------- players


class PlayerConfig(StrictModel):
    name: str
    agent: Literal["llm", "random"] = "llm"
    model: str | ModelConfig | None = Field(
        default=None, description="Model id string or full ModelConfig; None = llm.default_model"
    )
    persona: str | None = Field(default=None, description="Optional play-style instructions for this player")

    def resolved_model(self, default: ModelConfig) -> ModelConfig:
        if self.model is None:
            return default
        if isinstance(self.model, str):
            return default.model_copy(update={"model": self.model})
        return self.model


# -------------------------------------------------------------------------- deck

# Cards whose terms are pre-printed come in variants: Safety by round count, Pledge by category.
VARIANT_KINDS: dict[CardKind, str] = {CardKind.SAFETY: "rounds", CardKind.PLEDGE: "category"}
PLEDGE_CATEGORIES = ("nominate", "vote")

CardSpec = float | dict[int | str, float]  # plain count/weight, or {variant: count/weight}


class DeckConfig(StrictModel):
    """Either exact counts (size=None) or weights normalised to `size` cards."""

    size: int | None = Field(
        default=None,
        description="If set, `cards` are relative weights and the deck is scaled to this many cards. "
        "If None, `cards` are exact counts.",
    )
    cards: dict[CardKind, CardSpec] = Field(
        default_factory=lambda: {
            CardKind.TRAIN: 6,
            CardKind.DECLARE: 6,
            CardKind.MIST: 3,
            CardKind.SHUFFLE: 4,
            CardKind.EXCHANGE: 3,
            CardKind.WILD: 3,
            CardKind.TEA: 10,
            CardKind.MANIFESTO: 2,
            CardKind.CHIT_CHAT: 6,
            CardKind.THROW: 4,
            CardKind.JURY_VOTE: 6,
            CardKind.SAFETY: {"1": 2, "2": 2, "3": 2},
            CardKind.PLEDGE: {"nominate": 3, "vote": 2},
        },
        description="Safety and Pledge take a {variant: count} map (rounds printed / category printed).",
    )
    starting_hand: int = Field(default=5, description="Random cards dealt to each player at the start")
    starting_jury_votes: int = Field(default=1, description="Jury Vote cards dealt to each player at the start")
    jury_votes_in_deck: bool = Field(
        default=True, description="If false, Jury Votes only exist as starting cards (deck count ignored)"
    )
    reshuffle_discards: bool = Field(
        default=True, description="When the draw pile is empty, shuffle the discard pile back in"
    )

    @field_validator("cards")
    @classmethod
    def _check_specs(cls, v: dict[CardKind, CardSpec]) -> dict[CardKind, CardSpec]:
        out: dict[CardKind, CardSpec] = {}
        for kind, spec in v.items():
            if kind in VARIANT_KINDS:
                if not isinstance(spec, dict):
                    raise ValueError(
                        f"deck.cards[{kind.value}] must be a map of {VARIANT_KINDS[kind]} -> count, "
                        f"e.g. {{1: 2, 2: 2}} for Safety or {{nominate: 3, vote: 2}} for Pledge"
                    )
                clean: dict[str, float] = {}
                for variant, n in spec.items():
                    key = str(variant)
                    if kind is CardKind.SAFETY and (not key.isdigit() or int(key) < 1):
                        raise ValueError(f"Safety variant {variant!r} must be a positive round count")
                    if kind is CardKind.PLEDGE and key not in PLEDGE_CATEGORIES:
                        raise ValueError(f"Pledge variant {variant!r} must be one of {PLEDGE_CATEGORIES}")
                    if n < 0:
                        raise ValueError(f"deck.cards[{kind.value}][{key}] must be >= 0")
                    clean[key] = n
                out[kind] = clean
            else:
                if isinstance(spec, dict):
                    raise ValueError(f"deck.cards[{kind.value}] has no variants; give a single number")
                if spec < 0:
                    raise ValueError(f"deck.cards[{kind.value}] must be >= 0")
                out[kind] = spec
        return out

    def _flat(self) -> dict[tuple[CardKind, str | None], float]:
        flat: dict[tuple[CardKind, str | None], float] = {}
        for kind, spec in self.cards.items():
            if kind is CardKind.JURY_VOTE and not self.jury_votes_in_deck:
                continue
            if isinstance(spec, dict):
                for variant, n in spec.items():
                    flat[(kind, variant)] = n
            else:
                flat[(kind, None)] = spec
        return flat

    def counts(self) -> dict[tuple[CardKind, str | None], int]:
        """(kind, variant) -> number of physical cards."""
        flat = self._flat()
        if self.size is None:
            return {k: int(round(n)) for k, n in flat.items() if n > 0}
        total = sum(flat.values())
        if total <= 0:
            raise ValueError("deck.cards weights sum to zero")
        raw = {k: n / total * self.size for k, n in flat.items() if n > 0}
        floors = {k: int(x) for k, x in raw.items()}
        remainder = self.size - sum(floors.values())
        for k in sorted(raw, key=lambda k: raw[k] - floors[k], reverse=True)[:remainder]:
            floors[k] += 1
        return {k: n for k, n in floors.items() if n > 0}

    def counts_by_kind(self) -> dict[CardKind, int]:
        by_kind: dict[CardKind, int] = {}
        for (kind, _), n in self.counts().items():
            by_kind[kind] = by_kind.get(kind, 0) + n
        return by_kind


# ------------------------------------------------------------------------- rules


class RulesConfig(StrictModel):
    open_turns: int = Field(default=5, description="Turns per Open phase")
    turn_scope: Literal["each_player", "total", "chaos"] = Field(
        default="each_player",
        description="'each_player': every player acts once per turn (5 turns = 5 actions each). "
        "'total': 5 individual actions per phase in move order. "
        "'chaos': no move order; everyone has `open_turns` actions and `chaos_minutes` to use them.",
    )
    move_order: Literal["random", "seating"] = Field(
        default="random", description="Initial move order: shuffled, or the order players were listed/joined"
    )
    move_wait_s: float = Field(default=30.0, description="Seconds a human player has to make a decision")
    chaos_minutes: float = Field(default=5.0, description="Length of each Open phase in 'chaos' turn scope")
    public_chat: bool = Field(default=False, description="Web play: living players share a public chat room")
    tea_max_words: int = 3
    manifesto_max_words: int | None = None
    repeat_watcher_allowed_at_players: int = Field(
        default=4,
        description="No player may be Watcher in consecutive rounds while MORE than this many players remain "
        "(default 4 => the rule lifts for the penultimate [4-player] and final [3-player] rounds).",
    )
    deal_enforcement: Literal["expose", "auto"] = Field(
        default="expose",
        description="'expose': betrayals of deals AND declares only bite when EXPOSEd (rules as written). "
        "'auto': the host applies penalties the moment a term is violated (useful for debugging).",
    )
    reveal_votes: Literal["count", "full", "none"] = Field(
        default="count", description="What the table learns after a Vote phase"
    )
    eliminated_distribution: bool = Field(
        default=True, description="Eliminated players may hand their cards to survivors (rules say yes)"
    )
    history_events: int = Field(
        default=80,
        description="Public events shown to a player with no conversation history (random fallback views, "
        "trimmed histories). LLM players normally see every event through their own transcript.",
    )
    max_rounds: int = Field(default=60, description="Safety valve against runaway games")


class OutputConfig(StrictModel):
    dir: str = "runs"
    transcript: bool = True
    events: bool = True
    verbose: bool = Field(default=True, description="Print the transcript to stdout as it happens")
    print_llm_thinking: bool = Field(default=False, description="Echo each LLM 'thinking' field to stdout")


class GameConfig(StrictModel):
    name: str = "watchful-eye"
    seed: int | None = None
    players: list[PlayerConfig] = Field(min_length=3)
    deck: DeckConfig = Field(default_factory=DeckConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)

    @model_validator(mode="after")
    def _check(self) -> "GameConfig":
        names = [p.name for p in self.players]
        if len(set(names)) != len(names):
            raise ValueError("player names must be unique")
        for n in names:
            if not n or any(ch in n for ch in " \t\n:"):
                raise ValueError(f"player name {n!r} must be non-empty and contain no spaces or colons")
        by_kind = self.deck.counts_by_kind()
        jv = by_kind.get(CardKind.JURY_VOTE, 0)
        need = len(names) * self.deck.starting_hand
        have = sum(by_kind.values()) - (jv if self.deck.jury_votes_in_deck else 0)
        if have < need:
            raise ValueError(f"deck has {have} non-Jury cards but {need} are needed for starting hands")
        if self.deck.jury_votes_in_deck:
            jv_need = len(names) * self.deck.starting_jury_votes
            if jv < jv_need:
                raise ValueError(f"deck has {jv} Jury Votes but {jv_need} are dealt at start")
        return self


def load_config(path: str | Path) -> GameConfig:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return GameConfig.model_validate(data)
