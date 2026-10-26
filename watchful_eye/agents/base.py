from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..decisions import Decision


@dataclass
class DecisionRequest:
    kind: str
    player: str
    system_prompt: str
    user_prompt: str  # state update + decision request (or, on a retry, just the rejection)
    history_text: str  # what this decision should look like in the player's memory afterwards (events only)
    schema_model: type[Decision]
    legal: dict[str, Any]
    errors: list[str] = field(default_factory=list)  # validation errors from the previous attempt
    deadline: float | None = None  # absolute time.time() by which the decision is due (human players)
    validator: Callable[[dict[str, Any]], str | None] | None = None  # returns an error message or None


@dataclass
class AgentResponse:
    data: dict[str, Any] | None
    raw: str = ""
    cost: float = 0.0
    tokens: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0  # prompt tokens served from the provider's prompt cache
    cache_write_tokens: int = 0
    error: str | None = None
    transport_error: bool = False  # the request itself failed; nothing reached the model


class Agent(Protocol):
    name: str

    def decide(self, request: DecisionRequest) -> AgentResponse: ...

    def observe(self, text: str) -> None:
        """Tell the agent something that happened outside a decision (e.g. a move made for it)."""

    def commit(self, history_text: str, keep_reply: bool) -> None:
        """The decision is settled: collapse this decision's messages to the events-only record
        (plus the model's final reply if keep_reply). Position and legal options are not kept."""
