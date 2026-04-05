from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BookSummary:
    best_bid: float | None
    best_ask: float | None
    mid: float | None


def _to_float(x: Any) -> float | None:
    try:
        v = float(x)
        if v != v:
            return None
        return v
    except Exception:
        return None


def summarize_levels(bids: list[Any], asks: list[Any]) -> BookSummary:
    best_bid = None
    best_ask = None

    for level in bids:
        if isinstance(level, dict):
            p = _to_float(level.get("price"))
        elif isinstance(level, (list, tuple)) and len(level) >= 1:
            p = _to_float(level[0])
        else:
            p = None
        if p is None:
            continue
        if best_bid is None or p > best_bid:
            best_bid = p

    for level in asks:
        if isinstance(level, dict):
            p = _to_float(level.get("price"))
        elif isinstance(level, (list, tuple)) and len(level) >= 1:
            p = _to_float(level[0])
        else:
            p = None
        if p is None:
            continue
        if best_ask is None or p < best_ask:
            best_ask = p

    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0

    return BookSummary(best_bid=best_bid, best_ask=best_ask, mid=mid)


def normalize_polymarket_levels(levels: list[Any]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for level in levels:
        if isinstance(level, dict):
            p = _to_float(level.get("price"))
            s = _to_float(level.get("size"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _to_float(level[0])
            s = _to_float(level[1])
        else:
            continue
        if p is None or s is None:
            continue
        out.append({"price": p, "size": s})
    return out


def normalize_ccxt_levels(levels: list[Any]) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for level in levels:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            p = _to_float(level[0])
            s = _to_float(level[1])
        elif isinstance(level, dict):
            p = _to_float(level.get("price"))
            s = _to_float(level.get("amount"))
        else:
            continue
        if p is None or s is None:
            continue
        out.append({"price": p, "size": s})
    return out


def add_usd_depth(levels: list[dict[str, float]], multiplier: float | None) -> list[dict[str, float | None]]:
    out: list[dict[str, float | None]] = []
    cum = 0.0
    for lvl in levels:
        price = float(lvl["price"])
        size = float(lvl["size"])
        notional_native = price * size
        notional_usd = (notional_native * float(multiplier)) if multiplier is not None else None
        if notional_usd is not None:
            cum += notional_usd
        out.append(
            {
                **lvl,
                "notional_native": notional_native,
                "notional_usd": notional_usd,
                "cum_notional_usd": cum if multiplier is not None else None,
            }
        )
    return out


@dataclass(frozen=True)
class SimulatedFill:
    qty: float
    avg_price: float | None
    notional_native: float
    notional_usd: float | None
    worst_price: float | None
    levels_touched: int


def simulate_buy_by_budget(
    asks: list[dict[str, float]],
    budget_native: float,
    usd_multiplier: float | None,
) -> SimulatedFill:
    remaining = float(budget_native)
    qty = 0.0
    spent = 0.0
    worst_price = None
    touched = 0
    for lvl in asks:
        if remaining <= 0:
            break
        price = float(lvl["price"])
        size = float(lvl["size"])
        if price <= 0 or size <= 0:
            continue
        max_affordable = remaining / price
        take = size if size <= max_affordable else max_affordable
        if take <= 0:
            continue
        qty += take
        cost = take * price
        spent += cost
        remaining -= cost
        worst_price = price
        touched += 1
    avg = (spent / qty) if qty > 0 else None
    notional_usd = (spent * float(usd_multiplier)) if usd_multiplier is not None else None
    return SimulatedFill(
        qty=qty,
        avg_price=avg,
        notional_native=spent,
        notional_usd=notional_usd,
        worst_price=worst_price,
        levels_touched=touched,
    )


def simulate_sell_qty(
    bids: list[dict[str, float]],
    qty_to_sell: float,
    usd_multiplier: float | None,
) -> SimulatedFill:
    remaining = float(qty_to_sell)
    qty = 0.0
    proceeds = 0.0
    worst_price = None
    touched = 0
    for lvl in bids:
        if remaining <= 0:
            break
        price = float(lvl["price"])
        size = float(lvl["size"])
        if price <= 0 or size <= 0:
            continue
        take = size if size <= remaining else remaining
        if take <= 0:
            continue
        qty += take
        got = take * price
        proceeds += got
        remaining -= take
        worst_price = price
        touched += 1
    avg = (proceeds / qty) if qty > 0 else None
    notional_usd = (proceeds * float(usd_multiplier)) if usd_multiplier is not None else None
    return SimulatedFill(
        qty=qty,
        avg_price=avg,
        notional_native=proceeds,
        notional_usd=notional_usd,
        worst_price=worst_price,
        levels_touched=touched,
    )
