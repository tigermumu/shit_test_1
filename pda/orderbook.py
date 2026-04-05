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


@dataclass(frozen=True)
class NRangeResult:
    is_executable: bool
    n_min: float | None
    n_max: float | None
    suggested_n: float | None
    worst_case_profit_usd: float | None
    worst_case_roi_pct: float | None
    expected_profit_usd: float | None
    expected_roi_pct: float | None
    reason: str | None


def solve_n_range(
    poly_ask_price: float,
    deribit_ask_premium_usd: float,
    budget_usd: float,
    delta_s_usd: float,
    target_profit_pct: float,
    deribit_max_contracts: float | None,
    prob_above: float | None,
) -> NRangeResult:
    p = float(poly_ask_price)
    q = float(deribit_ask_premium_usd)
    w = float(budget_usd)
    ds = float(delta_s_usd)
    t = float(target_profit_pct)

    if w <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "budget<=0")
    if p <= 0 or p >= 1:
        return NRangeResult(False, None, None, None, None, None, None, None, "poly_ask_out_of_range")
    if q <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "deribit_premium<=0")
    if ds <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "delta_s<=0")
    if t < 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "target_profit_pct<0")

    denom_min = ds - q * (1.0 + t)
    if denom_min <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "premium_too_high_for_protection")
    n_min = w * (1.0 + t) / denom_min

    denom_max = q * (1.0 + t)
    top = (1.0 / p) - 1.0 - t
    if top <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, "poly_upside_not_enough")
    n_max = w * top / denom_max

    if deribit_max_contracts is not None:
        n_max = min(n_max, float(deribit_max_contracts))

    if n_min >= n_max:
        return NRangeResult(False, n_min, n_max, None, None, None, None, None, "no_feasible_n_range")

    n_star = (w / p) / ds
    n_suggest = min(max(n_star, n_min), n_max)

    shares = w / p
    cost = w + n_suggest * q
    pnl_up = shares - w - n_suggest * q
    pnl_down = n_suggest * ds - w - n_suggest * q
    worst = pnl_up if pnl_up < pnl_down else pnl_down
    worst_roi = (worst / cost) * 100.0 if cost > 0 else None

    exp_profit = None
    exp_roi = None
    if prob_above is not None:
        pa = float(prob_above)
        if 0.0 <= pa <= 1.0:
            exp_profit = pa * pnl_up + (1.0 - pa) * pnl_down
            exp_roi = (exp_profit / cost) * 100.0 if cost > 0 else None

    return NRangeResult(
        True,
        n_min,
        n_max,
        n_suggest,
        worst,
        worst_roi,
        exp_profit,
        exp_roi,
        None,
    )
