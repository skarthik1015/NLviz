"""Decimal text-token pricing; unknown prices are never silently treated as free."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import ipaddress
import json
from urllib.parse import urlsplit


def money(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("Price and budget values must be finite nonnegative decimals") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Price and budget values must be finite nonnegative decimals")
    return result


def tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Token counts must be nonnegative integers")
    return value


@dataclass(frozen=True)
class Price:
    input_per_million: Decimal
    output_per_million: Decimal

    def cost(self, prompt: int, completion: int) -> Decimal:
        return (tokens(prompt) * self.input_per_million +
                tokens(completion) * self.output_per_million) / Decimal(1_000_000)


class PriceTable:
    def __init__(self, overrides: str = ""):
        # Standard text-token rates, checked 2026-10-17:
        # https://developers.openai.com/api/docs/models/gpt-4.1-mini
        default = Price(Decimal("0.40"), Decimal("1.60"))
        self.prices = {"openai/gpt-4.1-mini": default,
                       "openai/gpt-4.1-mini-2025-04-14": default}
        if overrides:
            for name, values in json.loads(overrides).items():
                if not isinstance(name, str) or "/" not in name:
                    raise ValueError("Price keys must be provider/model")
                self.prices[name] = Price(money(values["input"]), money(values["output"]))

    def get(self, provider: str, model: str) -> Price | None:
        return self.prices.get(f"{provider}/{model}")


def is_local(provider: str, base_url: str | None = None) -> bool:
    """Only explicit local providers or known local OpenAI-compatible endpoints bypass."""
    if provider in {"local", "ollama"}:
        return True
    if provider != "openai" or not base_url:
        return False
    host = urlsplit(base_url).hostname
    if host in {"localhost", "ollama", "host.docker.internal"}:
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False
