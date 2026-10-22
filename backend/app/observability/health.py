"""Authenticated health details, with no DSNs, endpoints, credentials or error text."""
from __future__ import annotations

import asyncio
from fastapi import Depends, Request
from app.security.auth import AuthUser, get_current_user


def _probe(state):
    pool = getattr(state, "database_pool", None)
    database = "not_configured"
    if pool is not None:
        try:
            with pool.acquire() as conn:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = '3000ms'")
                    cur.execute("SELECT 1")
            database = "reachable"
        except Exception:
            database = "unreachable"
    provisioner = getattr(state, "jammi_provisioner", None)
    jammi = "not_configured"
    if provisioner is not None:
        try:
            jammi = "reachable" if provisioner._client.ping() else "unreachable"
        except Exception:
            jammi = "unreachable"
    config = getattr(state, "intent_config", None)
    llm = {"provider": getattr(config, "provider", None), "model": getattr(config, "model", None)}
    if config is not None:
        llm["tasks"] = {task: {"provider": config.for_task(task).provider, "model": config.for_task(task).model}
                        for task in ("intent", "answerability", "explain", "schema")}
    service = getattr(state, "observability", None)
    try:
        spend = service.budget_status() if service else {"status": "not_configured"}
    except Exception:
        spend = {"status": "unavailable", "enforcement": "pending_llm_hooks"}
    return {"jammi": jammi, "app_database": database, "llm": llm, "spend": spend}


async def health_details(request: Request, _user: AuthUser = Depends(get_current_user)):
    state = request.app.state
    # Keep one outstanding task so a down dependency cannot spawn unlimited blocked probes.
    task = getattr(state, "health_probe_task", None)
    if task is None or task.done():
        task = asyncio.create_task(asyncio.to_thread(_probe, state))
        state.health_probe_task = task
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=3)
    except asyncio.TimeoutError:
        return {"status": "degraded", "checks": "timeout"}
