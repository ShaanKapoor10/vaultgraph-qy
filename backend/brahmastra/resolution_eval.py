"""
Score the LLM merge judge against hand-labelled pairs -- ROADMAP item 7.

The judge (entity_confirm.py) is off by default: four runs on gpt-oss-120b
showed it break-even and unstable. Those runs had no labelled set, so "break-
even" was judged pair by pair after the fact. resolution_cases.json fixes the
questions in advance -- the embedding path's real candidates from the live
graph, answered by hand -- so any model, prompt or setting is scored the same
way:

  stopped   wrong merges the judge refused        (what it is FOR)
  broke     right merges the judge refused        (what it COSTS)
  silent    pairs it did not answer               (kept as before -- merged)

Without a judge every one of these pairs merges, so the judge is worth turning
on only when `stopped` clearly beats `broke`, run after run.

  python -m brahmastra.resolution_eval                 # the configured model
  python -m brahmastra.resolution_eval --model groq:qwen/qwen3.8-27b --runs 3

Each run after the first is a fresh ask (the memo is salted per run), because
stability at temperature 0 was the question last time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

CASES = Path(__file__).with_name("resolution_cases.json")


def load_cases() -> list[dict[str, Any]]:
    return json.loads(CASES.read_text(encoding="utf-8"))["pairs"]


def score(verdicts: dict[tuple[str, str], bool], unanswered: list[tuple[str, str]],
          cases: list[dict[str, Any]]) -> dict[str, Any]:
    out = {"stopped": 0, "missed": 0, "kept": 0, "broke": 0, "silent": len(unanswered),
           "wrong_total": 0, "right_total": 0, "broke_pairs": [], "missed_pairs": []}
    for c in cases:
        pair = (c["a"], c["b"])
        said = verdicts.get(pair)
        if c["same"]:
            out["right_total"] += 1
            if said is False:
                out["broke"] += 1
                out["broke_pairs"].append(f"{c['a']} ~ {c['b']}")
            elif said is True:
                out["kept"] += 1
        else:
            out["wrong_total"] += 1
            if said is False:
                out["stopped"] += 1
            elif said is True:
                out["missed"] += 1
                out["missed_pairs"].append(f"{c['a']} ~ {c['b']}")
    return out


def run(model: str | None = None, runs: int = 1,
        with_context: bool = True) -> list[dict[str, Any]]:
    from brahmastra import entity_confirm
    from brahmastra.llm import using_model

    import os

    cases = load_cases()
    pairs = [(c["a"], c["b"]) for c in cases]
    context = None
    if with_context:
        # The same evidence the pipeline gives it: the live notes' own quotes.
        from brahmastra import db
        from brahmastra.entity_resolution import usage_context

        context = usage_context(db.get_all_triples(), {n for p in pairs for n in p})
    base = entity_confirm.VARIANT
    results = []
    # Measuring the judge means asking it, whether or not the pipeline has it
    # switched on -- the first version of this reported 71 of 71 "silent".
    previous = os.environ.get("ENTITY_CONFIRM")
    os.environ["ENTITY_CONFIRM"] = "1"
    try:
        for i in range(runs):
            # Run 1 may come from the memo; every later run is asked afresh.
            entity_confirm.VARIANT = base if i == 0 else f"{base}#eval{i}"
            with using_model(model):
                verdicts, unanswered = entity_confirm._confirm(pairs, context)
            results.append(score(verdicts, unanswered, cases))
    finally:
        entity_confirm.VARIANT = base
        if previous is None:
            os.environ.pop("ENTITY_CONFIRM", None)
        else:
            os.environ["ENTITY_CONFIRM"] = previous
    return results


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    model = argv[argv.index("--model") + 1] if "--model" in argv else None
    runs = int(argv[argv.index("--runs") + 1]) if "--runs" in argv else 1
    for i, r in enumerate(run(model, runs, with_context="--no-context" not in argv), 1):
        print(f"run {i}: stopped {r['stopped']}/{r['wrong_total']} wrong merges, "
              f"broke {r['broke']}/{r['right_total']} right ones, silent {r['silent']}")
        for p in r["broke_pairs"]:
            print(f"    broke   {p}")
        for p in r["missed_pairs"]:
            print(f"    missed  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
