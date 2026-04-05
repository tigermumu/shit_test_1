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
    lower_breakeven: float | None
    upper_breakeven: float | None
    max_loss_usd: float | None
    deadzone_width_usd: float | None
    plateau_profit_usd: float | None
    score: float | None
    reason: str | None


def payoff_pnl_usd(
    *,
    s: float,
    k: float,
    shares: float,
    w: float,
    n: float,
    premium_usd: float,
    fees_usd: float,
) -> float:
    if s >= k:
        return (shares - w) - (n * premium_usd) - fees_usd
    return (n * (k - s)) - (w + n * premium_usd) - fees_usd


def payoff_curve(
    *,
    k: float,
    shares: float,
    w: float,
    n: float,
    premium_usd: float,
    fees_usd: float,
    s_min: float,
    s_max: float,
    step: float,
) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    s = float(s_min)
    while s <= float(s_max) + 1e-9:
        out.append((s, payoff_pnl_usd(s=s, k=k, shares=shares, w=w, n=n, premium_usd=premium_usd, fees_usd=fees_usd)))
        s += float(step)
    return out


def payoff_metrics(
    *,
    curve: list[tuple[float, float]],
    k: float,
    current_price: float | None,
) -> dict[str, float | None]:
    if not curve:
        return {
            "max_loss_usd": None,
            "lower_breakeven": None,
            "upper_breakeven": None,
            "deadzone_width_usd": None,
            "current_pnl_usd": None,
        }

    pnl_values = [p for _, p in curve]
    min_pnl = min(pnl_values)
    max_loss_usd = -min_pnl if min_pnl < 0 else 0.0

    roots: list[float] = []
    for i in range(1, len(curve)):
        s0, p0 = curve[i - 1]
        s1, p1 = curve[i]
        if p0 == 0:
            roots.append(float(s0))
            continue
        if p0 * p1 < 0:
            t = -p0 / (p1 - p0)
            roots.append(float(s0 + t * (s1 - s0)))
    roots = sorted(set(round(x, 8) for x in roots))
    lower_bep = roots[0] if roots else None
    upper_bep = roots[-1] if len(roots) >= 2 else None

    s_to_p = {float(s): float(p) for s, p in curve}
    k_key = float(k)
    if k_key in s_to_p:
        k_pnl = s_to_p[k_key]
    else:
        k_pnl = None
        for i in range(1, len(curve)):
            s0, p0 = curve[i - 1]
            s1, p1 = curve[i]
            if s0 <= k <= s1:
                if s1 == s0:
                    k_pnl = p0
                else:
                    t = (k - s0) / (s1 - s0)
                    k_pnl = p0 + t * (p1 - p0)
                break

    deadzone_width = None
    if k_pnl is not None and k_pnl < 0:
        idx_k = min(range(len(curve)), key=lambda i: abs(curve[i][0] - k))
        lo = idx_k
        hi = idx_k
        while lo - 1 >= 0 and curve[lo - 1][1] < 0:
            lo -= 1
        while hi + 1 < len(curve) and curve[hi + 1][1] < 0:
            hi += 1
        deadzone_width = float(curve[hi][0] - curve[lo][0])
    else:
        deadzone_width = 0.0

    current_pnl = None
    if current_price is not None:
        s_cur = float(current_price)
        if s_cur <= curve[0][0]:
            current_pnl = float(curve[0][1])
        elif s_cur >= curve[-1][0]:
            current_pnl = float(curve[-1][1])
        else:
            for i in range(1, len(curve)):
                s0, p0 = curve[i - 1]
                s1, p1 = curve[i]
                if s0 <= s_cur <= s1:
                    t = (s_cur - s0) / (s1 - s0) if s1 != s0 else 0.0
                    current_pnl = float(p0 + t * (p1 - p0))
                    break

    return {
        "max_loss_usd": float(max_loss_usd),
        "lower_breakeven": float(lower_bep) if lower_bep is not None else None,
        "upper_breakeven": float(upper_bep) if upper_bep is not None else None,
        "deadzone_width_usd": float(deadzone_width) if deadzone_width is not None else None,
        "current_pnl_usd": float(current_pnl) if current_pnl is not None else None,
    }


