"""Spend estimate for the Usage page: recorded tokens priced from an operator-supplied YAML table.

``control_plane.usage.prices`` is a list; the first entry whose ``model`` glob matches a record's model prices it:

    control_plane:
      usage:
        prices:
          - {model: "claude-sonnet-*", input: 3.0, output: 15.0, cached: 0.3}   # US dollars per million tokens
          - {model: "qwen*", input: 0, output: 0}                                  # self-hosted, priced at zero on purpose

There are no built-in prices: a model no entry matches is listed as unpriced, never guessed, and with no table the
estimate is simply off. ``cached`` prices cache-read tokens; when it is missing they are charged at the ``input`` rate,
which can only overstate. ``cached_in_input: true`` says the provider counts cache reads inside ``input_tokens`` (OpenAI
style) so they are not charged twice; the default is the Anthropic style, where they are separate. Reasoning tokens are
taken to be part of ``output_tokens`` and are not charged again. Pure functions; the repo supplies the records.
"""
from __future__ import annotations

import datetime as _dt
import fnmatch
import math

MAX_PRICES = 500
_RATE_KEYS = ("input", "output", "cached")
_ALLOWED = {"model", "cached_in_input", *_RATE_KEYS}


def usage_prices_config(mapping) -> list[dict]:
    """Validated price table from ``control_plane.usage`` (None = no table). Raises ValueError naming the bad key."""
    if mapping is None:
        return []
    if not isinstance(mapping, dict):
        raise ValueError("control_plane.usage must be a mapping")
    unknown = set(mapping) - {"prices"}
    if unknown:
        raise ValueError(f"control_plane.usage has an unknown key: {sorted(unknown)[0]}")
    prices = mapping.get("prices")
    if prices is None:
        return []
    if not isinstance(prices, list) or len(prices) > MAX_PRICES:
        raise ValueError(f"control_plane.usage.prices must be a list of at most {MAX_PRICES} entries")
    out = []
    for i, entry in enumerate(prices):
        where = f"control_plane.usage.prices[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a mapping")
        if set(entry) - _ALLOWED:
            raise ValueError(f"{where} has an unknown key: {sorted(set(entry) - _ALLOWED)[0]}")
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise ValueError(f"{where}.model must be a model name or glob")
        row = {"model": model}
        for key in ("input", "output"):
            row[key] = _rate(entry.get(key), f"{where}.{key}", required=True)
        row["cached"] = _rate(entry.get("cached"), f"{where}.cached", required=False)
        flag = entry.get("cached_in_input", False)
        if not isinstance(flag, bool):
            raise ValueError(f"{where}.cached_in_input must be true or false")
        row["cached_in_input"] = flag
        out.append(row)
    return out


def _rate(value, where, *, required):
    if value is None:
        if required:
            raise ValueError(f"{where} is required (dollars per million tokens; 0 is allowed)")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{where} must be a number >= 0 (dollars per million tokens)")
    return float(value)


def price_for(prices, model):
    return next((p for p in prices if fnmatch.fnmatchcase(model, p["model"])), None)


def _count(payload, key):
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def cost(price, tokens) -> float:
    """Dollars for one token bucket under one price entry."""
    cached = tokens["cache_read"]
    billed_input = max(0, tokens["input"] - cached) if price["cached_in_input"] else tokens["input"]
    cached_rate = price["input"] if price["cached"] is None else price["cached"]
    return (billed_input * price["input"] + tokens["output"] * price["output"] + cached * cached_rate) / 1_000_000


def _day(ts):
    return _dt.datetime.fromtimestamp(ts, _dt.UTC).strftime("%Y-%m-%d")


def estimate(records, prices) -> dict:
    """``records`` is ``[(ts, payload)]``. Per model, per UTC day and in total; unpriced models are listed with their tokens."""
    if not prices:
        return {"configured": False}
    cells: dict[tuple[str, str], dict] = {}
    for ts, p in records:
        model = p.get("model") or "unknown"
        cell = cells.setdefault((_day(ts), model), {"input": 0, "output": 0, "cache_read": 0, "requests": 0})
        cell["requests"] += 1
        cell["input"] += _count(p, "input_tokens")
        cell["output"] += _count(p, "output_tokens")
        cell["cache_read"] += _count(p, "cache_read_tokens")
    days: dict[str, dict] = {}
    models: dict[str, dict] = {}
    for (day, model), tokens in sorted(cells.items()):
        price = price_for(prices, model)
        value = None if price is None else cost(price, tokens)
        entry = {"model": model, "tokens": tokens, "cost": value}
        d = days.setdefault(day, {"day": day, "models": [], "cost": 0.0, "unpriced_tokens": 0})
        d["models"].append(entry)
        m = models.setdefault(model, {"model": model, "priced": price is not None, "match": price["model"] if price else None,
                                      "tokens": {"input": 0, "output": 0, "cache_read": 0, "requests": 0}, "cost": 0.0 if price else None})
        for k, v in tokens.items():
            m["tokens"][k] += v
        if value is None:
            d["unpriced_tokens"] += tokens["input"] + tokens["output"] + tokens["cache_read"]
        else:
            d["cost"] += value
            m["cost"] += value
    priced = [m for m in models.values() if m["priced"]]
    unpriced = [{"model": m["model"], "tokens": m["tokens"]} for m in models.values() if not m["priced"]]
    unpriced.sort(key=lambda m: -(m["tokens"]["input"] + m["tokens"]["output"] + m["tokens"]["cache_read"]))
    return {"configured": True, "estimate": True, "currency": "USD",
            "days": [dict(d, cost=round(d["cost"], 6)) for d in days.values()],
            "by_model": sorted(({**m, "cost": round(m["cost"], 6)} for m in priced), key=lambda m: -m["cost"]),
            "unpriced": unpriced,
            "total": round(sum(m["cost"] for m in priced), 6)}
