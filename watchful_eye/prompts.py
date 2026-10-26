"""Rules sheet and per-decision instructions handed to LLM players."""

from __future__ import annotations

import json

from .cards import CARD_TEXT, CATEGORY, CardKind
from .config import GameConfig
from .decisions import SCHEMAS, json_schema


def rules_sheet(cfg: GameConfig, web: bool = False) -> str:
    r = cfg.rules
    turn_note = (
        f"{r.open_turns} turns; every living player acts once per turn, in move order"
        if r.turn_scope == "each_player"
        else f"{r.open_turns} individual actions total, taken in move order"
    )
    enforcement = (
        "Betrayals only bite when EXPOSEd. Only the passer may expose a deal; anyone may expose a Declare. "
        "The host then checks: if the term really was violated (or the deal card discarded unfulfilled) the "
        "betrayer takes the penalty; if it was NOT violated, the accuser takes that same penalty instead. "
        "The host never announces violations on its own — you must notice."
        if r.deal_enforcement == "expose"
        else "The host applies penalties automatically the moment a deal or Declare is violated."
    )
    cards = "\n".join(f"- {k.value} ({CATEGORY[k].value}): {CARD_TEXT[k]}" for k in CardKind)
    talk_line = (
        "" if web and cfg.rules.public_chat
        else "- There is no table talk in this game. Information travels only through cards and public actions.\n"
    )
    if web and r.turn_scope == "chaos":
        talk_line += (f"- Chaos: Open phases have no move order. Everyone has {r.open_turns} actions and "
                      f"{r.chaos_minutes:g} minutes to use them, all at once.\n")
    return f"""# WATCHFUL EYE — rules sheet

A social-elimination card game (think Big Brother). Survive to the final two with the most Jury Votes.

## Setup
- Each player starts with {cfg.deck.starting_hand} random cards plus {cfg.deck.starting_jury_votes} Jury Vote card(s).
- No hand limit. Hands are hidden, but everyone can see how many cards each player holds. All cards look identical from the back.
- Move order is random at the start.

## Round structure
1. COMPETITION (1 turn): the spinner has one slot per original player. Whoever's slot it lands on becomes the WATCHER for the round and cannot be eliminated. Slots of eliminated players are empty (re-spin) unless someone has claimed them with Train. No one may be Watcher two rounds running while more than {r.repeat_watcher_allowed_at_players} players remain (their slot counts as empty that round, so Throws on it are dead too).
2. OPEN ({turn_note}). On your turn you do ONE of:
   - DRAW/DISCARD: draw up to 1 card and/or discard up to 1 card. Discards are shown to everyone.
   - WRITE: add words to a message card (Tea: {r.tea_max_words} words total; Manifesto: unlimited). Words cannot be erased while the card is in play; a discarded message card is wiped.
   - PLAY an action card (revealed to everyone) or a message card (read aloud, then discarded).
   - PASS any card to another player. The recipient sees the card and may ACCEPT or DECLINE. A declined card is shown to everyone, then discarded. Gifts cannot be declined. Deals passed under Mist cannot be declined.
   You may also EXPOSE at any time, and write on message cards you hold at any time.
3. NOMINATION (1 turn): the Watcher names 2 Nominees.
4. OPEN (as above).
5. VOTE (1 turn): everyone except the Watcher and the Nominees privately votes for a nominee to ELIMINATE. Most votes = eliminated. Ties are broken publicly by the Watcher. The eliminated player may hand any of their cards to survivors (no accept/decline); the rest are discarded.

With 3 players left, phases 2 and 3 are skipped: both non-Watchers are automatically the nominees, there are no voters, and the Watcher's tie-break decides who goes.

## Winning
When 2 players remain, whoever holds the most Jury Vote cards wins. Tie: eliminated players each vote for a finalist; if that is also tied, the first-eliminated player's vote is discarded.

## Deals, Declares and EXPOSE
- A deal card has binding terms printed on it. An ACCEPTED deal must stay in your hand until its terms are fulfilled; discarding it (or Exchanging your hand) before then is a betrayal. Fulfilled deals may be discarded or re-passed.
- {enforcement}
- Gifts can never be exposed.

## Cards
{cards}

## Host rulings
- Safety covers the next N Nomination phases after acceptance (forced nominations in the 3-player round never count as betrayal). Train bans count the current round as round 1.
- A Pledge is due by the end of the phase in which it was accepted. Accepting it on your last turn of a phase means your only way to honour it is to play it as a Declare at the moment of acceptance.
- A 'nominate' Declare binds you to nominate every declared player who is still alive the next time you choose nominees (if you are Watcher now and have not nominated yet, that means this round). A 'vote' Declare binds your vote (or tie-break) whenever exactly that pair are the nominees.
- Shuffle takes effect from the next turn; the shuffler picks the new order. Throw requires a slot you currently reserve (your own, or one you have Trained). Mist is a single table-wide effect: a new Mist replaces the old one.
- Jury Votes may be passed as gifts at any time; they only count in the hands of a finalist.
{talk_line}"""


