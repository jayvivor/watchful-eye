from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from .config import load_config
from .engine import Game


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="watchful-eye", description="Simulate Watchful Eye with LLM players.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Play one or more games from a YAML config")
    run.add_argument("config")
    run.add_argument("--dry-run", action="store_true", help="Replace every LLM player with a random legal bot (no API calls)")
    run.add_argument("--games", type=int, default=1, help="Play N games (seed increments each game)")
    run.add_argument("--seed", type=int, default=None, help="Override the config seed")
    run.add_argument("--quiet", action="store_true", help="Do not echo the transcript")

    val = sub.add_parser("validate", help="Validate a config and print the resolved deck")
    val.add_argument("config")

    args = ap.parse_args(argv)
    cfg = load_config(args.config)

    if args.cmd == "validate":
        counts = cfg.deck.counts_by_kind()
        print(f"OK: {len(cfg.players)} players, deck of {sum(counts.values())} cards")
        for k, n in counts.items():
            print(f"  {k.value:<10} {n}")
        for p in cfg.players:
            m = p.resolved_model(cfg.llm.default_model).model if p.agent == "llm" else "random"
            print(f"  player {p.name:<12} {p.agent:<7} {m}")
        return 0

    if args.quiet:
        cfg = cfg.model_copy(update={"output": cfg.output.model_copy(update={"verbose": False})})
    base_seed = args.seed if args.seed is not None else cfg.seed
    results = []
    for i in range(args.games):
        seed = None if base_seed is None else base_seed + i
        game_cfg = cfg.model_copy(update={"seed": seed})
        game = Game(game_cfg, dry_run=args.dry_run)
        res = game.run()
        results.append(res)
        print(f"\n>> game {i + 1}/{args.games}: winner={res['winner']} rounds={res['rounds']} "
              f"cost=${res['total_cost_usd']:.4f} → {res['run_dir']}\n", file=sys.stderr)
    if args.games > 1:
        wins = Counter(r["winner"] for r in results)
        print(json.dumps({"games": args.games, "wins": dict(wins),
                          "avg_rounds": sum(r["rounds"] for r in results) / len(results),
                          "total_cost_usd": round(sum(r["total_cost_usd"] for r in results), 4)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
