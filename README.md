# Watchful Eye — LLM beta-test simulator

A rules engine for *Watchful Eye* that plays the host, plus LLM players driven through the official
OpenRouter Python SDK. Everything — players, models, deck composition, rulings — is a YAML file
validated by pydantic.

```
pip install openrouter pydantic pyyaml
export OPENROUTER_API_KEY=sk-or-...

python -m watchful_eye validate configs/example.yaml   # check the config, print the resolved deck
python -m watchful_eye run configs/example.yaml --dry-run   # random bots, no API calls, exercises the engine
python -m watchful_eye run configs/example.yaml            # the real thing
python -m watchful_eye run configs/example.yaml --games 10 --seed 100 --quiet   # a batch; prints win counts
```

Each game writes a folder under `runs/`:

| file | contents |
|---|---|
| `transcript.txt` | human-readable log, including private lines marked `(private → player)` |
| `events.jsonl` | every event with structured `data` (cards, votes, commitments) — the thing to analyse |
| `llm_calls.jsonl` | every model reply: raw text, parsed JSON, validation error, cost, tokens |
| `result.json` | winner, elimination order, final Jury Vote counts, every deal/declare and how it ended, per-player cost |
| `config.yaml` | the fully-resolved config that produced the run |

## Config

See `configs/example.yaml` — every knob is commented there. The important ones:

**Players.** `agent: llm | random`. `model` is an OpenRouter id string, or a block with `temperature`,
`max_tokens`, `reasoning: {effort: ...}`, `provider: {...}` and `extra: {...}` (passed straight to
`chat.send`). Omit it to use `llm.default_model`. `persona` is free text appended to that player's
system prompt.

**Deck.** `deck.cards` maps card name → count. Safety and Pledge have printed terms, so they take a
map of variant → count (`Safety: {1: 2, 2: 2, 3: 2}`, `Pledge: {nominate: 3, vote: 2}`). Set
`deck.size` and the numbers become relative weights (rarity) scaled to that many cards.

**Rules.** The rulings below are the defaults; the ones that were ambiguous are knobs under `rules:`.

## How LLM players work

Each player has its own conversation with the host. The system prompt holds the rules sheet, the
host rulings, the persona and all decision schemas. For the current decision the host sends: *what
happened since your last decision* (public events plus anything private to you), your current position
and hand, the decision wanted, and the exact legal options. The model replies with one JSON object whose
`thinking` field is a few sentences of reasoning.

Once the decision is settled, that host message is trimmed in the history to just its events part; the
model's reply is kept verbatim. So the conversation a player carries is `[events before decision 1,
reply 1, events before decision 2, reply 2, ...]` plus the current full request — the model remembers
everything that happened and everything it thought, without re-reading stale hands and option lists. A
15-player finalist ends the game near 50k tokens of context. The prefix is append-only except for that
one trailing trim, so prompt caching covers nearly all of it; `llm.cache_control` sends the top-level
`cache_control` that Anthropic models need, and `result.json` reports each player's measured
`cache_hit_rate`.

The host validates every reply against the schema and against the legal options. An invalid reply is
sent back with the reason (`llm.max_retries` times); after that a random legal move is made, logged as a
`fallback` event, and the model is told in its next message. If a provider rejects the
`json_schema` response format the agent drops it and parses JSON out of the text instead.

There is no table talk and no memory limit by design: this harness tests mechanics and balance, not
social play. `llm.max_history_messages` caps the number of past decisions kept, if you ever need it.

If a provider's measured cache hit rate is poor, try `llm.response_format: none`: the JSON schema sent
per decision kind may be injected into the prompt by some providers, which breaks the cached prefix
whenever the decision kind changes. The schemas are in the system prompt anyway and replies are parsed
from text.


## Playing it in a browser

```
pip install fastapi uvicorn jinja2 websockets
uvicorn watchful_eye.web.app:app --port 8000
```

Open `http://localhost:8000/play`. Pick a name, then either join a queue (Silent, Chat, Speed,
Chaos — a game starts when ten people are waiting) or create a custom room and send the link to the
others. Custom rooms start when the creator presses Start (three players minimum). The game runs
at `/play/{room_id}`; reopening that link in the same browser puts you back in your seat.

Custom options: cast size, deck size (the composition in `web/rulesets.py` scaled to that many
cards), seconds per move, public chat on/off, and move order (random, seating, or none for Chaos
with a minutes-per-Open-phase clock).