DECISION_INSTRUCTIONS: dict[str, str] = {
    "open_turn": (
        "It is your OPEN turn. Choose exactly one `action`:\n"
        "- `draw_discard` with `draw: true` and/or `discard_card: <id>`\n"
        "- `write` with `card: <message card id>` and `text`\n"
        "- `play` with `card: <id>` (+ `play_as` for Wild; `train_slot` for Train; `declare` for Declare; "
        "`new_order` for Shuffle)\n"
        "- `pass` with `card: <id>`, `to: <player>` (+ `throw_slot` for Throw)\n"
        "- `skip`\n"
        "Optionally add `expose` (free action). Use only ids/names listed under LEGAL OPTIONS."
    ),
    "respond_pass": (
        "A card is being passed to you. Return `accept: true/false`. If it is a Pledge you may also give "
        "`declare` to accept it and immediately play it as a Declare of its printed category."
    ),
    "nominate": "You are the Watcher. Return `nominees`: exactly two names from the candidates.",
    "vote": "Vote privately. Return `vote`: the nominee you want ELIMINATED.",
    "tiebreak": "The vote is tied. As Watcher you must publicly choose who is eliminated. Return `eliminate`.",
    "distribute": (
        "You have been eliminated. Return `transfers`: a list of {card, to} for cards you want to give to "
        "survivors (Jury Votes are what matters). Anything not listed is discarded."
    ),
    "jury_vote": "You are on the jury. The finalists are tied on Jury Votes. Return `vote`: who should WIN.",
}


def system_prompt(cfg: GameConfig, player: str, persona: str | None) -> str:
    schemas = "\n".join(
        f"## {kind}\n{json.dumps(json_schema(model, strict=False), separators=(',', ':'))}"
        for kind, model in SCHEMAS.items()
    )
    parts = [
        rules_sheet(cfg),
        f"\n# YOU\nYou are playing as **{player}**. Play to win. The host enforces the rules and the binding "
        "terms of deals and declares exactly as written.",
    ]
    if persona:
        parts.append(f"\nYour play style / persona: {persona}")
    parts.append(
        "\n# HOW THIS CONVERSATION WORKS\nEach of my messages reports everything that has happened since your "
        "last decision, your current hand and position, then asks for ONE decision and lists your LEGAL "
        "OPTIONS. Respond with a single JSON object matching the schema for that decision kind. No prose "
        "outside the JSON.\n\nYour earlier replies stay in this conversation and are your only memory of "
        "WHY you did things, so use `thinking` for a few sentences of real reasoning each turn: who you "
        "trust, what you promised, what you intend. Card ids in old replies may refer to cards you no longer "
        "hold; your current hand is always listed in the latest message.\n\n# DECISION SCHEMAS\n" + schemas
    )
    return "\n".join(parts)
