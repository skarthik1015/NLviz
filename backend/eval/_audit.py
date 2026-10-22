"""The audit trail of one question, as saved in eval results (docs/ablation-results.md).

Ask with "debug": true (on /chat and on each /chat/resume) and pass every response of the question
here. A resumed turn's last response already holds all of the turn's steps; LLM calls are counted
per request, so they are added up across the responses.
"""
from __future__ import annotations


def merge(bodies: list[dict]) -> dict | None:
    audits = [b.get("audit") for b in bodies if isinstance(b, dict) and b.get("audit")]
    if not audits:
        return None
    calls = [call for audit in audits for call in audit.get("llm_calls") or ()]
    return {
        "run": audits[0].get("run"),
        "steps": audits[-1].get("steps") or [],
        "llm_calls": calls,
        "tokens": {"prompt": sum(c.get("prompt_tokens") or 0 for c in calls),
                   "completion": sum(c.get("completion_tokens") or 0 for c in calls)},
        "sql": bodies[-1].get("sql"),
    }
