"""Benchmark harness for the app (plan step 0b; approaches B and C of step 0d).

Two steps, so every arm answers on the same semantic layer:

    # 1. Create, generate and publish one connection per database; ids saved to --state.
    python eval/bench/run.py setup --state <file> --ro-password-file <pw> --taxi-dir <dir> [--databases tpch,taxi]

    # 2. Ask every case (turns of a case share a conversation); clarifications answered as scripted.
    python eval/bench/run.py ask --state <file> --cases eval/bench/cases/dev.jsonl --arm nlviz-full --out <json>
        [--databases ...] [--ids tpch-01,...] [--repeat 3]

The arm's configuration (model, PHRASE_REWRITES) is the running backend's; each answer's audit
records it, and `ask` refuses to mix runs whose model or rule setting differ.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]

from _audit import merge  # noqa: E402
from _wait import wait_for_grounding  # noqa: E402
from score import OUTCOMES, clarification_matches, final_correct, first_outcome  # noqa: E402

DB_HOST_FOR_APP = "host.docker.internal"  # as the backend and jammi-server containers see the dev databases
DATABASES = {
    "tpch": {"connector_type": "postgres", "params": {"port": 15432, "dbname": "tpch"}},
    "employees": {"connector_type": "mysql", "params": {"port": 13306, "dbname": "employees"}},
    "taxi": {"files": ["yellow_tripdata_2024-01.parquet", "taxi_zone_lookup.csv"]},
}
MAX_CLARIFICATIONS = 3
ENDPOINTS = {"chat": "/chat", "bench-sql": "/bench/sql", "bench-sql-guarded": "/bench/sql"}
# Extra request fields per endpoint name.
ENDPOINT_FIELDS = {"bench-sql-guarded": {"guard": True}}


# --------------------------------------------------------------------------------------------- setup
def _publish(s: requests.Session, base: str, conn: str) -> dict:
    job = s.post(f"{base}/connections/{conn}/generate", timeout=60).json()
    started = time.perf_counter()
    while (status := s.get(f"{base}/connections/{conn}/jobs/{job['job_id']}", timeout=60).json())["status"] not in (
            "succeeded", "failed"):
        time.sleep(3)
    info = {"generation_seconds": round(time.perf_counter() - started, 1), "job_status": status["status"],
            "validation": status.get("validation_summary")}
    if not status.get("schema_version_id"):
        raise RuntimeError(f"generation failed for {conn}: {status.get('error')}")
    published = s.post(f"{base}/connections/{conn}/publish", json={"version_id": status["schema_version_id"]},
                       timeout=600)
    published.raise_for_status()
    schema, waited = wait_for_grounding(s, base, conn)
    info.update(waited, metrics=[m["name"] for m in schema["metrics"]],
                dimensions=[d["name"] for d in schema["dimensions"]])
    return info


def setup(args) -> None:
    s = requests.Session()
    state = json.loads(Path(args.state).read_text()) if Path(args.state).exists() else {}
    password = Path(args.ro_password_file).read_text().strip() if args.ro_password_file else None
    specs = json.loads(Path(args.spec).read_text(encoding="utf-8")) if args.spec else DATABASES
    for name in args.databases.split(","):
        spec = specs[name]
        label = f"bench {name} {time.strftime('%Y%m%d-%H%M')}"
        if "files" in spec:
            first, *rest = spec["files"]
            with (Path(spec.get("dir") or args.taxi_dir) / first).open("rb") as handle:
                created = s.post(f"{args.base}/connections/upload", files={"file": (first, handle)},
                                 data={"display_name": label}, timeout=1800)
            created.raise_for_status()
            conn = created.json()["connection_id"]
            for extra in rest:
                with (Path(spec.get("dir") or args.taxi_dir) / extra).open("rb") as handle:
                    s.post(f"{args.base}/connections/{conn}/files", files={"file": (extra, handle)},
                           timeout=600).raise_for_status()
        else:
            params = {"host": DB_HOST_FOR_APP, "user": spec.get("user", "nlviz_ro"), "password": password, **spec["params"]}
            created = s.post(f"{args.base}/connections", json={"connector_type": spec["connector_type"],
                                                               "params": params, "display_name": label}, timeout=1800)
            created.raise_for_status()
            conn = created.json()["connection_id"]
        info = _publish(s, args.base, conn)
        state[name] = {"connection_id": conn, "display_name": label, **info}
        Path(args.state).write_text(json.dumps(state, indent=2))
        print(f"{name}: {conn} ({len(info['metrics'])} metrics, {len(info['dimensions'])} dimensions, "
              f"grounding {info.get('grounding')})")


# ----------------------------------------------------------------------------------------------- ask
def _reply(clarification: dict, turn: dict) -> tuple[object, str]:
    """The scripted answer to the app's clarification, and how it was chosen."""
    entry = clarification_matches(clarification, turn.get("acceptable_clarifications") or [])
    if entry is not None and "reply" in entry:
        return entry["reply"], "scripted"
    if clarification.get("kind") == "value":
        return clarification.get("any_value"), "default: any value"
    choices = clarification.get("choices") or []
    return (choices[0]["id"] if choices else None), "default: first choice"