How the browser game differs from the LLM harness: writing on a message card is free and allowed at
any time (others see that you are writing, not what); EXPOSE is a button that works at any time;
votes and jury votes are collected from everyone at once; a player who doesn't answer in time skips
(open turn), declines (pass offers), discards everything (hand-off), or gets a random legal choice
(nominate, vote, tiebreak, jury). Eliminated players stay as spectators with their own chat, which
exists in every ruleset. Public feed lines never show card ids, so a discarded "Safety (2 rounds)"
could be any Safety (2 rounds).

Everything is in memory: rooms vanish when the server stops. Each game still writes
`transcript.txt`, `events.jsonl` and `result.json` under `runs/web/`.

Fonts: Optician Sans (SIL OFL, bundled in `web/static/fonts`) and Atkinson Hyperlegible (Google
Fonts). Layout and styling are plain CSS and JS modules in `web/static`; no build step.

## Rulings the simulator makes

Where the rules sheet was silent, the host does this:

- **Open phase turns** — every living player acts once per turn (`rules.turn_scope: total` for N actions per phase instead).
- **Draw/discard** — one draw and/or one discard in the same action. Discards are shown to everyone.
- **Passing** — the recipient sees the card before accepting; the table sees that a pass happened and whether it was accepted; a declined card is shown and discarded.
- **Message cards** — writing is permanent while in play; a discarded message card is wiped.
- **Safety** — covers the next N Nomination phases after acceptance. Forced nominations in the 3-player round never count as betrayal. The Train ban counts the current round as round 1.
- **Pledge** — due by the end of the phase it was accepted in. The recipient may, at the moment of accepting (even under Mist), play the Pledge as a Declare of its printed category. That is the only way a Pledge is ever played; one from the deck is only for passing.
- **Declare** — "nominate" takes 1–2 names and binds you to nominate every declared player still alive the next time you choose nominees (this round if you are Watcher and have not nominated yet). "vote" names a pair and a target, and binds your vote or tie-break whenever exactly that pair are the nominees.
- **EXPOSE** — offered as a free action on your own Open turn. Deals may be exposed only by the passer; Declares by anyone. The host never announces a violation on its own (`rules.deal_enforcement: auto` makes it do so, for debugging).
- **Active deal cards** cannot be passed or discarded without betraying; fulfilled ones can be re-passed.
- **Mist** — one table-wide effect; a new Mist replaces the old; it ends at the next eviction.
- **Shuffle** — the shuffler chooses the new order; it takes effect from the next turn.
- **Train/Throw** — Train claims an empty (eliminated, unclaimed) slot for one Competition. Throw needs a slot you currently reserve; a Throw to a dead or barred player is a re-spin, as is a barred Watcher's own slot.
- **Repeat Watcher** — barred while more than 4 players remain (`rules.repeat_watcher_allowed_at_players`).
- **Vote reveal** — tallies are public, ballots are not (`rules.reveal_votes: full | none`).
- **Elimination** — the leaver hands cards to survivors with no accept/decline; cards arrive inert; deals they had given are void and free the holder.
- **Finale tie** — jurors vote in elimination order; if tied, the first-eliminated juror's vote is dropped (with two finalists that always settles it).

## Layout

```
watchful_eye/
  config.py       pydantic models for the YAML (players, deck, rules, llm, output)
  cards.py        card catalogue + rules text per card
  deck.py         deck construction, draw/discard, reshuffle
  state.py        PlayerState, Commitment (deal/declare), Event
  decisions.py    pydantic schemas the agents must return
  prompts.py      rules sheet + system prompt
  engine.py       the host
  agents/
    openrouter_agent.py   OpenRouter SDK player with per-player conversation history
    random_agent.py       legal-but-mindless bot (dry runs, fallback)
  web/
    app.py          FastAPI routes + websockets (lobby, rooms)
    room.py         Room, HumanAgent (socket-backed Agent), chat, spectators
    rulesets.py     the four presets, custom options, deck composition
    templates/      lobby.html, game.html
    static/         style.css, game.js, lobby.js, identity.js, fonts/
configs/example.yaml
```

## Rough cost

A 16-player game is ~1,900 decisions (14 rounds; ~10 open turns per player per round plus passes and
votes). A finalist's context grows to ~50k tokens; the average request is ~30k, nearly all cached.
`result.json` reports the real spend per player. Note that OpenRouter caps prompt size against your
remaining credit balance, so a low balance fails long-context requests before the model's limit does.
