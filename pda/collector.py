from __future__ import annotations

import argparse
import csv
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .clients import DeribitClient, PolymarketClient
from .orderbook import (
    normalize_ccxt_levels,
    normalize_polymarket_levels,
    simulate_buy_by_budget,
    simulate_sell_qty,
    summarize_levels,
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(x: Any) -> float | None:
    try:
        v = float(x)
        if v != v:
            return None
        return v
    except Exception:
        return None


@dataclass(frozen=True)
class EntryPlan:
    position_id: str
    poly_budget_usd: float
    deribit_budget_usd: float
    poly_event_slug: str
    strike: int
    date_iso: str
    currency: str
    option_type: str


@dataclass(frozen=True)
class EntryFill:
    initial_cost_usd: float
    poly_shares: float
    poly_avg_ask: float | None
    deribit_contracts: float
    deribit_avg_ask_btc: float | None
    deribit_index_price: float | None


def _write_row(path: str, row: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def _poly_slippage_metrics(asks: list[dict[str, float]], budgets: list[float]) -> dict[str, float | None]:
    asks_sorted = sorted(asks, key=lambda x: x["price"])
    best_ask = asks_sorted[0]["price"] if asks_sorted else None
    out: dict[str, float | None] = {}
    for b in budgets:
        fill = simulate_buy_by_budget(asks_sorted, budget_native=b, usd_multiplier=1.0)
        out[f"poly_buy_avg_{int(b)}"] = fill.avg_price
        out[f"poly_buy_qty_{int(b)}"] = fill.qty
        out[f"poly_buy_worst_{int(b)}"] = fill.worst_price
        if best_ask is not None and fill.avg_price is not None:
            out[f"poly_buy_slip_{int(b)}"] = fill.avg_price - float(best_ask)
        else:
            out[f"poly_buy_slip_{int(b)}"] = None
    return out


def monitor(
    plan: EntryPlan,
    csv_path: str,
    poll_s: float,
    depth: int,
) -> None:
    poly = PolymarketClient(
        gamma_api_base=os.getenv("PDA_GAMMA_API", "https://gamma-api.polymarket.com"),
        clob_api_base=os.getenv("PDA_CLOB_API", "https://clob.polymarket.com"),
        timeout_s=float(os.getenv("PDA_REQUEST_TIMEOUT_S", "15")),
    )
    deribit = DeribitClient(timeout_s=float(os.getenv("PDA_REQUEST_TIMEOUT_S", "15")))

    instrument = deribit.get_instrument(
        currency=plan.currency,
        expiry=datetime.fromisoformat(plan.date_iso).date(),
        strike=plan.strike,
        option_type=plan.option_type,
    ).instrument_name

    target = poly.resolve_from_event_slug(event_slug=plan.poly_event_slug, strike=plan.strike)
    token_id = target.yes_token_id

    poly_book = poly.fetch_order_book(token_id=token_id, depth=depth)
    deribit_book = deribit.fetch_order_book(instrument_name=instrument, limit=depth)

    poly_bids = normalize_polymarket_levels(poly_book.get("bids", []))
    poly_asks = normalize_polymarket_levels(poly_book.get("asks", []))
    poly_bids.sort(key=lambda x: x["price"], reverse=True)
    poly_asks.sort(key=lambda x: x["price"])

    deri_bids = normalize_ccxt_levels(deribit_book.get("bids", []))
    deri_asks = normalize_ccxt_levels(deribit_book.get("asks", []))
    deri_bids.sort(key=lambda x: x["price"], reverse=True)
    deri_asks.sort(key=lambda x: x["price"])

    poly_entry = simulate_buy_by_budget(poly_asks, budget_native=plan.poly_budget_usd, usd_multiplier=1.0)
    deri_index = _safe_float(deribit_book.get("index_price"))
    deri_budget_btc = (plan.deribit_budget_usd / deri_index) if deri_index else 0.0
    deri_entry = simulate_buy_by_budget(deri_asks, budget_native=deri_budget_btc, usd_multiplier=deri_index)

    entry = EntryFill(
        initial_cost_usd=float(poly_entry.notional_usd or 0.0) + float(deri_entry.notional_usd or 0.0),
        poly_shares=poly_entry.qty,
        poly_avg_ask=poly_entry.avg_price,
        deribit_contracts=deri_entry.qty,
        deribit_avg_ask_btc=deri_entry.avg_price,
        deribit_index_price=deri_index,
    )

    while True:
        ts = _utc_now_iso()

        poly_book = poly.fetch_order_book(token_id=token_id, depth=depth)
        deribit_book = deribit.fetch_order_book(instrument_name=instrument, limit=depth)

        poly_bids = normalize_polymarket_levels(poly_book.get("bids", []))
        poly_asks = normalize_polymarket_levels(poly_book.get("asks", []))
        poly_bids.sort(key=lambda x: x["price"], reverse=True)
        poly_asks.sort(key=lambda x: x["price"])
        poly_sum = summarize_levels(poly_bids, poly_asks)
        poly_spread = (poly_sum.best_ask - poly_sum.best_bid) if (poly_sum.best_ask is not None and poly_sum.best_bid is not None) else None

        deri_bids = normalize_ccxt_levels(deribit_book.get("bids", []))
        deri_asks = normalize_ccxt_levels(deribit_book.get("asks", []))
        deri_bids.sort(key=lambda x: x["price"], reverse=True)
        deri_asks.sort(key=lambda x: x["price"])
        deri_sum = summarize_levels(deri_bids, deri_asks)
        deri_spread = (deri_sum.best_ask - deri_sum.best_bid) if (deri_sum.best_ask is not None and deri_sum.best_bid is not None) else None

        deri_index = _safe_float(deribit_book.get("index_price"))
        greeks = deribit_book.get("greeks") if isinstance(deribit_book, dict) else None
        deri_delta = _safe_float(greeks.get("delta")) if isinstance(greeks, dict) else None
        deri_theta = _safe_float(greeks.get("theta")) if isinstance(greeks, dict) else None
        deribit_prob_above = (1.0 + deri_delta) if deri_delta is not None else None

        poly_exit = simulate_sell_qty(poly_bids, qty_to_sell=entry.poly_shares, usd_multiplier=1.0)
        deri_exit = simulate_sell_qty(deri_bids, qty_to_sell=entry.deribit_contracts, usd_multiplier=deri_index)
        current_value_usd = float(poly_exit.notional_usd or 0.0) + float(deri_exit.notional_usd or 0.0)
        pnl_usd = current_value_usd - entry.initial_cost_usd
        roi = (pnl_usd / entry.initial_cost_usd) if entry.initial_cost_usd > 0 else 0.0
        gap = (poly_sum.mid - deribit_prob_above) if (poly_sum.mid is not None and deribit_prob_above is not None) else None

        note = "normal"
        if roi >= 0.10:
            note = "target_reached"

        row: dict[str, Any] = {
            "ts_utc": ts,
            "position_id": plan.position_id,
            "invest_usd": round(entry.initial_cost_usd, 8),
            "value_now_usd": round(current_value_usd, 8),
            "roi_pct": round(roi * 100.0, 6),
            "gap": gap,
            "note": note,
            "poly_event_slug": plan.poly_event_slug,
            "poly_yes_token_id": token_id,
            "poly_best_bid": poly_sum.best_bid,
            "poly_best_ask": poly_sum.best_ask,
            "poly_spread": poly_spread,
            "poly_entry_avg_ask": entry.poly_avg_ask,
            "poly_entry_shares": entry.poly_shares,
            "poly_exit_avg_bid": poly_exit.avg_price,
            "poly_exit_shares_sold": poly_exit.qty,
            "deribit_instrument": instrument,
            "deribit_index_price": deri_index,
            "deribit_best_bid": deri_sum.best_bid,
            "deribit_best_ask": deri_sum.best_ask,
            "deribit_spread": deri_spread,
            "deribit_entry_avg_ask_btc": entry.deribit_avg_ask_btc,
            "deribit_entry_contracts": entry.deribit_contracts,
            "deribit_exit_avg_bid_btc": deri_exit.avg_price,
            "deribit_exit_contracts_sold": deri_exit.qty,
            "deribit_delta": deri_delta,
            "deribit_theta": deri_theta,
            "deribit_prob_above": deribit_prob_above,
        }
        row.update(_poly_slippage_metrics(poly_asks, budgets=[1500.0, 5000.0]))

        print(row)
        _write_row(csv_path, row)
        time.sleep(float(poll_s))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=os.getenv("PDA_COLLECTOR_CSV", "f:/workshop/polymarket/polymarket_deribit/data/collector.csv"))
    p.add_argument("--poll", type=float, default=float(os.getenv("PDA_COLLECTOR_POLL_S", "15")))
    p.add_argument("--depth", type=int, default=int(os.getenv("PDA_COLLECTOR_DEPTH", "15")))
    p.add_argument("--event-slug", default=os.getenv("PDA_POLY_EVENT_SLUG", "bitcoin-above-on-april-10"))
    p.add_argument("--strike", type=int, default=int(os.getenv("PDA_DEFAULT_STRIKE", "70000")))
    p.add_argument("--date", dest="date_iso", default=os.getenv("PDA_DEFAULT_DATE", "2026-04-10"))
    p.add_argument("--currency", default=os.getenv("PDA_DEFAULT_CCY", "BTC"))
    p.add_argument("--option-type", default=os.getenv("PDA_DEFAULT_OPTION_TYPE", "P"))
    p.add_argument("--poly-usd", type=float, default=float(os.getenv("PDA_POLY_BUDGET_USD", "1500")))
    p.add_argument("--deribit-usd", type=float, default=float(os.getenv("PDA_DERIBIT_BUDGET_USD", "200")))
    args = p.parse_args()

    plan = EntryPlan(
        position_id=f"{args.currency}-{args.strike}-{args.date_iso}",
        poly_budget_usd=float(args.poly_usd),
        deribit_budget_usd=float(args.deribit_usd),
        poly_event_slug=str(args.event_slug),
        strike=int(args.strike),
        date_iso=str(args.date_iso),
        currency=str(args.currency),
        option_type=str(args.option_type).upper(),
    )

    monitor(plan=plan, csv_path=str(args.csv), poll_s=float(args.poll), depth=int(args.depth))


if __name__ == "__main__":
    main()
