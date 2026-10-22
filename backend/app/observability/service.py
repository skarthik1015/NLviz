"""Public per-attempt hooks, independent of any provider SDK or request lifecycle."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from decimal import Decimal
import json
import logging
import os
from threading import Lock

from .budget import BudgetExceeded, Reservation, SpendStore
from .pricing import Price, PriceTable, is_local, money, tokens

logger = logging.getLogger("nlviz.questions")


def configure_json_logging() -> None:
    """Keep these events as standalone JSON lines, independent of root log formatting."""
    if not any(getattr(handler, "_nlviz_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._nlviz_json = True
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@dataclass
class QuestionContext:
    request_id: str
    user: str | None = None
    calls: list[dict] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock)


question_context: ContextVar[QuestionContext | None] = ContextVar("question_observability", default=None)


@dataclass(frozen=True)
class Call:
    provider: str
    model: str
    local: bool
    price: Price | None
    reservation: Reservation | None
    context: QuestionContext | None


class Observability:
    # Set true only when app.llm wraps EVERY actual provider attempt, including SDK retries.
    hooks_integrated = False

    def __init__(self, store: SpendStore, *, prices=None, cap=None, debug_text=False):
        self.store = store
        self.prices = prices or PriceTable()
        self.cap = None if cap is None else money(cap)
        self.debug_text = debug_text

    @classmethod
    def from_env(cls, *, pool=None):
        value = os.getenv("LLM_MONTHLY_BUDGET_USD", "").strip()
        return cls(
            SpendStore(os.getenv("LLM_SPEND_SQLITE_PATH", "data/llm-spend.sqlite3"), pool=pool),
            prices=PriceTable(os.getenv("LLM_PRICE_TABLE_JSON", "")),
            cap=money(value) if value else None,
            debug_text=os.getenv("INTENT_DEBUG_LOGGING", "false").lower() in {"1", "true", "yes", "on"},
        )

    def before_call(self, *, provider: str, model: str, prompt_tokens_upper_bound: int,
                    max_completion_tokens: int, base_url: str | None = None) -> Call:
        """Reserve BEFORE each billable attempt; never pass an estimated lower bound."""
        tokens(prompt_tokens_upper_bound)
        tokens(max_completion_tokens)
        local = is_local(provider, base_url)
        price = self.prices.get(provider, model)
        reservation = None
        if not local:
            if price is None and self.cap is not None:
                raise BudgetExceeded("Hosted-model price unavailable; configure LLM_PRICE_TABLE_JSON before calling")
            if price is not None:
                reservation = self.store.reserve(price.cost(prompt_tokens_upper_bound, max_completion_tokens), self.cap)
        return Call(provider, model, local, price, reservation, question_context.get())

    def after_call(self, call: Call, *, prompt_tokens: int | None = None,
                   completion_tokens: int | None = None, failed: bool = False) -> None:
        """Finish each attempt, including failed validation/retries and unknown-usage errors.

        If usage is unavailable, charge the reserved upper bound. Never release uncertain spend.
        """
        complete = prompt_tokens is not None and completion_tokens is not None
        if prompt_tokens is not None:
            tokens(prompt_tokens)
        if completion_tokens is not None:
            tokens(completion_tokens)
        cost = (Decimal(0) if call.local else call.price.cost(prompt_tokens, completion_tokens)
                if call.price and complete else None)
        if call.reservation:
            if not self.store.settle(call.reservation, cost):
                return
        event = {"provider": call.provider, "model": call.model,
                 "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                 "cost_usd": str(cost) if cost is not None else None,
                 "usage_complete": complete, "failed": failed,
                 "charged_usd": str(cost if cost is not None else call.reservation.amount)
                 if cost is not None or call.reservation else None}
        if call.context is not None:
            with call.context.lock:
                call.context.calls.append(event)
        logger.info(json.dumps({"event": "llm_call", "entry_point": "llm",
                                "request_id": call.context.request_id if call.context else None,
                                **event}, separators=(",", ":")))

    def budget_status(self):
        return {**self.store.snapshot(), "cap_usd": str(self.cap) if self.cap is not None else None,
                "enforcement": "active" if self.hooks_integrated else "pending_llm_hooks"}


_service: Observability | None = None


def configure(service: Observability | None) -> None:
    """Install at app startup; call with None during shutdown. Does not patch app.llm."""
    global _service
    _service = service


def before_call(**kwargs) -> Call:
    if _service is None:
        raise RuntimeError("LLM observability must be configured before provider calls")
    return _service.before_call(**kwargs)


def after_call(call: Call, **kwargs) -> None:
    if _service is None:
        raise RuntimeError("LLM observability must be configured before provider calls")
    _service.after_call(call, **kwargs)
