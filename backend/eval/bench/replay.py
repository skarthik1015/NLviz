"""Rewrite replay (plan step 0d): the LLM proposals recorded in an NLviz run, through the agent again
with the phrase rules on and off. Same model output on both sides, so the difference is the rules'.

    python eval/bench/replay.py --run <run.py ask output, /chat arm> --out <json> [--cases eval/bench/cases/dev.jsonl]

Needs BENCH_ENDPOINTS=on. The "on" replay should reproduce the recorded run; how often it does is
reported (`reproduced`), as a check on the replay itself.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from score import final_correct, first_outcome  # noqa: E402


def _turns(recorded: dict) -> list[dict]:
    turns = []
    for turn in recorded["turns"]:
        steps = (turn.get("audit") or {}).get("steps") or []
        turns.append({"question": turn["question"],
                      "proposals": [s for s in steps if s.get("stage") == "proposal"],
                      "answers": [c["reply"] for c in turn.get("clarifications") or ()]})
    return turns


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", required=True)
    parser.add_argument("--cases", default=str(HERE / "cases" / "dev.jsonl"))
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    run = json.loads(Path(args.run).read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in (json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines()
                                  if line.strip())}
    s = requests.Session()
    records, tally = [], {True: Counter(), False: Counter()}
    reproduced = total = 0
    for repeat in run["runs"]:
        for recorded in repeat["results"]:
            case = cases[recorded["id"]]
            conn = run["state"][case["database"]]["connection_id"]
            turns = _turns(recorded)
            sides = {}
            for rules in (True, False):
                r = s.post(f"{args.base}/bench/replay", json={"turns": turns, "phrase_rewrites": rules},
                           headers={"X-Connection-Id": conn}, timeout=1800)
                if r.status_code != 200:
                    sides[rules] = [{"error": r.text[:300]} for _ in turns]
                    continue
                scored = []
                for spec, result in zip(case["turns"], r.json()["turns"]):
                    outcome, _ = first_outcome(spec, result["first"])
                    scored.append({"first_outcome": outcome, "final_correct": final_correct(spec, result["final"]),
                                   "intent": result["final"].get("intent")})
                sides[rules] = scored
            for i, original in enumerate(recorded["turns"]):
                on, off = sides[True][i], sides[False][i]
                for rules, side in ((True, on), (False, off)):
                    tally[rules][side.get("first_outcome", "error")] += 1
                    tally[rules]["final_correct"] += bool(side.get("final_correct"))
                total += 1
                reproduced += on.get("first_outcome") == original["first_outcome"]
                records.append({"id": recorded["id"], "turn": i + 1, "repeat": repeat["repeat"],
                                "recorded": original["first_outcome"], "rules_on": on, "rules_off": off,
                                "differs": (on.get("first_outcome"), on.get("final_correct")) !=
                                           (off.get("first_outcome"), off.get("final_correct"))})
            print(recorded["id"], [(r["rules_on"].get("first_outcome"), r["rules_off"].get("first_outcome"))
                                   for r in records if r["id"] == recorded["id"] and r["repeat"] == repeat["repeat"]])
    summary = {"turns": total, "reproduced": reproduced, "rules_on": dict(tally[True]), "rules_off": dict(tally[False]),
               "differs": sum(r["differs"] for r in records)}
    Path(args.out).write_text(json.dumps({"run": args.run, "summary": summary, "turns": records}, indent=1,
                                         ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
