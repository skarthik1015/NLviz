"""Waiting for a published connection's value index, shared by the eval harnesses.

The index is built in the background after publish. The harnesses used to sleep 40 s and hope; now
they poll /schema until grounding is no longer "building", and record what they waited for, so a
run that started questions before the index was ready says so.
"""
from __future__ import annotations

import time

import requests


def wait_for_grounding(s: requests.Session, base: str, conn: str, *, timeout: float = 240,
                       interval: float = 2) -> tuple[dict, dict]:
    """(the /schema response once grounding is settled, {"grounding": status, "grounding_wait_s": n})."""
    started = time.perf_counter()
    while True:
        schema = s.get(f"{base}/schema", headers={"X-Connection-Id": conn}, timeout=60).json()
        status = schema.get("grounding")
        if status is None:  # a backend from before readiness was exposed
            time.sleep(40)
            schema = s.get(f"{base}/schema", headers={"X-Connection-Id": conn}, timeout=60).json()
            status = "unknown (slept 40 s)"
            break
        if status != "building" or time.perf_counter() - started > timeout:
            break
        time.sleep(interval)
    return schema, {"grounding": status, "grounding_wait_s": round(time.perf_counter() - started, 1)}
