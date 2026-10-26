"""The host: runs rounds and phases, validates every decision, enforces deals and declares."""

from __future__ import annotations

import contextlib
import json
import random
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .agents.base import Agent, DecisionRequest
from .agents.random_agent import RandomAgent
from .cards import WILD_TARGETS, Card, CardKind, Category
from .config import GameConfig
from .deck import Deck
from .decisions import (
    SCHEMAS,
    DeclareTerms,
    Decision,
    DistributeDecision,
    JuryVoteDecision,
    NominateDecision,
    OpenTurnDecision,
    RespondPassDecision,
    TiebreakDecision,
    VoteDecision,
)
from .prompts import DECISION_INSTRUCTIONS, system_prompt
from .state import Commitment, Event, PlayerState

PHASE_COMPETITION = "competition"
PHASE_OPEN1 = "open1"
PHASE_NOMINATION = "nomination"
PHASE_OPEN2 = "open2"
PHASE_VOTE = "vote"
PHASE_FINALE = "finale"


class GameError(RuntimeError):
    pass


class Game:
    def __init__(
        self,
        cfg: GameConfig,
        agents: dict[str, Agent] | None = None,
        dry_run: bool = False,
        run_dir: str | Path | None = None,
    ):
        self.cfg = cfg
        self.seed = cfg.seed if cfg.seed is not None else random.randrange(1 << 31)
        self.rng = random.Random(self.seed)
        self.dry_run = dry_run

        self.players: dict[str, PlayerState] = {p.name: PlayerState(name=p.name) for p in cfg.players}
        self.slots: list[str] = [p.name for p in cfg.players]  # spinner slots, one per original player
        self.order: list[str] = list(self.slots)
        if cfg.rules.move_order == "random":
            self.rng.shuffle(self.order)
        self._order_pos = 0

        # Concurrency (web play): every state mutation happens under this lock; waiting for a
        # decision never does. on_event lets a room forward events as they happen.
        self.lock = threading.RLock()
        self.on_event: Any = None
        self.phase_deadline: float | None = None  # chaos Open phases
        self.chaos_actions_left: dict[str, int] = {}
        self.pending: dict[str, dict[str, Any]] = {}  # player -> {kind, deadline} currently being asked
        self.active_player: str | None = None

        self.deck = Deck(cfg.deck, self.rng)
        self.round = 0
        self.phase = "setup"
        self.turn = 0
        self.watcher: str | None = None
        self.last_watcher: str | None = None
        self.nominees: list[str] = []
        self.mist_holder: str | None = None
        self.train_claims: dict[str, str] = {}  # slot -> claimant, for the next competition
        self.throws: list[dict[str, str]] = []  # {slot, gifter, giftee}, for the next competition
        self.commitments: dict[str, Commitment] = {}
        self._next_commitment = 1
        self.events: list[Event] = []
        self.eliminated_order: list[str] = []
        self.winner: str | None = None
        self.total_cost = 0.0

        self.random_agent = RandomAgent(self.rng)
        self.agents: dict[str, Agent] = agents or self._build_agents()
        self._system_prompts = {p.name: system_prompt(cfg, p.name, p.persona) for p in cfg.players}

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = Path(run_dir) if run_dir else Path(cfg.output.dir) / f"{cfg.name}-{stamp}-{self.seed}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._events_fh = open(self.run_dir / "events.jsonl", "w", encoding="utf-8") if cfg.output.events else None
        self._transcript_fh = open(self.run_dir / "transcript.txt", "w", encoding="utf-8") if cfg.output.transcript else None
        self._llm_fh = open(self.run_dir / "llm_calls.jsonl", "w", encoding="utf-8") if cfg.llm.log_raw_responses else None
        with open(self.run_dir / "config.yaml", "w", encoding="utf-8") as fh:
            yaml.safe_dump(json.loads(cfg.model_dump_json()), fh, sort_keys=False)

    # ================================================================== setup

    def _build_agents(self) -> dict[str, Agent]:
        agents: dict[str, Agent] = {}
        api_key = self.cfg.llm.resolve_api_key()
        for p in self.cfg.players:
            if self.dry_run or p.agent == "random":
                agents[p.name] = self.random_agent
            else:
                if not api_key:
                    raise GameError(
                        f"Player {p.name} is an LLM but no API key was found "
                        f"(set ${self.cfg.llm.api_key_env} or llm.api_key, or use --dry-run)"
                    )
                from .agents.openrouter_agent import OpenRouterAgent

                agents[p.name] = OpenRouterAgent(p.name, p.resolved_model(self.cfg.llm.default_model), self.cfg.llm, api_key)
        return agents

    def _deal_starting_hands(self) -> None:
        d = self.cfg.deck
        for name in self.order:
            ps = self.players[name]
            for _ in range(d.starting_jury_votes):
                card = self.deck.draw_kind(CardKind.JURY_VOTE) if d.jury_votes_in_deck else None
                ps.hand.append(card or self.deck.mint(CardKind.JURY_VOTE))
            for _ in range(d.starting_hand):
                card = self.deck.draw()
                if card is None:
                    raise GameError("deck ran out while dealing starting hands")
                ps.hand.append(card)
        for name in self.order:
            ps = self.players[name]
            self._event("deal", private={name: "Your starting hand: " + "; ".join(c.describe() for c in ps.hand)},
                        data={"player": name, "hand": [c.to_dict() for c in ps.hand]})

    # ==================================================================== run

    def run(self) -> dict[str, Any]:
        self._event(
            "start",
            public=f"The game begins with {len(self.players)} players: {', '.join(self.slots)}. Move order: {', '.join(self.order)}.",
            data={"seed": self.seed, "order": list(self.order)},
        )
        self._event(
            "deck",
            public=f"The deck holds {len(self.deck)} cards: {self.deck.composition()}.",
            data={"deck": {f"{k.value}{f'({v})' if v else ''}": n for (k, v), n in self.deck.initial_counts.items()}},
        )
        self._deal_starting_hands()
        try:
            while len(self.alive()) > 2:
                if self.round >= self.cfg.rules.max_rounds:
                    raise GameError("max_rounds reached")
                self._round()
            self._finale()
        finally:
            result = self._result()
            with open(self.run_dir / "result.json", "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
            for fh in (self._events_fh, self._transcript_fh, self._llm_fh):
                if fh:
                    fh.close()
        return result

    def _result(self) -> dict[str, Any]:
        return {
            "name": self.cfg.name,
            "seed": self.seed,
            "winner": self.winner,
            "rounds": self.round,
            "finalists": [p for p in self.players if self.players[p].alive],
            "eliminated_order": list(self.eliminated_order),
            "jury_votes": {p: self._jury_count(p) for p in self.players if self.players[p].alive},
            "commitments": [c.to_dict() for c in self.commitments.values()],
            "llm": {p: {"calls": s.llm_calls, "fallbacks": s.llm_fallbacks, "cost_usd": round(s.llm_cost, 6),
                        "prompt_tokens": s.llm_prompt_tokens, "cached_tokens": s.llm_cached_tokens,
                        "cache_hit_rate": round(s.llm_cached_tokens / s.llm_prompt_tokens, 3) if s.llm_prompt_tokens else None}
                    for p, s in self.players.items()},
            "total_cost_usd": round(self.total_cost, 6),
            "run_dir": str(self.run_dir),
        }

    # ================================================================== round

    def _round(self) -> None:
        self.round += 1
        self.turn = 0
        n = len(self.alive())
        self._event("round", public=f"Round {self.round} — {n} remain: {', '.join(self.alive())}")
        self._competition()
        if n > 3:
            self._open_phase(PHASE_OPEN1)
            self._nomination()
        else:
            with self.lock:
                self.phase = PHASE_NOMINATION
                self.nominees = [p for p in self.alive() if p != self.watcher]
                self._event("nominate",
                            public=f"Final three: Nomination is skipped. {self.nominees[0]} and {self.nominees[1]} are automatically the nominees.",
                            data={"watcher": self.watcher, "nominees": list(self.nominees), "forced": True})
                self._after_nomination(chosen=False)
        self._open_phase(PHASE_OPEN2)
        self._vote()

    # ------------------------------------------------------------ competition

    def _competition(self) -> None:
        with self.lock:
            self.phase = PHASE_COMPETITION
            self.turn = 1
            alive = self.alive()
            # self.watcher still holds LAST round's Watcher at this point; that is who may not repeat.
            barred = self.watcher if len(alive) > self.cfg.rules.repeat_watcher_allowed_at_players else None
            if barred and not self.players[barred].alive:
                barred = None

            def outcome(slot: str) -> tuple[str | None, str]:
                owner = self._slot_owner(slot)
                if owner is None:
                    return None, f"{slot}'s slot is empty"
                if owner == barred:
                    return None, f"{owner} was Watcher last round and may not repeat"
                for t in self.throws:
                    if t["slot"] == slot and t["gifter"] == owner:
                        g = t["giftee"]
                        if not self.players[g].alive:
                            return None, f"{owner}'s slot was thrown to {g}, who is eliminated"
                        if g == barred:
                            return None, f"{owner}'s slot was thrown to {g}, who may not repeat as Watcher"
                        return g, f"{owner}'s slot — but it was THROWN to {g}"
                return owner, f"{owner}'s slot" + (f" (claimed from {slot})" if owner != slot else "")

            if all(outcome(s)[0] is None for s in self.slots):
                barred = None  # cannot happen in practice, but never deadlock
            spins = 0
            while True:
                spins += 1
                slot = self.rng.choice(self.slots)
                winner, why = outcome(slot)
                if winner is None:
                    self._event("spin", public=f"The spinner lands on {why}. Spin again.", data={"slot": slot})
                    continue
                break
            self.last_watcher, self.watcher = self.watcher, winner
            self.nominees = []
            self._event("watcher",
                        public=f"The spinner lands on {why}. {winner} is the WATCHER for round {self.round} and cannot be eliminated.",
                        data={"slot": slot, "watcher": winner, "spins": spins, "claims": dict(self.train_claims), "throws": list(self.throws)})
            self.train_claims.clear()
            self.throws.clear()

    def _slot_owner(self, slot: str) -> str | None:
        if slot in self.train_claims:
            c = self.train_claims[slot]
            return c if self.players[c].alive else None
        return slot if self.players[slot].alive else None

    def _reserved_slots(self, player: str) -> list[str]:
        slots = [player] if self.players[player].alive else []
        slots += [s for s, c in self.train_claims.items() if c == player]
        return slots

    # ------------------------------------------------------------------ open

    def _open_phase(self, key: str) -> None:
        self.phase = key
        r = self.cfg.rules
        self._event("phase", public=f"Open phase {'I' if key == PHASE_OPEN1 else 'II'}. Move order: {', '.join(self.order)}.")
        if r.turn_scope == "each_player":
            for turn in range(1, r.open_turns + 1):
                self.turn = turn
                for p in list(self.order):  # snapshot: Shuffle applies from the next turn
                    if self.players[p].alive:
                        self._open_turn(p)
        elif r.turn_scope == "total":
            for turn in range(1, r.open_turns + 1):
                self.turn = turn
                p = self.order[self._order_pos % len(self.order)]
                self._order_pos += 1
                self._open_turn(p)
        else:
            self._chaos_phase()
        with self.lock:
            self._end_of_phase_checks()

    def _chaos_phase(self) -> None:
        """No move order: every living player has `open_turns` actions and a shared clock."""
        r = self.cfg.rules
        self.turn = 0
        self.phase_deadline = time.time() + r.chaos_minutes * 60
        alive = self.alive()
        self.chaos_actions_left = {p: r.open_turns for p in alive}
        self._event("chaos", public=f"Everyone has {r.open_turns} actions and {r.chaos_minutes:g} minutes. Go.",
                    data={"deadline": self.phase_deadline})

        def run(p: str) -> None:
            while self.chaos_actions_left.get(p, 0) > 0 and self.players[p].alive:
                if time.time() >= self.phase_deadline:  # type: ignore[operator]
                    break
                self._open_turn(p)
                self.chaos_actions_left[p] -= 1
            left = self.chaos_actions_left.get(p, 0)
            if left and self.players[p].alive:
                self._event("skip", public=f"Time is up for {p} with {left} action(s) unused.")

        threads = [threading.Thread(target=run, args=(p,), name=f"chaos-{p}", daemon=True) for p in alive]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.phase_deadline = None
        self.chaos_actions_left = {}

    def _end_of_phase_checks(self) -> None:
        for c in list(self.commitments.values()):
            if c.kind == "pledge" and c.status == "active":
                if (c.terms["due_round"], c.terms["due_phase"]) == (self.round, self.phase):
                    self._violate(c, "the pledged Declare was not made before the phase ended")

    def _open_turn(self, player: str) -> None:
        ps = self.players[player]
        for _ in range(5):
            with self.lock:
                legal = self._open_legal(player)
            self.active_player = player
            d: OpenTurnDecision = self._decide(player, "open_turn", legal)  # type: ignore[assignment]
            with self.lock:
                if self.cfg.rules.turn_scope == "chaos":
                    # Other players acted while this one was deciding; make sure the move is still legal.
                    err = self._validate_open(player, d, self._open_legal(player))
                    if err:
                        agent = self.agents[player]
                        if getattr(agent, "is_human", False):
                            agent.observe(f"That move is no longer available: {err}")
                            continue
                        d = OpenTurnDecision(action="skip")
                if d.expose:
                    self._expose(player, d.expose.commitment_id)
                    if d.action in ("write", "play", "pass") and ps.card(d.card) is None:  # type: ignore[arg-type]
                        self._event("skip", public=f"{player} no longer holds the card they meant to use; their turn ends.")
                        return
                if d.action != "pass":
                    self._execute_simple(player, d)
                    return
            self._pass(player, d)  # takes the lock itself, in two segments around the recipient's answer
            return
        with self.lock:
            self._event("skip", public=f"{player} could not find a legal move in time; their action is skipped.")

    def _execute_simple(self, player: str, d: OpenTurnDecision) -> None:
        ps = self.players[player]
        a = d.action
        if a == "skip":
            self._event("skip", public=f"{player} does nothing this turn.")
        elif a == "draw_discard":
            if d.draw:
                card = self.deck.draw()
                if card is None:
                    self._event("draw", public=f"{player} tries to draw, but the deck is empty.")
                else:
                    ps.hand.append(card)
                    self._event("draw", public=f"{player} draws a card ({len(ps.hand)} in hand).",
                                private={player: f"You drew {card.describe()}."},
                                data={"player": player, "card": card.to_dict()})
            if d.discard_card:
                self._discard_from_hand(player, d.discard_card)
        elif a == "write":
            card = ps.card(d.card)  # type: ignore[arg-type]
            assert card is not None and d.text is not None
            card.writing = (card.writing + " " + d.text.strip()).strip()
            self._event("write", public=f"{player} writes on a card.",
                        private={player: f"{card.id} now reads: \"{card.writing}\""},
                        data={"player": player, "card": card.to_dict()})
        elif a == "play":
            self._play(player, d)

    # ----------------------------------------------------------- legal moves

    def _open_legal(self, player: str) -> dict[str, Any]:
        ps = self.players[player]
        r = self.cfg.rules
        others = [p for p in self.alive() if p != player]
        active_deals = {c.id for c in ps.hand if c.commitment_id and self.commitments[c.commitment_id].status == "active"}
        train_ok = ps.train_banned_through_round < self.round
        train_slots = [s for s in self.slots if self._slot_owner(s) is None and s not in self.train_claims] if train_ok else []

        def action_ok(kind: CardKind) -> bool:
            if kind is CardKind.TRAIN:
                return bool(train_slots)
            if kind is CardKind.DECLARE:
                return len(others) >= 2
            if kind is CardKind.SHUFFLE:
                return r.turn_scope != "chaos"
            return True

        playable: dict[str, list[str]] = {}
        for c in ps.hand:
            if c.kind is CardKind.WILD:
                kinds = [k.value for k in WILD_TARGETS if action_ok(k)]
                if kinds:
                    playable[c.id] = kinds
            elif c.category is Category.ACTION:
                if action_ok(c.kind):
                    playable[c.id] = [c.kind.value]
            elif c.category is Category.MESSAGE:
                playable[c.id] = [c.kind.value]

        writable = []
        for c in ps.hand:
            if c.kind is CardKind.TEA and len(c.writing.split()) < r.tea_max_words:
                writable.append(c.id)
            elif c.kind is CardKind.MANIFESTO and (r.manifesto_max_words is None or len(c.writing.split()) < r.manifesto_max_words):
                writable.append(c.id)

        thrown = {t["slot"] for t in self.throws if t["gifter"] == player}
        exposable = [
            c.id for c in self.commitments.values()
            if c.status in ("active", "violated") and self.players[c.holder].alive
            and ((c.kind in ("safety", "pledge") and c.beneficiary == player) or (c.kind == "declare" and c.holder != player))
        ]
        return {
            "hand": [self._card_view(c) for c in ps.hand],
            "card_kind": {c.id: c.kind.value for c in ps.hand},
            "can_draw": len(self.deck) > 0 or (self.cfg.deck.reshuffle_discards and bool(self.deck.discard_pile)),
            "discardable": [c.id for c in ps.hand if c.category is not Category.MESSAGE],
            "active_deals_you_hold_WARNING_discarding_betrays": sorted(active_deals),
            "writable": writable,
            "playable": playable,
            "passable": [c.id for c in ps.hand if c.id not in active_deals],
            "recipients": others,
            "others": others,
            "living": self.alive(),
            "current_nominees": list(self.nominees),
            "train_slots": train_slots,
            "throwable_slots": [s for s in self._reserved_slots(player) if s not in thrown],
            "exposable": exposable,
        }

    def _validate_open(self, player: str, d: OpenTurnDecision, legal: dict[str, Any]) -> str | None:
        ps = self.players[player]
        kinds = legal["card_kind"]
        if d.expose and d.expose.commitment_id not in legal["exposable"]:
            return f"expose.commitment_id must be one of {legal['exposable']}"
        a = d.action
        if a == "skip":
            return None
        if a == "draw_discard":
            if not d.draw and not d.discard_card:
                return "draw_discard needs draw=true and/or discard_card"
            if d.draw and not legal["can_draw"]:
                return "the deck is empty; you cannot draw"
            if d.discard_card and d.discard_card not in legal["discardable"]:
                return f"discard_card must be one of {legal['discardable']}"
            return None
        if d.card is None:
            return f"action {a} requires `card`"
        if d.card not in kinds:
            return f"card {d.card!r} is not in your hand"
        kind = kinds[d.card]
        if a == "write":
            if d.card not in legal["writable"]:
                return f"card must be a message card with room to write: {legal['writable']}"
            words = (d.text or "").split()
            if not words:
                return "write requires non-empty `text`"
            have = len(ps.card(d.card).writing.split())  # type: ignore[union-attr]
            limit = self.cfg.rules.tea_max_words if kind == CardKind.TEA.value else self.cfg.rules.manifesto_max_words
            if limit is not None and len(words) > limit - have:
                return f"{kind} has room for only {limit - have} more word(s)"
            return None
        if a == "play":
            if d.card not in legal["playable"]:
                return f"card {d.card} cannot be played; playable: {legal['playable']}"
            allowed = legal["playable"][d.card]
            as_kind = d.play_as.value if d.play_as else (allowed[0] if len(allowed) == 1 else None)
            if as_kind is None:
                return f"Wild needs `play_as`, one of {allowed}"
            if as_kind not in allowed:
                return f"{d.card} may only be played as {allowed}"
            d.play_as = CardKind(as_kind)  # normalise so _play never has to guess
            if as_kind == CardKind.TRAIN.value and d.train_slot not in legal["train_slots"]:
                return f"train_slot must be one of {legal['train_slots']}"
            if as_kind == CardKind.DECLARE.value:
                return self._validate_declare(d.declare, legal["others"], None)
            if as_kind == CardKind.SHUFFLE.value and (d.new_order is None or sorted(d.new_order) != sorted(legal["living"])):
                return f"Shuffle needs new_order = a permutation of {legal['living']}"
            return None
        if a == "pass":
            if d.card not in legal["passable"]:
                return f"card cannot be passed (active deals must be kept); passable: {legal['passable']}"
            if d.to not in legal["recipients"]:
                return f"`to` must be one of {legal['recipients']}"
            if kind == CardKind.THROW.value and d.throw_slot not in legal["throwable_slots"]:
                return f"passing Throw requires throw_slot from {legal['throwable_slots']}"
            return None
        return f"unknown action {a}"

    @staticmethod
    def _validate_declare(dec: DeclareTerms | None, others: list[str], required_category: str | None) -> str | None:
        if dec is None:
            return "a Declare requires `declare` terms"
        if required_category and dec.category != required_category:
            return f"this Pledge requires a '{required_category}' Declare"
        if dec.category == "nominate":
            t = dec.targets or []
            if not (1 <= len(t) <= 2) or len(set(t)) != len(t) or any(x not in others for x in t):
                return f"nominate Declare needs 1-2 distinct targets from {others}"
        else:
            pair = dec.pair or []
            if len(pair) != 2 or len(set(pair)) != 2 or any(x not in others for x in pair):
                return f"vote Declare needs `pair` of two distinct players from {others}"
            if dec.target not in pair:
                return "vote Declare needs `target` from the pair"
        return None

    # ---------------------------------------------------------------- play

    def _play(self, player: str, d: OpenTurnDecision) -> None:
        ps = self.players[player]
        card = ps.card(d.card)  # type: ignore[arg-type]
        assert card is not None
        as_kind = d.play_as if card.kind is CardKind.WILD else card.kind
        assert as_kind is not None
        via = " (via Wild)" if as_kind is not card.kind else ""

        if card.category is Category.MESSAGE:
            text = f'"{card.writing}"' if card.writing else "a blank card"
            self._event("play_message", public=f"{player} plays a {card.kind.value}: it reads {text}.",
                        data={"player": player, "card": card.to_dict()})
        elif as_kind is CardKind.TRAIN:
            self.train_claims[d.train_slot] = player  # type: ignore[index]
            self._event("play", public=f"{player} plays Train{via}: claims {d.train_slot}'s spinner slot for the next Competition.",
                        data={"player": player, "card": card.to_dict(), "slot": d.train_slot})
        elif as_kind is CardKind.DECLARE:
            assert d.declare is not None
            self._declare(player, d.declare, f"{player} plays Declare{via}", card)
        elif as_kind is CardKind.MIST:
            self.mist_holder = player
            self._event("play", public=f"{player} plays Mist{via}: deals passed by {player} must now be accepted (until another Mist or the next eviction).",
                        data={"player": player, "card": card.to_dict()})
        elif as_kind is CardKind.SHUFFLE:
            assert d.new_order is not None
            self.order = list(d.new_order)
            self._event("play", public=f"{player} plays Shuffle{via}: new move order from next turn: {' > '.join(self.order)}.",
                        data={"player": player, "card": card.to_dict(), "order": list(self.order)})
        elif as_kind is CardKind.EXCHANGE:
            ps.remove(card.id)
            self.deck.discard(card)
            old = list(ps.hand)
            shown = "; ".join(c.public() for c in old) or "nothing"
            for c in old:
                if c.commitment_id and self.commitments[c.commitment_id].status == "active":
                    self._violate(self.commitments[c.commitment_id], "discarded via Exchange before fulfilment")
                ps.remove(c.id)
                self.deck.discard(c)
            new = []
            for _ in range(5):
                c = self.deck.draw()
                if c:
                    ps.hand.append(c)
                    new.append(c)
            self._event("play", public=f"{player} plays Exchange{via}: discards {len(old)} cards ({shown}) and draws {len(new)} new ones.",
                        private={player: "Your new hand: " + "; ".join(c.describe() for c in new)},
                        data={"player": player, "discarded": [c.to_dict() for c in old], "drawn": [c.to_dict() for c in new]})
            return
        else:  # pragma: no cover
            raise GameError(f"cannot play {card.kind}")
        ps.remove(card.id)
        self.deck.discard(card)

    def _declare(self, player: str, dec: DeclareTerms, how: str, card: Card) -> Commitment:
        if dec.category == "nominate":
            terms = {
                "category": "nominate",
                "targets": list(dec.targets or []),
                "earliest_round": self.round if (self.phase == PHASE_OPEN1 or self.watcher != player) else self.round + 1,
            }
            text = f"will nominate {' and '.join(terms['targets'])} the next time they choose nominees"
        else:
            terms = {"category": "vote", "pair": list(dec.pair or []), "target": dec.target}
            text = f"will vote to eliminate {dec.target} if the nominees are {terms['pair'][0]} and {terms['pair'][1]}"
        c = self._new_commitment("declare", holder=player, beneficiary=None, card_id=None, terms=terms, public=True)
        self._event("declare", public=f"{how} ({c.id}): {player} {text}. Binding — betrayal shows their whole hand.",
                    data={"player": player, "card": card.to_dict(), "commitment": c.to_dict()})
        for p in list(self.commitments.values()):
            if p.kind == "pledge" and p.holder == player and p.status == "active" and p.terms["category"] == dec.category:
                self._fulfil(p, "pledged Declare made")
        return c

    # ---------------------------------------------------------------- pass

    def _pass(self, player: str, d: OpenTurnDecision) -> None:
        ps = self.players[player]
        to = d.to
        assert to is not None
        with self.lock:
            card = ps.card(d.card)  # type: ignore[arg-type]
            assert card is not None
            card.params = {"slot": d.throw_slot} if card.kind is CardKind.THROW else {}
            card.commitment_id = None
            forced = card.category is Category.GIFT or (card.category is Category.DEAL and self.mist_holder == player)
            desc = card.describe()
            needs_answer = not forced or card.kind is CardKind.PLEDGE
            if not needs_answer:
                why = "a gift" if card.category is Category.GIFT else f"{player} has Mist active"
                self._event("offer", private={to: f"{player} passes you {desc}. It cannot be declined ({why})."},
                            data={"from": player, "to": to, "card": card.to_dict(), "forced": True})
            else:
                note = f" It cannot be declined ({player} has Mist active)." if forced else " Accept or decline?"
                if card.kind is CardKind.PLEDGE:
                    note += " You may also accept it and immediately play it as a Declare of its printed category."
                self._event("offer", private={to: f"{player} offers you {desc}.{note}"},
                            data={"from": player, "to": to, "card": card.to_dict(), "forced": forced})
                legal = {
                    "from": player, "card": self._card_view(card), "can_decline": not forced,
                    "pledge_category": card.printed.get("category") if card.kind is CardKind.PLEDGE else None,
                    "others": [p for p in self.alive() if p != to], "current_nominees": list(self.nominees),
                }
                self._event("waiting", public=f"{player} is passing a card to {to}…", data={"from": player, "to": to})

        resp: RespondPassDecision | None = None
        accept = True
        if needs_answer:
            resp = self._decide(to, "respond_pass", legal)  # type: ignore[assignment]  (no lock held while waiting)
            accept = resp.accept or forced

        with self.lock:
            if ps.card(card.id) is not card:
                self._event("skip", public=f"The card {player} was passing to {to} is no longer in their hand; the pass is void.")
                return
            ps.remove(card.id)
            if not accept:
                self.deck.discard(card)
                self._event("decline", public=f"{player} passes a card to {to} — DECLINED. The card is shown to everyone: {card.public()}. It is discarded.",
                            data={"from": player, "to": to, "card": card.to_dict()})
                return
            if card.kind is CardKind.PLEDGE and resp is not None and resp.declare is not None:
                # Accept-and-play: the only way a Pledge is ever played. The card never enters the hand.
                self._event("accept", public=f"{player} passes a card to {to} — accepted. {to} reveals it: {card.public()}, and plays it on the spot.",
                            private={player: f"{to} accepted your {desc} and is playing it as a Declare immediately."},
                            data={"from": player, "to": to, "card": card.to_dict(), "played_on_accept": True})
                self._declare(to, resp.declare, f"{to} plays the Pledge as a Declare (out of turn)", card)
                self.deck.discard(card)
                return
            self.players[to].hand.append(card)
            note = ""
            if card.category is Category.DEAL:
                c = self._create_deal(player, to, card)
                note = f" Deal {c.id} is now binding on {to}."
            elif card.kind is CardKind.THROW:
                self.throws.append({"slot": card.params["slot"], "gifter": player, "giftee": to})
            self._event("accept", public=f"{player} passes a card to {to} — accepted ({len(self.players[to].hand)} in {to}'s hand).",
                        private={to: f"You accepted {desc} from {player}.{note}", player: f"{to} accepted your {desc}.{note}"},
                        data={"from": player, "to": to, "card": card.to_dict()})

    def _create_deal(self, passer: str, holder: str, card: Card) -> Commitment:
        if card.kind is CardKind.SAFETY:
            n = int(card.printed["rounds"])
            terms = {"protected": passer, "nominations_remaining": n, "rounds": n}
            kind = "safety"
        else:
            terms = {"category": card.printed["category"], "due_round": self.round, "due_phase": self.phase}
            kind = "pledge"
        c = self._new_commitment(kind, holder=holder, beneficiary=passer, card_id=card.id, terms=terms)
        card.commitment_id = c.id
        return c

    # ------------------------------------------------------------ nomination

    def _nomination(self) -> None:
        with self.lock:
            self.phase = PHASE_NOMINATION
            self.turn = 1
            w = self.watcher
            assert w is not None
            candidates = [p for p in self.alive() if p != w]
            legal = {
                "candidates": candidates,
                "your_binding_declares": [c.describe() for c in self.commitments.values() if c.kind == "declare" and c.holder == w and c.status == "active" and c.terms["category"] == "nominate"],
                "safety_deals_you_hold": [c.describe() for c in self.commitments.values() if c.kind == "safety" and c.holder == w and c.status == "active"],
            }
            d: NominateDecision = self._decide(w, "nominate", legal)  # type: ignore[assignment]
            self.nominees = list(d.nominees)
            self._event("nominate", public=f"Watcher {w} nominates {self.nominees[0]} and {self.nominees[1]}.",
                        data={"watcher": w, "nominees": list(self.nominees)})
            self._after_nomination(chosen=True)

    def _after_nomination(self, chosen: bool) -> None:
        w = self.watcher
        for c in list(self.commitments.values()):
            if c.status != "active":
                continue
            if c.kind == "safety":
                if chosen and c.holder == w and c.terms["protected"] in self.nominees:
                    self._violate(c, f"{w} nominated {c.terms['protected']} while Safety was active")
                    continue
                c.terms["nominations_remaining"] -= 1
                if c.terms["nominations_remaining"] <= 0:
                    self._fulfil(c, "the protected period has passed")
            elif c.kind == "declare" and c.terms["category"] == "nominate" and c.holder == w:
                if c.terms["earliest_round"] > self.round:
                    continue
                if not chosen:
                    self._fulfil(c, "nominees were forced in the final three")
                    continue
                must = [t for t in c.terms["targets"] if self.players[t].alive]
                if all(t in self.nominees for t in must):
                    self._fulfil(c, "nominated as declared")
                else:
                    self._violate(c, f"{w} declared {', '.join(c.terms['targets'])} but nominated {', '.join(self.nominees)}")

    # ------------------------------------------------------------------ vote

    def _vote(self) -> None:
        with self.lock:
            self.phase = PHASE_VOTE
            self.turn = 1
            w = self.watcher
            assert w is not None
            voters = [p for p in self.alive() if p != w and p not in self.nominees]
            votes: dict[str, str] = {}
            legal_by = {v: {"nominees": list(self.nominees), "your_binding_declares": [c.describe() for c in self._vote_declares(v)]} for v in voters}
            if voters:
                self._event("waiting", public=f"{len(voters)} players are voting…", data={"voters": voters})
            decisions = self._decide_many("vote", legal_by)
            for v in voters:
                d: VoteDecision = decisions[v]  # type: ignore[assignment]
                votes[v] = d.vote
                self._event("vote_cast", private={v: f"You voted to eliminate {d.vote}."}, data={"voter": v, "vote": d.vote})
                self._check_vote_declares(v, d.vote)
            tally = Counter(votes.values())
            counts = {n: tally.get(n, 0) for n in self.nominees}
            rv = self.cfg.rules.reveal_votes
            if rv == "full":
                detail = "; ".join(f"{v} voted {t}" for v, t in votes.items()) or "no voters"
            elif rv == "count":
                detail = ", ".join(f"{n}: {k}" for n, k in counts.items())
            else:
                detail = "votes are secret"
            a, b = self.nominees
            if counts[a] == counts[b]:
                self._event("tally", public=f"The vote is tied ({detail}). Watcher {w} must break the tie.", data={"votes": votes if rv == "full" else counts})
                legal = {"nominees": list(self.nominees), "your_binding_declares": [c.describe() for c in self._vote_declares(w)]}
                t: TiebreakDecision = self._decide(w, "tiebreak", legal)  # type: ignore[assignment]
                out = t.eliminate
                self._event("tiebreak", public=f"{w} breaks the tie: {out} is eliminated.", data={"watcher": w, "eliminate": out})
                self._check_vote_declares(w, out)
            else:
                out = a if counts[a] > counts[b] else b
                self._event("tally", public=f"Votes ({detail}). {out} is eliminated.", data={"votes": votes if rv == "full" else counts, "eliminated": out})
            self._eliminate(out)

    def _vote_declares(self, player: str) -> list[Commitment]:
        pair = set(self.nominees)
        return [c for c in self.commitments.values()
                if c.kind == "declare" and c.holder == player and c.status == "active" and c.terms["category"] == "vote" and set(c.terms["pair"]) == pair]

    def _check_vote_declares(self, player: str, voted: str) -> None:
        for c in self._vote_declares(player):
            if voted == c.terms["target"]:
                self._fulfil(c, "voted as declared")
            else:
                self._violate(c, f"{player} declared a vote against {c.terms['target']} but voted {voted}")

    # ------------------------------------------------------------- eliminate

    def _eliminate(self, player: str) -> None:
        with self.lock:
            ps = self.players[player]
            ps.alive = False
            ps.eliminated_round = self.round
            self.eliminated_order.append(player)
            ps.eliminated_order = len(self.eliminated_order)
            self.order = [p for p in self.order if p != player]
            if self.mist_holder:
                self._event("mist_end", public=f"The Mist around {self.mist_holder} fades with the eviction.")
                self.mist_holder = None
            for c in list(self.commitments.values()):
                if c.status != "active":
                    continue
                if c.holder == player:
                    c.status = "resolved"
                    c.violation = f"{player} eliminated"
                elif c.beneficiary == player:
                    self._fulfil(c, f"{player} was eliminated")
                elif c.kind == "declare" and (
                    (c.terms["category"] == "vote" and player in c.terms["pair"])
                    or (c.terms["category"] == "nominate" and all(not self.players[t].alive for t in c.terms["targets"]))
                ):
                    self._fulfil(c, f"{player} was eliminated; the declare can no longer apply")
            for c in ps.hand:
                c.commitment_id = None

            survivors = self.alive()
            self._event("eliminated", public=f"{player} has been ELIMINATED (round {self.round}, #{ps.eliminated_order} out) holding {len(ps.hand)} cards, {self._jury_count(player)} of them Jury Votes.")
            if ps.hand and survivors and self.cfg.rules.eliminated_distribution:
                legal = {"hand": [self._card_view(c) for c in ps.hand], "hand_ids": [c.id for c in ps.hand], "recipients": survivors}
                d: DistributeDecision = self._decide(player, "distribute", legal)  # type: ignore[assignment]
                given: Counter[str] = Counter()
                for t in d.transfers:
                    card = ps.card(t.card)
                    if card is None or t.to not in survivors:
                        continue
                    ps.remove(card.id)
                    card.params, card.commitment_id = {}, None
                    self.players[t.to].hand.append(card)
                    given[t.to] += 1
                    self._event("transfer", private={t.to: f"{player} leaves you {card.describe()}."},
                                data={"from": player, "to": t.to, "card": card.to_dict()})
                summary = ", ".join(f"{n} to {p}" for p, n in given.items()) or "nothing"
                left = "; ".join(c.public() for c in ps.hand) or "none"
                self._event("distribute", public=f"{player} hands out cards: {summary}. Discarded: {left}.", data={"given": dict(given)})
            for c in list(ps.hand):
                ps.remove(c.id)
                self.deck.discard(c)

        # ---------------------------------------------------------------- finale

    def _finale(self) -> None:
        with self.lock:
            self.phase = PHASE_FINALE
            self.turn = 1
            finalists = self.alive()
            counts = {p: self._jury_count(p) for p in finalists}
            self._event("finale", public=f"FINAL TWO: {finalists[0]} and {finalists[1]}. Jury Votes in hand — " + ", ".join(f"{p}: {n}" for p, n in counts.items()),
                        data={"jury_votes": counts})
            best = max(counts.values())
            leaders = [p for p, n in counts.items() if n == best]
            if len(leaders) == 1:
                self.winner = leaders[0]
            else:
                votes: list[tuple[str, str]] = []
                self._event("waiting", public="The jury is voting…", data={"jurors": list(self.eliminated_order)})
                decisions = self._decide_many("jury_vote", {j: {"finalists": finalists} for j in self.eliminated_order})
                for j in self.eliminated_order:
                    d: JuryVoteDecision = decisions[j]  # type: ignore[assignment]
                    votes.append((j, d.vote))
                    self._event("jury", public=f"Juror {j} votes for {d.vote} to win.", data={"juror": j, "vote": d.vote})
                tally = Counter(v for _, v in votes)
                if tally[finalists[0]] == tally[finalists[1]]:
                    dropped, _ = votes.pop(0)
                    self._event("jury_drop", public=f"The jury is tied; the vote of {dropped} (eliminated first) is discarded.")
                    tally = Counter(v for _, v in votes)
                self.winner = finalists[0] if tally[finalists[0]] > tally[finalists[1]] else finalists[1]
            self._event("winner", public=f"🏆 {self.winner} WINS Watchful Eye.", data={"winner": self.winner})

        # ============================================================ commitments

    def _new_commitment(self, kind: str, *, holder: str, beneficiary: str | None, card_id: str | None, terms: dict[str, Any], public: bool = False) -> Commitment:
        c = Commitment(id=f"D{self._next_commitment}", kind=kind, holder=holder, beneficiary=beneficiary, card_id=card_id,  # type: ignore[arg-type]
                       terms=terms, created_round=self.round, created_phase=self.phase, public=public)
        self._next_commitment += 1
        self.commitments[c.id] = c
        return c

    def _fulfil(self, c: Commitment, reason: str) -> None:
        c.status = "fulfilled"
        c.violation = reason
        self._free_card(c)
        if c.kind == "declare":
            self._event("fulfilled", public=f"Declare {c.id} by {c.holder} is fulfilled ({reason}).", data={"commitment": c.to_dict()})
        else:
            private = {c.holder: f"Deal {c.id} is fulfilled ({reason}); the card may now be discarded or re-passed."}
            if c.beneficiary:
                private[c.beneficiary] = f"Your deal {c.id} with {c.holder} is fulfilled ({reason})."
            self._event("fulfilled", private=private, data={"commitment": c.to_dict()})

    def _violate(self, c: Commitment, reason: str) -> None:
        c.status = "violated"
        c.violation = reason
        self._event("violated", data={"commitment": c.to_dict()})  # host-only record; nobody is told
        if self.cfg.rules.deal_enforcement == "auto":
            self._event("betrayal", public=f"BETRAYAL: {c.holder} broke {c.kind} {c.id} — {reason}. The host applies the penalty.")
            self._apply_penalty(c, c.holder)
            c.status = "resolved"
            self._free_card(c)

    def _expose(self, accuser: str, cid: str) -> None:
        c = self.commitments[cid]
        if c.status == "violated":
            self._event("expose", public=f"{accuser} EXPOSES {c.holder} over {c.kind} {c.id}. The host confirms: {c.violation}. {c.holder} takes the penalty.",
                        data={"accuser": accuser, "commitment": c.to_dict(), "upheld": True})
            self._apply_penalty(c, c.holder)
            c.status = "resolved"
            self._free_card(c)
        else:
            self._event("expose", public=f"{accuser} EXPOSES {c.holder} over {c.kind} {c.id}. The host finds NO violation — {accuser} takes the penalty instead.",
                        data={"accuser": accuser, "commitment": c.to_dict(), "upheld": False})
            self._apply_penalty(c, accuser)

    def _apply_penalty(self, c: Commitment, target: str) -> None:
        ps = self.players[target]
        if c.kind == "safety":
            n = int(c.terms["rounds"])
            ps.train_banned_through_round = max(ps.train_banned_through_round, self.round + n - 1)
            self._event("penalty", public=f"PENALTY: {target} may not play Train through round {ps.train_banned_through_round}.",
                        data={"target": target, "kind": "train_ban", "through": ps.train_banned_through_round})
        elif c.kind == "pledge":
            msgs = [m for m in ps.hand if m.category is Category.MESSAGE]
            if not msgs:
                self._event("penalty", public=f"PENALTY: all of {target}'s message cards are played — but {target} holds none.",
                            data={"target": target, "kind": "play_messages", "cards": []})
            for m in msgs:
                ps.remove(m.id)
                text = f'"{m.writing}"' if m.writing else "a blank card"
                self._event("penalty", public=f"PENALTY: {target}'s {m.kind.value} is force-played: it reads {text}.",
                            data={"target": target, "kind": "play_messages", "card": m.to_dict()})
                self.deck.discard(m)
        else:
            shown = "; ".join(x.public() for x in ps.hand) or "an empty hand"
            self._event("penalty", public=f"PENALTY: {target}'s entire hand is shown — {shown}.",
                        data={"target": target, "kind": "show_hand", "hand": [x.to_dict() for x in ps.hand]})

    def _free_card(self, c: Commitment) -> None:
        if not c.card_id:
            return
        for ps in self.players.values():
            card = ps.card(c.card_id)
            if card:
                card.commitment_id = None

    # ============================================================== decisions

    @contextlib.contextmanager
    def _unlocked(self):
        """Release self.lock (however many times this thread holds it) for the duration."""
        if self.lock._is_owned():  # type: ignore[attr-defined]
            state = self.lock._release_save()  # type: ignore[attr-defined]
            try:
                yield
            finally:
                self.lock._acquire_restore(state)  # type: ignore[attr-defined]
        else:
            yield

    def _deadline(self, kind: str) -> float:
        mw = self.cfg.rules.move_wait_s
        if self.phase_deadline is not None:
            return self.phase_deadline if kind == "open_turn" else min(time.time() + mw, self.phase_deadline)
        return time.time() + mw

    def _decide_many(self, kind: str, legal_by_player: dict[str, dict[str, Any]]) -> dict[str, Decision]:
        """Ask several players at once (votes, jury). Returns when all have answered or timed out."""
        results: dict[str, Decision] = {}
        errors: dict[str, BaseException] = {}

        def run(p: str) -> None:
            try:
                results[p] = self._decide(p, kind, legal_by_player[p])
            except BaseException as e:  # noqa: BLE001
                errors[p] = e

        with self._unlocked():
            threads = [threading.Thread(target=run, args=(p,), daemon=True) for p in legal_by_player]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        if errors:
            raise next(iter(errors.values()))
        return results

    def _decide(self, player: str, kind: str, legal: dict[str, Any]) -> Decision:
        schema = SCHEMAS[kind]
        agent = self.agents[player]
        ps = self.players[player]
        is_human = bool(getattr(agent, "is_human", False))
        is_llm = agent is not self.random_agent and not is_human
        errors: list[str] = []
        attempts = 1 if is_human else (self.cfg.llm.max_retries + 1 if is_llm else 20)
        prompt, record = self._user_prompt(player, kind, legal) if is_llm else ("", "")
        deadline = self._deadline(kind)
        self.pending[player] = {"kind": kind, "deadline": deadline}
        for attempt in range(attempts):
            req = DecisionRequest(kind=kind, player=player, system_prompt=self._system_prompts[player],
                                  user_prompt=prompt, history_text=record, schema_model=schema, legal=legal,
                                  errors=list(errors), deadline=deadline,
                                  validator=lambda data, _k=kind, _l=legal: self._validate_locked(player, _k, data, _l))
            with self._unlocked():
                resp = agent.decide(req)
            self.pending.pop(player, None)
            if is_llm:
                ps.llm_calls += 1
                ps.llm_cost += resp.cost
                ps.llm_prompt_tokens += resp.prompt_tokens
                ps.llm_cached_tokens += resp.cached_tokens
                self.total_cost += resp.cost
            if resp.data is not None:
                d, err = self._validate(player, kind, resp.data, legal)
            else:
                d, err = None, resp.error or "no response"
            self._log_llm(player, kind, attempt, req, resp, err)
            if d is not None:
                if is_llm:
                    agent.commit(record, keep_reply=True)
                    if self.cfg.output.print_llm_thinking and d.thinking:
                        self._print(f"    [{player} thinks] {d.thinking}")
                return d
            errors = [err or "invalid"]
            if is_llm:
                self._print(f"    [{player} attempt {attempt + 1} rejected: {err}]")
                prompt = "" if resp.transport_error else (
                    "# YOUR PREVIOUS ANSWER WAS REJECTED\n" + err + "\nAnswer the same decision again with a valid JSON object.")  # type: ignore[operator]
        if not is_llm or not self.cfg.llm.fallback_to_random:
            raise GameError(f"{player} could not produce a legal {kind} decision: {errors}")
        ps.llm_fallbacks += 1
        for _ in range(50):
            resp = self.random_agent.decide(DecisionRequest(kind, player, "", "", "", schema, legal))
            d, err = self._validate(player, kind, resp.data, legal)
            if d is not None:
                self._event("fallback", public=f"(host) {player} failed to make a legal {kind} decision; a random legal move is made for them.",
                            data={"player": player, "kind": kind, "errors": errors, "decision": resp.data})
                note = (f"\n[HOST NOTE] Your answers to this {kind} decision were invalid ({errors[-1]}); "
                        f"a random legal move was made for you: {json.dumps(resp.data)}")
                agent.commit(record + note, keep_reply=False)
                return d
        raise GameError(f"random fallback could not produce a legal {kind} for {player}: {err}")

    def _validate_locked(self, player: str, kind: str, data: dict[str, Any], legal: dict[str, Any]) -> str | None:
        with self.lock:
            return self._validate(player, kind, data, legal)[1]

    def _validate(self, player: str, kind: str, data: dict[str, Any] | None, legal: dict[str, Any]) -> tuple[Decision | None, str | None]:
        schema = SCHEMAS[kind]
        try:
            d = schema.model_validate(data)
        except ValidationError as e:
            return None, "schema error: " + "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        err: str | None = None
        if kind == "open_turn":
            err = self._validate_open(player, d, legal)  # type: ignore[arg-type]
        elif kind == "respond_pass":
            if d.declare is not None:  # type: ignore[attr-defined]
                if not legal.get("pledge_category"):
                    err = "`declare` is only allowed when accepting a Pledge"
                elif not d.accept:  # type: ignore[attr-defined]
                    err = "to play the Pledge as a Declare you must also accept it"
                else:
                    err = self._validate_declare(d.declare, legal["others"], legal["pledge_category"])  # type: ignore[attr-defined]
        elif kind == "nominate":
            n = d.nominees  # type: ignore[attr-defined]
            if len(n) != 2 or len(set(n)) != 2 or any(x not in legal["candidates"] for x in n):
                err = f"nominees must be two distinct names from {legal['candidates']}"
        elif kind in ("vote", "tiebreak"):
            v = d.vote if kind == "vote" else d.eliminate  # type: ignore[attr-defined]
            if v not in legal["nominees"]:
                err = f"must be one of {legal['nominees']}"
        elif kind == "distribute":
            seen = set()
            for t in d.transfers:  # type: ignore[attr-defined]
                if t.card not in legal["hand_ids"] or t.to not in legal["recipients"] or t.card in seen:
                    err = f"each transfer needs a unique card from {legal['hand_ids']} and a recipient from {legal['recipients']}"
                    break
                seen.add(t.card)
        elif kind == "jury_vote":
            if d.vote not in legal["finalists"]:  # type: ignore[attr-defined]
                err = f"vote must be one of {legal['finalists']}"
        return (None, err) if err else (d, None)

    # ================================================================== views

    def _card_view(self, c: Card) -> dict[str, Any]:
        v = c.to_dict()
        if c.commitment_id:
            v["deal"] = self.commitments[c.commitment_id].describe()
        return v

    # Keys of the legal-options dict that duplicate the position block; kept for validation, not sent.
    _LEGAL_HIDDEN = {"hand", "card_kind"}

    def _user_prompt(self, player: str, kind: str, legal: dict[str, Any]) -> tuple[str, str]:
        """Returns (host message for this decision, events-only record to keep in history afterwards)."""
        ps = self.players[player]
        new_events = self._visible_events(player, since=ps.last_seen_seq)
        if ps.last_seen_seq == 0:
            new_events = new_events[-self.cfg.rules.history_events :]
        ps.last_seen_seq = self.events[-1].seq if self.events else 0
        record = (f"# ROUND {self.round} · {self.phase.upper()} · TURN {self.turn} — your {kind} decision\n"
                  "Since your previous decision:\n" + ("\n".join(new_events) or "(nothing new)"))
        shown = {k: v for k, v in legal.items() if k not in self._LEGAL_HIDDEN}
        parts = [record,
                 self._state_block(player),
                 "# DECISION\n" + DECISION_INSTRUCTIONS[kind],
                 "# LEGAL OPTIONS\n" + json.dumps(shown, separators=(",", ":"), default=str)]
        return "\n\n".join(parts), record

    def _visible_events(self, player: str, since: int) -> list[str]:
        out = []
        for e in self.events:
            if e.seq <= since:
                continue
            txt = []
            if e.public:
                txt.append(e.public)
            if player in e.private:
                txt.append(f"[private] {e.private[player]}")
            if txt:
                out.append(f"[R{e.round} {e.phase} T{e.turn}] " + " ".join(txt))
        return out

    def _state_block(self, player: str) -> str:
        ps = self.players[player]
        r = self.cfg.rules
        lines = [f"# YOUR POSITION — round {self.round}, {self.phase} phase, turn {self.turn}", f"You are {player}."]
        lines.append("Living players in move order: " + ", ".join(f"{p} ({len(self.players[p].hand)} cards)" for p in self.order))
        if self.eliminated_order:
            lines.append("Eliminated (in order): " + ", ".join(self.eliminated_order))
        lines.append(f"Watcher: {self.watcher or 'none yet'}; last round's Watcher: {self.last_watcher or 'none'}")
        if self.nominees:
            lines.append("Nominees: " + " and ".join(self.nominees))
        slot_desc = []
        for s in self.slots:
            if s in self.train_claims:
                slot_desc.append(f"{s}: claimed by {self.train_claims[s]} (Train)")
            elif self.players[s].alive:
                slot_desc.append(f"{s}: {s}")
            else:
                slot_desc.append(f"{s}: empty (claimable)")
        lines.append("Spinner slots for the next Competition: " + "; ".join(slot_desc))
        lines.append(f"Mist: {self.mist_holder or 'none'}")
        if ps.train_banned_through_round >= self.round:
            lines.append(f"You may not play Train through round {ps.train_banned_through_round}.")
        decl = [c.describe() for c in self.commitments.values() if c.kind == "declare" and c.status in ("active", "violated")]
        if decl:
            lines.append("Public Declares outstanding (the host does not say whether they have been broken):\n  " + "\n  ".join(decl))
        lines.append(f"Your hand ({len(ps.hand)} cards, {self._jury_count(player)} Jury Votes):")
        lines += [f"  {c.describe()}" for c in ps.hand] or ["  (empty)"]
        held = [c for c in self.commitments.values() if c.kind != "declare" and c.holder == player and c.status in ("active", "fulfilled")]
        if held:
            lines.append("Deal cards you hold:\n  " + "\n  ".join(c.describe_status() for c in held))
        given = [c for c in self.commitments.values() if c.kind != "declare" and c.beneficiary == player and c.status in ("active", "violated", "fulfilled")]
        if given:
            lines.append("Deals you have given:")
            for c in given:
                lines.append("  " + (c.describe_status() if (r.deal_enforcement == "auto" or c.status == "fulfilled") else c.describe() + " — status: unknown to you (EXPOSE if you believe it was broken)"))
        th = [t for t in self.throws if player in (t["gifter"], t["giftee"])]
        if th:
            lines.append("Throws you know about: " + "; ".join(f"{t['gifter']}'s slot {t['slot']} → {t['giftee']}" for t in th))
        return "\n".join(lines)

    # ====================================================== any-time actions (web play)

    def write(self, player: str, card_id: str, text: str) -> str | None:
        """Add words to a message card in hand. Allowed at any time; returns an error or None."""
        with self.lock:
            ps = self.players[player]
            card = ps.card(card_id)
            if card is None or not ps.alive:
                return "that card is not in your hand"
            if card.category is not Category.MESSAGE:
                return "only Tea and Manifesto cards can be written on"
            words = text.split()
            if not words:
                return "nothing to write"
            limit = self.cfg.rules.tea_max_words if card.kind is CardKind.TEA else self.cfg.rules.manifesto_max_words
            have = len(card.writing.split())
            if limit is not None and len(words) > limit - have:
                return f"this {card.kind.value} has room for {max(0, limit - have)} more word(s)"
            card.writing = (card.writing + " " + " ".join(words)).strip()
            self._event("write", public=f"{player} writes on a card.",
                        private={player: f"{card.id} now reads: \"{card.writing}\""},
                        data={"player": player, "card": card.to_dict()})
            return None

    def exposable(self, player: str) -> list[Commitment]:
        with self.lock:
            return [
                c for c in self.commitments.values()
                if c.status in ("active", "violated") and self.players[c.holder].alive
                and ((c.kind in ("safety", "pledge") and c.beneficiary == player) or (c.kind == "declare" and c.holder != player))
            ]

    def expose_now(self, player: str, commitment_id: str) -> str | None:
        """EXPOSE at any time (the rules allow it whenever). Returns an error or None."""
        with self.lock:
            if not self.players[player].alive:
                return "eliminated players cannot expose"
            if commitment_id not in {c.id for c in self.exposable(player)}:
                return "you cannot expose that"
            self._expose(player, commitment_id)
            return None

    def snapshot(self, player: str | None) -> dict[str, Any]:
        """Everything a client needs to draw the table, plus the private view for `player`."""
        with self.lock:
            slots = []
            for sname in self.slots:
                if sname in self.train_claims:
                    slots.append({"slot": sname, "status": "claimed", "by": self.train_claims[sname]})
                elif self.players[sname].alive:
                    slots.append({"slot": sname, "status": "own", "by": sname})
                else:
                    slots.append({"slot": sname, "status": "empty", "by": None})
            public = {
                "round": self.round, "phase": self.phase, "turn": self.turn,
                "open_turns": self.cfg.rules.open_turns, "turn_scope": self.cfg.rules.turn_scope,
                "phase_deadline": self.phase_deadline, "chaos_actions_left": dict(self.chaos_actions_left),
                "order": list(self.order), "active_player": self.active_player,
                "pending": {p: dict(v) for p, v in self.pending.items()},
                "watcher": self.watcher, "last_watcher": self.last_watcher, "nominees": list(self.nominees),
                "mist": self.mist_holder, "slots": slots, "eliminated": list(self.eliminated_order),
                "players": [
                    {"name": n, "alive": ps.alive, "hand": len(ps.hand), "watcher": n == self.watcher,
                     "nominee": n in self.nominees, "eliminated_order": ps.eliminated_order}
                    for n, ps in self.players.items()
                ],
                "declares": [{"id": c.id, "holder": c.holder, "text": c.describe()} for c in self.commitments.values()
                             if c.kind == "declare" and c.status in ("active", "violated")],
                "deck": len(self.deck), "winner": self.winner,
            }
            you: dict[str, Any] | None = None
            if player and player in self.players:
                ps = self.players[player]
                you = {
                    "name": player, "alive": ps.alive, "jury_votes": self._jury_count(player),
                    "hand": [self._card_view(c) for c in ps.hand],
                    "train_banned_through": ps.train_banned_through_round,
                    "deals_held": [{"id": c.id, "text": c.describe(), "status": c.status, "card": c.card_id}
                                   for c in self.commitments.values() if c.kind != "declare" and c.holder == player and c.status in ("active", "fulfilled")],
                    "deals_given": [{"id": c.id, "text": c.describe(), "status": c.status if c.status == "fulfilled" else "unknown", "holder": c.holder}
                                    for c in self.commitments.values() if c.kind != "declare" and c.beneficiary == player and c.status in ("active", "violated", "fulfilled")],
                    "exposable": [{"id": c.id, "text": c.describe()} for c in self.exposable(player)] if ps.alive else [],
                    "throws": [t for t in self.throws if player in (t["gifter"], t["giftee"])],
                    "writable": [c.id for c in ps.hand if c.category is Category.MESSAGE and (
                        (c.kind is CardKind.TEA and len(c.writing.split()) < self.cfg.rules.tea_max_words)
                        or (c.kind is CardKind.MANIFESTO and (self.cfg.rules.manifesto_max_words is None or len(c.writing.split()) < self.cfg.rules.manifesto_max_words)))],
                }
            return {"game": public, "you": you}

    # ================================================================ helpers

    def alive(self) -> list[str]:
        return [p for p in self.order if self.players[p].alive]

    def _jury_count(self, player: str) -> int:
        return sum(1 for c in self.players[player].hand if c.kind is CardKind.JURY_VOTE)

    def _discard_from_hand(self, player: str, card_id: str) -> None:
        ps = self.players[player]
        card = ps.remove(card_id)
        desc = card.describe()
        if card.commitment_id and self.commitments[card.commitment_id].status == "active":
            self._violate(self.commitments[card.commitment_id], f"{player} discarded the deal card before fulfilment")
        self.deck.discard(card)
        self._event("discard", public=f"{player} discards {card.public()} ({len(ps.hand)} in hand).",
                    private={player: f"You discarded {desc}."}, data={"player": player, "card": card.to_dict()})

    def _event(self, type_: str, public: str | None = None, private: dict[str, str] | None = None, data: dict[str, Any] | None = None) -> Event:
        with self.lock:
            e = Event(seq=len(self.events) + 1, round=self.round, phase=self.phase, turn=self.turn, type=type_, public=public, private=private or {}, data=data or {})
            self.events.append(e)
            if self._events_fh:
                self._events_fh.write(json.dumps(e.to_dict(), default=str) + "\n")
            if public:
                self._print(f"[R{e.round} {e.phase} T{e.turn}] {public}")
            for p, t in e.private.items():
                self._print(f"[R{e.round} {e.phase} T{e.turn}]   (private → {p}) {t}", transcript_only=True)
            if self.on_event is not None:
                try:
                    self.on_event(e)
                except Exception:  # noqa: BLE001 — a broken listener must not stop the game
                    pass
            return e

    def _print(self, line: str, transcript_only: bool = False) -> None:
        if self._transcript_fh:
            self._transcript_fh.write(line + "\n")
            self._transcript_fh.flush()
        if self.cfg.output.verbose and not transcript_only:
            print(line, flush=True)

    def _log_llm(self, player: str, kind: str, attempt: int, req: DecisionRequest, resp: Any, err: str | None) -> None:
        if not self._llm_fh or self.agents[player] is self.random_agent or getattr(self.agents[player], "is_human", False):
            return
        rec: dict[str, Any] = {
            "round": self.round, "phase": self.phase, "turn": self.turn, "player": player, "kind": kind,
            "attempt": attempt, "raw": resp.raw, "parsed": resp.data, "error": err or resp.error,
            "cost": resp.cost, "tokens": resp.tokens, "prompt_tokens": resp.prompt_tokens,
            "cached_tokens": resp.cached_tokens, "cache_write_tokens": resp.cache_write_tokens,
        }
        if self.cfg.llm.log_prompts:
            rec["system_prompt"], rec["user_prompt"] = req.system_prompt, req.user_prompt
        self._llm_fh.write(json.dumps(rec, default=str) + "\n")
        self._llm_fh.flush()