def solve_n_range(
    poly_ask_price: float,
    deribit_ask_premium_usd: float,
    budget_usd: float,
    strike: float,
    current_price: float | None,
    delta_s_usd: float,
    target_profit_pct: float,
    deribit_max_contracts: float | None,
    prob_above: float | None,
    fees_pct_total: float = 0.0,
    max_deadzone_loss_pct: float = 0.15,
    scan_lower: float = 2000.0,
    scan_upper: float = 1000.0,
    scan_step: float = 50.0,
    deadzone_max_width_usd: float = 400.0,
) -> NRangeResult:
    p = float(poly_ask_price)
    q = float(deribit_ask_premium_usd)
    w = float(budget_usd)
    k = float(strike)
    ds = float(delta_s_usd)
    t = float(target_profit_pct)
    fee_pct = float(fees_pct_total)
    m = float(max_deadzone_loss_pct)

    if w <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "budget<=0")
    if p <= 0 or p >= 1:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "poly_ask_out_of_range")
    if q <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "deribit_premium<=0")
    if ds <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "delta_s<=0")
    if t < 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "target_profit_pct<0")
    if fee_pct < 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "fees_pct_total<0")
    if m < 0 or m >= 1:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "max_deadzone_loss_pct_out_of_range")

    denom_min = ds - q * (1.0 + t)
    if denom_min <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "premium_too_high_for_protection")
    n_min = w * (1.0 + t) / denom_min

    denom_max = q * (1.0 + t)
    top = (1.0 / p) - 1.0 - t
    if top <= 0:
        return NRangeResult(False, None, None, None, None, None, None, None, None, None, None, None, None, None, "poly_upside_not_enough")
    n_max = w * top / denom_max

    shares = w / p

    denom_plateau = q * (1.0 + fee_pct) * (1.0 - m)
    top_plateau = shares - (1.0 + fee_pct) * (1.0 - m) * w
    if denom_plateau > 0:
        n_max_plateau = top_plateau / denom_plateau
        n_max = min(n_max, n_max_plateau)

    if deribit_max_contracts is not None:
        n_max = min(n_max, float(deribit_max_contracts))

    if n_min >= n_max:
        return NRangeResult(False, n_min, n_max, None, None, None, None, None, None, None, None, None, None, None, "no_feasible_n_range")

    n_star = (w / p) / ds
    n_suggest = min(max(n_star, n_min), n_max)

    fees_usd = fee_pct * (w + n_suggest * q)
    cost = w + n_suggest * q + fees_usd
    s_min = k - float(scan_lower)
    s_max = k + float(scan_upper)
    curve = payoff_curve(k=k, shares=shares, w=w, n=n_suggest, premium_usd=q, fees_usd=fees_usd, s_min=s_min, s_max=s_max, step=float(scan_step))
    metrics = payoff_metrics(curve=curve, k=k, current_price=current_price)

    min_pnl = min(p for _, p in curve) if curve else None
    worst_roi = ((min_pnl / cost) * 100.0) if (min_pnl is not None and cost > 0) else None

    exp_profit = None
    exp_roi = None
    if prob_above is not None:
        pa = float(prob_above)
        if 0.0 <= pa <= 1.0:
            pnl_up = payoff_pnl_usd(s=k + 1.0, k=k, shares=shares, w=w, n=n_suggest, premium_usd=q, fees_usd=fees_usd)
            pnl_down = payoff_pnl_usd(s=k - float(scan_lower), k=k, shares=shares, w=w, n=n_suggest, premium_usd=q, fees_usd=fees_usd)
            exp_profit = pa * pnl_up + (1.0 - pa) * pnl_down
            exp_roi = (exp_profit / cost) * 100.0 if cost > 0 else None

    plateau_profit = payoff_pnl_usd(s=k + 1.0, k=k, shares=shares, w=w, n=n_suggest, premium_usd=q, fees_usd=fees_usd)
    score = None
    if prob_above is not None:
        pa = float(prob_above)
        max_loss = metrics.get("max_loss_usd")
        if max_loss is not None and max_loss > 0 and 0.0 <= pa <= 1.0:
            score = (float(plateau_profit) * pa) / float(max_loss)

    deadzone_width = metrics.get("deadzone_width_usd")
    in_profit = False
    if current_price is not None:
        cur_pnl = metrics.get("current_pnl_usd")
        in_profit = bool(cur_pnl is not None and cur_pnl >= 0)
    width_ok = bool(deadzone_width is not None and deadzone_width <= float(deadzone_max_width_usd))
    is_exec = bool(in_profit or width_ok)

    return NRangeResult(
        is_exec,
        n_min,
        n_max,
        n_suggest,
        float(min_pnl) if min_pnl is not None else None,
        worst_roi,
        exp_profit,
        exp_roi,
        metrics.get("lower_breakeven"),
        metrics.get("upper_breakeven"),
        metrics.get("max_loss_usd"),
        metrics.get("deadzone_width_usd"),
        float(plateau_profit),
        score,
        None,
    )
