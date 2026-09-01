"""Reviewed server-owned pricing for MCP-visible API-equivalent cost simulation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

PRICING_REGISTRY_VERSION = "openai-2026-08-21-promo"
PRICING_VERIFIED_AT = date(2026, 9, 2)
OPENAI_PRICING_SOURCE_LABEL = "OpenAI API model pricing"
OPENAI_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/models/compare"


@dataclass(frozen=True, slots=True)
class PricingEntry:
    model_id: str
    input_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    valid_through: date | None = None
    source_label: str = OPENAI_PRICING_SOURCE_LABEL
    source_url: str = OPENAI_PRICING_SOURCE_URL


_PRICING: dict[str, PricingEntry] = {
    "gpt-5.6-sol": PricingEntry(
        "gpt-5.6-sol", Decimal("4.00"), Decimal("0.40"), Decimal("20.00"), date(2026, 11, 21)
    ),
    "gpt-5.6-sol-pro": PricingEntry(
        "gpt-5.6-sol-pro", Decimal("5.00"), Decimal("0.50"), Decimal("30.00")
    ),
    "gpt-5.6-terra": PricingEntry(
        "gpt-5.6-terra", Decimal("2.00"), Decimal("0.20"), Decimal("12.00")
    ),
    "gpt-5.6-luna": PricingEntry("gpt-5.6-luna", Decimal("0.20"), Decimal("0.02"), Decimal("1.20")),
    "gpt-5.5": PricingEntry("gpt-5.5", Decimal("5.00"), Decimal("0.50"), Decimal("30.00")),
    "gpt-5.4": PricingEntry("gpt-5.4", Decimal("2.50"), Decimal("0.25"), Decimal("15.00")),
    "gpt-5.4-mini": PricingEntry(
        "gpt-5.4-mini", Decimal("0.75"), Decimal("0.075"), Decimal("4.50")
    ),
    "gpt-5.3-codex": PricingEntry(
        "gpt-5.3-codex", Decimal("1.75"), Decimal("0.175"), Decimal("14.00")
    ),
    "gpt-5.2": PricingEntry("gpt-5.2", Decimal("1.75"), Decimal("0.175"), Decimal("14.00")),
}

_ALIASES: dict[str, str] = {
    "gpt-5.6-sol": "gpt-5.6-sol",
    "gpt-5.6": "gpt-5.6-sol",
    "gpt-5.6-sol-pro": "gpt-5.6-sol-pro",
    "gpt-5.6-terra": "gpt-5.6-terra",
    "gpt-5.6-luna": "gpt-5.6-luna",
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",
    "gpt-5.4-mini": "gpt-5.4-mini",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.2": "gpt-5.2",
}


def _model_key(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "-".join(value.strip().lower().replace("_", " ").split())
    return cleaned or None


def normalize_pricing_model(model_reported: str | None, model_canonical: str | None) -> str | None:
    """Resolve only explicitly reviewed exact aliases; never fuzzy-match model text."""

    canonical_key = _model_key(model_canonical)
    if canonical_key in _PRICING:
        return canonical_key
    reported_key = _model_key(model_reported)
    return _ALIASES.get(reported_key or "")


def _decimal_text(value: Decimal) -> str:
    # Keep enough precision for tiny per-request MCP costs without binary float drift.
    quantized = value.quantize(Decimal("0.000000000001"))
    text = format(quantized, "f").rstrip("0").rstrip(".")
    return text or "0"


def _base_projection(event: Any) -> dict[str, object]:
    if hasattr(event, "model_dump"):
        return dict(event.model_dump(mode="json"))
    return dict(event)


def project_usage_cost(event: Any, *, today: date | None = None) -> dict[str, object]:
    """Project reviewed API-equivalent cost onto one strict usage event."""

    projected = _base_projection(event)
    now = today or date.today()
    reported = projected.get("model_reported")
    canonical = normalize_pricing_model(
        reported if isinstance(reported, str) else None,
        projected.get("model_canonical")
        if isinstance(projected.get("model_canonical"), str)
        else None,
    )

    pricing_status: str
    if not reported:
        pricing_status = "model_not_reported"
    elif canonical is None:
        pricing_status = "unknown_model"
    else:
        entry = _PRICING[canonical]
        pricing_status = "stale" if entry.valid_through and now > entry.valid_through else "current"

    projected.update(
        {
            "model_canonical": canonical,
            "pricing_status": pricing_status,
            "pricing_version": PRICING_REGISTRY_VERSION,
            "pricing_verified_at": PRICING_VERIFIED_AT.isoformat(),
            "pricing_valid_through": None,
            "pricing_source_label": OPENAI_PRICING_SOURCE_LABEL,
            "pricing_source_url": OPENAI_PRICING_SOURCE_URL,
            "input_usd_per_million": None,
            "cached_input_usd_per_million": None,
            "output_usd_per_million": None,
            "input_cost_usd": None,
            "cached_input_cost_usd": None,
            "output_cost_usd": None,
            "simulated_cost_usd": None,
        }
    )
    if canonical is None:
        return projected

    entry = _PRICING[canonical]
    input_tokens = Decimal(int(projected.get("input_tokens_estimated") or 0))
    output_tokens = Decimal(int(projected.get("output_tokens_estimated") or 0))
    million = Decimal(1_000_000)
    input_cost = input_tokens / million * entry.input_usd_per_million
    output_cost = output_tokens / million * entry.output_usd_per_million
    projected.update(
        {
            "pricing_valid_through": entry.valid_through.isoformat()
            if entry.valid_through
            else None,
            "pricing_source_label": entry.source_label,
            "pricing_source_url": entry.source_url,
            "input_usd_per_million": str(entry.input_usd_per_million),
            "cached_input_usd_per_million": str(entry.cached_input_usd_per_million),
            "output_usd_per_million": str(entry.output_usd_per_million),
            "input_cost_usd": _decimal_text(input_cost),
            "cached_input_cost_usd": None,
            "output_cost_usd": _decimal_text(output_cost),
            "simulated_cost_usd": _decimal_text(input_cost + output_cost),
        }
    )
    return projected