def _post(s: requests.Session, url: str, payload: dict, conn: str) -> dict:
    for wait in (2, 5, 10, 20, 40):
        r = s.post(url, json=payload, headers={"X-Connection-Id": conn}, timeout=600)
        # The app's per-user rate limit: wait, rather than score a refusal the question never got.
        if r.status_code != 429:
            break
        time.sleep(wait)
    if r.status_code != 200:
        return {"status": "error", "error": f"{r.status_code} {r.text[:300]}"}
    return r.json()


def ask_turn(s: requests.Session, base: str, conn: str, conversation: str, turn: dict, endpoint: str = "/chat",
             fields: dict | None = None) -> dict:
    started = time.perf_counter()
    first = _post(s, f"{base}{endpoint}", {"question": turn["question"], "conversation_id": conversation, "debug": True,
                                           **(fields or {})}, conn)
    bodies, asked, body = [first], [], first
    while body.get("status") == "clarification" and len(asked) < MAX_CLARIFICATIONS:
        clarification = body.get("clarification") or {}
        reply, how = _reply(clarification, turn)
        asked.append({"clarification": clarification, "reply": reply, "how": how})
        body = _post(s, f"{base}/chat/resume", {"conversation_id": conversation, "answer": reply, "debug": True}, conn)
        bodies.append(body)
    if first.get("status") == "error":
        outcome, review = "error", False
    else:
        outcome, review = first_outcome(turn, first)
    return {
        "question": turn["question"],
        "first_outcome": outcome,
        "needs_review": review,
        "final_correct": body.get("status") != "error" and final_correct(turn, body),
        "first_status": first.get("status"),
        "final_status": body.get("status"),
        "clarifications": asked,
        "intent": body.get("intent"),
        "rows": (body.get("rows") or [])[:10],
        "row_count": body.get("row_count"),
        "expected": (turn.get("expected_result") or {}).get("rows", [])[:10] if turn.get("expected_result") else None,
        "error": body.get("error"),
        "seconds": round(time.perf_counter() - started, 2),
        "audit": merge([b for b in bodies if b.get("status") != "error"]),
    }


