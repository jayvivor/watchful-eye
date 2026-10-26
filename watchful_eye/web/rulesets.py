"""Room options → GameConfig. All the numbers live in rulesets.json next to this file."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..cards import CardKind
from ..config import DeckConfig, GameConfig, LLMConfig, OutputConfig, PlayerConfig, RulesConfig

RULESETS: dict[str, Any] = json.loads((Path(__file__).with_name("rulesets.json")).read_text(encoding="utf-8"))

_CAST = RULESETS["cast"]
_OPT = RULESETS["options"]
DEFAULT_CAST: int = _CAST["default"]
MIN_CAST: int = _CAST["min"]
MAX_CAST: int = _CAST["max"]

PRESET_NAMES: list[str] = list(RULESETS["presets"])
RULESET_LABELS: dict[str, str] = {k: v["label"] for k, v in RULESETS["presets"].items()} | {"custom": "Custom"}
RULESET_BLURBS: dict[str, str] = {k: v["blurb"] for k, v in RULESETS["presets"].items()}

# "Safety": {"1": 24, ...} in JSON → {CardKind.SAFETY: {"1": 24, ...}}; plain numbers pass through.
DECK_WEIGHTS: dict[CardKind, Any] = {CardKind(name): spec for name, spec in RULESETS["deck"].items()}

# Base rules shared by every ruleset; RulesConfig and DeckConfig each take the keys they know.
_BASE = RULESETS["rules"]
_RULE_KEYS = set(RulesConfig.model_fields)
_DECK_KEYS = set(DeckConfig.model_fields)
BASE_RULES = {k: v for k, v in _BASE.items() if k in _RULE_KEYS}
BASE_DECK = {k: v for k, v in _BASE.items() if k in _DECK_KEYS}


class RoomOptions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ruleset: str = "silent"
    cast: int = Field(default=DEFAULT_CAST, ge=MIN_CAST, le=MAX_CAST)
    deck_size: int = Field(default=_OPT["deck_size"]["default"], ge=_OPT["deck_size"]["min"], le=_OPT["deck_size"]["max"])
    move_wait_s: float = Field(default=_OPT["move_wait_s"]["default"], ge=_OPT["move_wait_s"]["min"], le=_OPT["move_wait_s"]["max"])
    chaos_minutes: float = Field(default=_OPT["chaos_minutes"]["default"], ge=_OPT["chaos_minutes"]["min"], le=_OPT["chaos_minutes"]["max"])
    public_chat: bool = _OPT["public_chat"]["default"]
    move_order: str = _OPT["move_order"]["default"]

    @field_validator("ruleset")
    @classmethod
    def _known_ruleset(cls, v: str) -> str:
        if v != "custom" and v not in RULESETS["presets"]:
            raise ValueError(f"unknown ruleset {v!r}; choose one of {PRESET_NAMES} or 'custom'")
        return v

    @field_validator("move_order")
    @classmethod
    def _known_order(cls, v: str) -> str:
        if v not in _OPT["move_order"]["choices"]:
            raise ValueError(f"move_order must be one of {_OPT['move_order']['choices']}")
        return v

    @property
    def label(self) -> str:
        return RULESET_LABELS.get(self.ruleset, "Custom")


def preset(ruleset: str) -> RoomOptions:
    spec = {k: v for k, v in RULESETS["presets"][ruleset].items() if k not in ("label", "blurb")}
    return RoomOptions(ruleset=ruleset, **spec)


def game_config(options: RoomOptions, names: list[str], seed: int | None, run_dir_parent: str) -> GameConfig:
    rules = RulesConfig(
        **BASE_RULES,
        move_wait_s=options.move_wait_s,
        public_chat=options.public_chat,
        chaos_minutes=options.chaos_minutes,
        turn_scope="chaos" if options.move_order == "none" else "each_player",
        move_order="seating" if options.move_order == "seating" else "random",
    )
    deck = DeckConfig(**BASE_DECK, size=options.deck_size, cards=DECK_WEIGHTS, jury_votes_in_deck=False)
    return GameConfig(
        name=f"web-{options.ruleset}",
        seed=seed,
        players=[PlayerConfig(name=n, agent="random") for n in names],
        deck=deck,
        rules=rules,
        llm=LLMConfig(max_retries=0, log_raw_responses=False),
        output=OutputConfig(dir=run_dir_parent, verbose=False, transcript=True, events=True),
    )