def _summary(results: list[dict]) -> dict:
    turns = [(r, t) for r in results for t in r["turns"]]

    def tally(pairs) -> dict:
        outcomes = Counter(t["first_outcome"] for _, t in pairs)
        return {"turns": len(pairs), **{o: outcomes.get(o, 0) for o in (*OUTCOMES, "error")},
                "final_correct": sum(t["final_correct"] for _, t in pairs),
                "needs_review": sum(t["needs_review"] for _, t in pairs)}

    groups: dict[str, dict] = {"all": tally(turns)}
    for key in ("database", "category", "grammar_supported"):
        split = defaultdict(list)
        for r, t in turns:
            split[f"{key}={r[key]}"].append((r, t))
        groups.update({name: tally(pairs) for name, pairs in sorted(split.items())})
    tokens = [t["audit"]["tokens"] for _, t in turns if t.get("audit")]
    groups["tokens"] = {"prompt": sum(x["prompt"] for x in tokens), "completion": sum(x["completion"] for x in tokens)}
    seconds = sorted(t["seconds"] for _, t in turns)
    groups["seconds"] = {"p50": seconds[len(seconds) // 2], "max": seconds[-1]} if seconds else {}
    return groups


def ask(args) -> None:
    state = json.loads(Path(args.state).read_text())
    cases = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.databases:
        cases = [c for c in cases if c["database"] in args.databases.split(",")]
    if args.ids:
        cases = [c for c in cases if c["id"] in args.ids.split(",")]
    s = requests.Session()
    runs = []
    for repeat in range(args.repeat):
        results = []
        for case in cases:
            conn = state[case["database"]]["connection_id"]
            conversation = f"bench-{uuid.uuid4().hex[:12]}"
            # Extra fields only for the endpoints that have them: the holdout runner wraps ask_turn
            # with the original six-argument signature (a seventh positional broke the frozen run).
            extra = {"fields": ENDPOINT_FIELDS[args.endpoint]} if args.endpoint in ENDPOINT_FIELDS else {}
            turns = [ask_turn(s, args.base, conn, conversation, turn, ENDPOINTS[args.endpoint], **extra)
                     for turn in case["turns"]]
            results.append({key: case[key] for key in ("id", "family", "database", "category", "grammar_supported")}
                           | {"turns": turns})
            marks = " ".join(f"{t['first_outcome']}{'' if t['final_correct'] else '(final wrong)'}" for t in turns)
            print(f"[{repeat + 1}] {case['id']:<9} {marks}")
        runs.append({"repeat": repeat + 1, "results": results, "summary": _summary(results)})
    configs = {json.dumps((t["audit"] or {}).get("run"), sort_keys=True) for run in runs for r in run["results"]
               for t in r["turns"] if t.get("audit")}
    # Each database has its own layer (schema fingerprint); only the rest must agree across answers.
    settings = {json.dumps({k: v for k, v in json.loads(c).items() if k != "schema"}, sort_keys=True)
                for c in configs if c != "null"}
    if len(settings) > 1:
        print("WARNING: answers came from different configurations:", *settings, sep="\n  ")
    out = {"arm": args.arm, "endpoint": args.endpoint, "cases_file": args.cases, "state": state, "configurations": sorted(configs), "runs": runs}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    for run in runs:
        print(json.dumps({"repeat": run["repeat"], **run["summary"]["all"]}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["setup", "ask"])
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--state", required=True)
    parser.add_argument("--databases", default="")
    parser.add_argument("--ro-password-file")
    parser.add_argument("--taxi-dir")
    # Other databases (the sealed holdout): a JSON file shaped like DATABASES, each entry either
    # {"connector_type", "params": {port, dbname}[, "user"]} or {"files": [...], "dir": <folder>}.
    parser.add_argument("--spec")
    parser.add_argument("--cases", default=str(HERE / "cases" / "dev.jsonl"))
    parser.add_argument("--ids", default="")
    parser.add_argument("--arm", default="nlviz")
    parser.add_argument("--repeat", type=int, default=1)
    # chat: NLviz (approaches B and C); bench-sql: the text-to-SQL baseline (approach A, BENCH_ENDPOINTS=on).
    # Names, not paths: Git Bash rewrites an argument starting with "/" into a Windows path.
    parser.add_argument("--endpoint", default="chat", choices=sorted(ENDPOINTS))
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.command == "setup":
        args.databases = args.databases or ",".join(json.loads(Path(args.spec).read_text()) if args.spec else DATABASES)
        setup(args)
    else:
        if not args.out:
            parser.error("ask needs --out")
        ask(args)


if __name__ == "__main__":
    main()
