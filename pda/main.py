from __future__ import annotations

import csv
import os
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse

from .clients import DeribitClient, PolymarketClient
from .config import SETTINGS
from .orderbook import (
    add_usd_depth,
    normalize_ccxt_levels,
    normalize_polymarket_levels,
    simulate_buy_by_budget,
    simulate_sell_qty,
    payoff_curve,
    payoff_metrics,
    solve_w_range_for_n,
    solve_n_range,
    summarize_levels,
)

app = FastAPI(title="PoliDeribit-Arb (PDA) Orderbook Viewer", version="0.1.0")

_deribit = DeribitClient(exchange_id=SETTINGS.deribit_exchange_id, timeout_s=SETTINGS.request_timeout_s)
_poly = PolymarketClient(
    gamma_api_base=SETTINGS.gamma_api_base,
    clob_api_base=SETTINGS.clob_api_base,
    timeout_s=SETTINGS.request_timeout_s,
)

_resolved_poly_cache: dict[tuple[str, int, str], dict] = {}

_collector_lock = threading.Lock()
_collector_rows: deque[dict[str, Any]] = deque(maxlen=int(os.getenv("PDA_COLLECTOR_MAX_ROWS", "3000")))
_collector_config: dict[str, Any] | None = None
_collector_entry: dict[str, Any] | None = None
_collector_last_error: str | None = None
_shadow_trades: dict[str, dict[str, Any]] = {}
_shadow_trade_seq = 0
_delta_trader_enabled = os.getenv("PDA_DELTA_TRADER_ENABLED", "false").lower() in {"1", "true", "yes"}
_delta_position: dict[str, Any] | None = None
_delta_series: deque[dict[str, Any]] = deque(maxlen=int(os.getenv("PDA_DELTA_SERIES_MAX_POINTS", "600")))
_delta_snapshots: deque[dict[str, Any]] = deque(maxlen=int(os.getenv("PDA_DELTA_SNAPSHOT_MAX_ROWS", "20000")))


def _parse_date(d: str) -> date:
    return date.fromisoformat(d)

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


def _fmt_float(x: Any, decimals: int = 6, thousands: bool = False) -> str:
    v = _safe_float(x)
    if v is None:
        return ""
    if thousands:
        return f"{v:,.{decimals}f}"
    return f"{v:.{decimals}f}"


def _append_csv_row(path: str, row: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    exists = p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


def _collector_loop() -> None:
    global _collector_config, _collector_entry, _collector_last_error, _shadow_trades, _shadow_trade_seq, _delta_trader_enabled, _delta_position, _delta_series, _delta_snapshots

    poll_s = float(os.getenv("PDA_COLLECTOR_POLL_S", "15"))
    depth = int(os.getenv("PDA_COLLECTOR_DEPTH", str(SETTINGS.book_depth)))
    csv_path = os.getenv("PDA_COLLECTOR_CSV", str(Path.cwd() / "data" / "collector.csv"))
    delta_snapshot_csv_path = os.getenv("PDA_DELTA_SNAPSHOT_CSV", str(Path.cwd() / "data" / "delta_snapshots.csv"))
    delta_snapshot_write_csv = os.getenv("PDA_DELTA_SNAPSHOT_WRITE_CSV", "true").lower() in {"1", "true", "yes"}

    event_slug = os.getenv("PDA_POLY_EVENT_SLUG", SETTINGS.polymarket_event_slug or "bitcoin-above-on-april-10")
    strike = int(os.getenv("PDA_DEFAULT_STRIKE", str(SETTINGS.default_strike)))
    date_iso = os.getenv("PDA_DEFAULT_DATE", SETTINGS.default_date_iso)
    currency = os.getenv("PDA_DEFAULT_CCY", SETTINGS.default_currency)
    option_type = os.getenv("PDA_DEFAULT_OPTION_TYPE", SETTINGS.default_deribit_option_type)
    poly_budget_usd = float(os.getenv("PDA_POLY_BUDGET_USD", "1500"))
    deribit_budget_usd = float(os.getenv("PDA_DERIBIT_BUDGET_USD", "200"))
    max_capital_usd = float(os.getenv("PDA_MAX_CAPITAL_USD", str(min(poly_budget_usd, deribit_budget_usd))))
    delta_s_usd = float(os.getenv("PDA_DELTA_S_USD", "500"))
    target_profit_pct = float(os.getenv("PDA_TARGET_PROFIT_PCT", "0.03"))
    max_shadow_trades = int(os.getenv("PDA_MAX_SHADOW_TRADES", "50"))
    fees_pct_total = float(os.getenv("PDA_FEES_PCT_TOTAL", "0.0"))
    max_deadzone_loss_pct = float(os.getenv("PDA_MAX_DEADZONE_LOSS_PCT", "0.15"))
    deadzone_max_width_usd = float(os.getenv("PDA_DEADZONE_MAX_WIDTH_USD", "400"))
    n_step = float(os.getenv("PDA_N_STEP", "0.1"))
    n_max = float(os.getenv("PDA_N_MAX", "1.0"))
    delta_gap_threshold = float(os.getenv("PDA_DELTA_GAP_THRESHOLD", "0.01"))
    delta_profit_target_pct = float(os.getenv("PDA_DELTA_PROFIT_TARGET_PCT", "0.05"))
    delta_n_fixed = float(os.getenv("PDA_DELTA_N_FIXED", "0.1"))

    position_id = f"{currency}-{strike}-{date_iso}"

    with _collector_lock:
        _collector_config = {
            "position_id": position_id,
            "event_slug": event_slug,
            "strike": strike,
            "date_iso": date_iso,
            "currency": currency,
            "option_type": option_type,
            "poly_budget_usd": poly_budget_usd,
            "deribit_budget_usd": deribit_budget_usd,
            "max_capital_usd": max_capital_usd,
            "delta_s_usd": delta_s_usd,
            "target_profit_pct": target_profit_pct,
            "max_shadow_trades": max_shadow_trades,
            "fees_pct_total": fees_pct_total,
            "max_deadzone_loss_pct": max_deadzone_loss_pct,
            "deadzone_max_width_usd": deadzone_max_width_usd,
            "n_step": n_step,
            "n_max": n_max,
            "delta_gap_threshold": delta_gap_threshold,
            "delta_profit_target_pct": delta_profit_target_pct,
            "delta_n_fixed": delta_n_fixed,
            "poll_s": poll_s,
            "depth": depth,
            "csv_path": csv_path,
        }
        _collector_entry = None
        _collector_last_error = None
        _shadow_trades = {}
        _shadow_trade_seq = 0
        _delta_position = None
        _delta_series = deque(maxlen=_delta_series.maxlen)
        _delta_snapshots = deque(maxlen=_delta_snapshots.maxlen)

    try:
        expiry = _parse_date(date_iso)
        instrument_name = _deribit.get_instrument(currency=currency, expiry=expiry, strike=strike, option_type=option_type).instrument_name
        resolved = _poly.resolve_from_event_slug(event_slug=event_slug, strike=strike)
        token_id = resolved.yes_token_id
        ev = _poly.fetch_event_by_slug(event_slug=event_slug)
        poly_end_date_raw = ev.get("endDate") or ev.get("endDateIso") or ""
        poly_end_dt = None
        if isinstance(poly_end_date_raw, str) and poly_end_date_raw:
            try:
                poly_end_dt = datetime.fromisoformat(poly_end_date_raw.replace("Z", "+00:00"))
            except Exception:
                poly_end_dt = None
        deribit_exp_dt = datetime.fromisoformat(f"{date_iso}T08:00:00+00:00")
        drift_hours = abs((poly_end_dt - deribit_exp_dt).total_seconds()) / 3600.0 if poly_end_dt is not None else None

        with _collector_lock:
            _collector_entry = {
                "ts_utc": _utc_now_iso(),
                "position_id": position_id,
                "poly_event_slug": event_slug,
                "poly_yes_token_id": token_id,
                "deribit_instrument": instrument_name,
                "poly_end": poly_end_date_raw,
                "deribit_exp": deribit_exp_dt.isoformat(),
                "time_drift_hours": drift_hours,
            }

        while True:
            ts = _utc_now_iso()

            poly_book = _poly.fetch_order_book(token_id=token_id, depth=depth)
            deribit_book = _deribit.fetch_order_book(instrument_name=instrument_name, limit=depth)

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
            deri_spread_btc = (deri_sum.best_ask - deri_sum.best_bid) if (deri_sum.best_ask is not None and deri_sum.best_bid is not None) else None

            deri_index = _safe_float(deribit_book.get("index_price"))
            greeks = deribit_book.get("greeks") if isinstance(deribit_book, dict) else None
            deri_delta = _safe_float(greeks.get("delta")) if isinstance(greeks, dict) else None
            deri_theta = _safe_float(greeks.get("theta")) if isinstance(greeks, dict) else None
            deribit_prob_above = (1.0 + deri_delta) if deri_delta is not None else None
            deribit_delta_abs = abs(float(deri_delta)) if deri_delta is not None else None
            delta_ref_prob = None
            if deribit_delta_abs is not None:
                delta_ref_prob = max(0.0, min(1.0, 1.0 - float(deribit_delta_abs)))

            poly_l1_ask = poly_asks[0] if poly_asks else None
            poly_l1_bid = poly_bids[0] if poly_bids else None
            deri_l1_ask = deri_asks[0] if deri_asks else None
            deri_l1_bid = deri_bids[0] if deri_bids else None

            poly_ask_price = float(poly_l1_ask["price"]) if poly_l1_ask else None
            poly_ask_shares = float(poly_l1_ask["size"]) if poly_l1_ask else None
            poly_ask_depth_usd = (poly_ask_price * poly_ask_shares) if (poly_ask_price is not None and poly_ask_shares is not None) else None
            poly_bid_price = float(poly_l1_bid["price"]) if poly_l1_bid else None

            deri_ask_price_btc = float(deri_l1_ask["price"]) if deri_l1_ask else None
            deri_ask_contracts = float(deri_l1_ask["size"]) if deri_l1_ask else None
            deri_bid_price_btc = float(deri_l1_bid["price"]) if deri_l1_bid else None

            deri_ask_premium_usd = (deri_ask_price_btc * deri_index) if (deri_ask_price_btc is not None and deri_index is not None) else None
            deri_ask_depth_usd = (deri_ask_premium_usd * deri_ask_contracts) if (deri_ask_premium_usd is not None and deri_ask_contracts is not None) else None

            delta_gap = abs(float(poly_ask_price) - float(delta_ref_prob)) if (poly_ask_price is not None and delta_ref_prob is not None) else None
            delta_entry_readiness = None
            if delta_gap is not None and delta_gap_threshold > 0:
                delta_entry_readiness = max(0.0, min(1.0, 1.0 - float(delta_gap) / float(delta_gap_threshold)))

            delta_deri_cost_usd = (float(delta_n_fixed) * float(deri_ask_premium_usd)) if (deri_ask_premium_usd is not None) else None
            delta_poly_shares_needed = (float(delta_deri_cost_usd) / float(poly_ask_price)) if (delta_deri_cost_usd is not None and poly_ask_price is not None and float(poly_ask_price) > 0) else None
            delta_poly_cost_usd = (float(delta_poly_shares_needed) * float(poly_ask_price)) if (delta_poly_shares_needed is not None and poly_ask_price is not None) else None
            delta_fees_usd = (float(fees_pct_total) * (float(delta_poly_cost_usd) + float(delta_deri_cost_usd))) if (delta_poly_cost_usd is not None and delta_deri_cost_usd is not None) else None
            delta_total_entry_cost_usd = (float(delta_poly_cost_usd) + float(delta_deri_cost_usd) + float(delta_fees_usd)) if (delta_poly_cost_usd is not None and delta_deri_cost_usd is not None and delta_fees_usd is not None) else None

            delta_liquidity_ok = bool(
                delta_poly_shares_needed is not None
                and poly_ask_shares is not None
                and float(poly_ask_shares) >= float(delta_poly_shares_needed)
                and deri_ask_contracts is not None
                and float(deri_ask_contracts) >= float(delta_n_fixed)
            )
            delta_entry_executable = bool(delta_gap is not None and float(delta_gap) <= float(delta_gap_threshold) and delta_liquidity_ok)

            effective_usd = None
            if poly_ask_depth_usd is not None and deri_ask_depth_usd is not None:
                effective_usd = min(float(max_capital_usd), float(poly_ask_depth_usd), float(deri_ask_depth_usd))

            poly_w_available = min(float(max_capital_usd), float(poly_ask_depth_usd)) if poly_ask_depth_usd is not None else None
            capacity_probe = []
            if poly_ask_price is not None and deri_ask_premium_usd is not None and deri_ask_contracts is not None and poly_w_available is not None:
                max_n_here = min(float(deri_ask_contracts), float(n_max))
                step = float(n_step) if float(n_step) > 0 else 0.1
                n_val = step
                while n_val <= max_n_here + 1e-9:
                    r = solve_w_range_for_n(
                        poly_ask_price=float(poly_ask_price),
                        deribit_ask_premium_usd=float(deri_ask_premium_usd),
                        n=float(n_val),
                        delta_s_usd=float(delta_s_usd),
                        target_profit_pct=float(target_profit_pct),
                        fees_pct_total=float(fees_pct_total),
                    )
                    if r.is_feasible and r.w_min is not None and r.w_max is not None and poly_w_available >= float(r.w_min):
                        w_reco = min(float(poly_w_available), float(r.w_max))
                        shares = w_reco / float(poly_ask_price)
                        fee_usd = float(fees_pct_total) * (w_reco + float(n_val) * float(deri_ask_premium_usd))
                        curve = payoff_curve(
                            k=float(strike),
                            shares=shares,
                            w=w_reco,
                            n=float(n_val),
                            premium_usd=float(deri_ask_premium_usd),
                            fees_usd=fee_usd,
                            s_min=float(strike) * 0.9,
                            s_max=float(strike) * 1.1,
                            step=float(strike) * 0.01,
                        )
                        metrics = payoff_metrics(curve=curve, k=float(strike), current_price=deri_index)
                        plateau_profit = (shares - w_reco) - float(n_val) * float(deri_ask_premium_usd) - fee_usd
                        max_loss = metrics.get("max_loss_usd")
                        score = None
                        if deribit_prob_above is not None and max_loss is not None and float(max_loss) > 0:
                            pa = float(deribit_prob_above)
                            if 0.0 <= pa <= 1.0:
                                score = (float(plateau_profit) * pa) / float(max_loss)
                        capacity_probe.append(
                            {
                                "n": float(n_val),
                                "w_min": float(r.w_min),
                                "w_max": float(r.w_max),
                                "w_recommended": float(w_reco),
                                "plateau_profit_usd": float(plateau_profit),
                                "plateau_roi_pct": (float(plateau_profit) / (w_reco + float(n_val) * float(deri_ask_premium_usd) + fee_usd) * 100.0) if (w_reco + float(n_val) * float(deri_ask_premium_usd) + fee_usd) > 0 else None,
                                "max_loss_usd": max_loss,
                                "deadzone_width_usd": metrics.get("deadzone_width_usd"),
                                "lower_breakeven": metrics.get("lower_breakeven"),
                                "score": score,
                            }
                        )
                    n_val += step
                capacity_probe.sort(key=lambda x: (x.get("score") is not None, x.get("score") or -1e18, x.get("w_recommended") or 0.0), reverse=True)
                capacity_probe = capacity_probe[:8]

            poly_spread_pct = None
            if poly_sum.mid is not None and poly_spread is not None and poly_sum.mid > 0:
                poly_spread_pct = float(poly_spread) / float(poly_sum.mid)
            deri_spread_pct = None
            if deri_sum.mid is not None and deri_spread_btc is not None and deri_sum.mid > 0:
                deri_spread_pct = float(deri_spread_btc) / float(deri_sum.mid)

            drift_hours = abs((poly_end_dt - deribit_exp_dt).total_seconds()) / 3600.0 if poly_end_dt is not None else None

            nres = None
            if poly_ask_price is not None and deri_ask_premium_usd is not None and effective_usd is not None:
                nres = solve_n_range(
                    poly_ask_price=poly_ask_price,
                    deribit_ask_premium_usd=float(deri_ask_premium_usd),
                    budget_usd=float(effective_usd),
                    strike=float(strike),
                    current_price=deri_index,
                    delta_s_usd=float(delta_s_usd),
                    target_profit_pct=float(target_profit_pct),
                    deribit_max_contracts=deri_ask_contracts,
                    prob_above=deribit_prob_above,
                    fees_pct_total=float(fees_pct_total),
                    max_deadzone_loss_pct=float(max_deadzone_loss_pct),
                    deadzone_max_width_usd=float(deadzone_max_width_usd),
                )

            executable = bool(
                nres
                and nres.is_executable
                and effective_usd is not None
                and effective_usd >= 100.0
                and (poly_spread_pct is None or poly_spread_pct <= 0.05)
                and (deri_spread_pct is None or deri_spread_pct <= 0.05)
            )

            gap = (poly_sum.mid - deribit_prob_above) if (poly_sum.mid is not None and deribit_prob_above is not None) else None
            with _collector_lock:
                _collector_entry = {
                    **(_collector_entry or {}),
                    "ts_utc": ts,
                    "poly_best_bid": poly_sum.best_bid,
                    "poly_best_ask": poly_sum.best_ask,
                    "poly_spread_pct": poly_spread_pct,
                    "deribit_best_bid": deri_sum.best_bid,
                    "deribit_best_ask": deri_sum.best_ask,
                    "deribit_spread_pct": deri_spread_pct,
                    "deribit_index_price": deri_index,
                    "gap": gap,
                    "deribit_delta_abs": deribit_delta_abs,
                    "delta_ref_prob": delta_ref_prob,
                    "effective_usd": effective_usd,
                    "poly_w_available": poly_w_available,
                    "poly_l1_ask": poly_ask_price,
                    "poly_l1_ask_depth_usd": poly_ask_depth_usd,
                    "deribit_l1_ask_btc": deri_ask_price_btc,
                    "deribit_l1_ask_premium_usd": deri_ask_premium_usd,
                    "deribit_l1_ask_depth_usd": deri_ask_depth_usd,
                    "delta_gap": delta_gap,
                    "delta_entry_readiness": delta_entry_readiness,
                    "delta_entry_executable": delta_entry_executable,
                    "delta_poly_shares_needed": delta_poly_shares_needed,
                    "delta_poly_cost_usd": delta_poly_cost_usd,
                    "delta_deribit_cost_usd": delta_deri_cost_usd,
                    "delta_total_entry_cost_usd": delta_total_entry_cost_usd,
                    "delta_liquidity_ok": delta_liquidity_ok,
                    "n_range": None if not nres else nres.__dict__,
                    "is_executable": executable,
                    "time_drift_hours": drift_hours,
                    "shadow_trades": len(_shadow_trades),
                    "capacity_probe": capacity_probe,
                }

            delta_snapshot = {
                "ts_utc": ts,
                "poly_best_ask": poly_sum.best_ask,
                "poly_best_bid": poly_sum.best_bid,
                "delta_ref_prob": delta_ref_prob,
                "deribit_best_ask": deri_sum.best_ask,
                "deribit_best_bid": deri_sum.best_bid,
                "deribit_index_price": deri_index,
            }
            with _collector_lock:
                _delta_snapshots.append(delta_snapshot)
            if delta_snapshot_write_csv:
                _append_csv_row(delta_snapshot_csv_path, delta_snapshot)

            if _delta_trader_enabled and _delta_position is None and delta_entry_executable and delta_total_entry_cost_usd is not None:
                _delta_position = {
                    "position_id": position_id,
                    "entry_ts_utc": ts,
                    "poly_entry_ask": poly_ask_price,
                    "poly_shares": float(delta_poly_shares_needed) if delta_poly_shares_needed is not None else None,
                    "poly_entry_cost_usd": float(delta_poly_cost_usd) if delta_poly_cost_usd is not None else None,
                    "deribit_entry_ask_btc": deri_ask_price_btc,
                    "deribit_entry_premium_usd": float(deri_ask_premium_usd) if deri_ask_premium_usd is not None else None,
                    "deribit_contracts": float(delta_n_fixed),
                    "deribit_entry_cost_usd": float(delta_deri_cost_usd) if delta_deri_cost_usd is not None else None,
                    "fees_usd": float(delta_fees_usd) if delta_fees_usd is not None else None,
                    "total_entry_cost_usd": float(delta_total_entry_cost_usd),
                    "exit_triggered": False,
                    "exit_trigger_ts_utc": None,
                }
                print(f"发现平衡点，建议入场：Poly ${float(delta_poly_cost_usd or 0.0):.2f} USD + Deri {float(delta_n_fixed):.1f}张")

            if executable and nres and nres.suggested_n is not None and poly_ask_price is not None and deri_ask_premium_usd is not None and effective_usd is not None:
                _shadow_trade_seq += 1
                trade_id = f"{position_id}|{_shadow_trade_seq}"
                shares = float(effective_usd) / float(poly_ask_price)
                n = float(nres.suggested_n)
                fee_usd = float(fees_pct_total) * (float(effective_usd) + n * float(deri_ask_premium_usd))
                cost = float(effective_usd) + n * float(deri_ask_premium_usd) + fee_usd
                entry = {
                    "trade_id": trade_id,
                    "entry_ts_utc": ts,
                    "position_id": position_id,
                    "budget_usd": float(effective_usd),
                    "poly_ask_entry": float(poly_ask_price),
                    "poly_shares": shares,
                    "deribit_ask_premium_usd": float(deri_ask_premium_usd),
                    "deribit_contracts": n,
                    "initial_cost_usd": cost,
                    "fees_usd": fee_usd,
                    "strike": float(strike),
                    "n_min": nres.n_min,
                    "n_max": nres.n_max,
                    "worst_case_roi_pct": nres.worst_case_roi_pct,
                    "expected_roi_pct": nres.expected_roi_pct,
                    "lower_breakeven": nres.lower_breakeven,
                    "upper_breakeven": nres.upper_breakeven,
                    "max_loss_usd": nres.max_loss_usd,
                    "deadzone_width_usd": nres.deadzone_width_usd,
                    "plateau_profit_usd": nres.plateau_profit_usd,
                    "score": nres.score,
                }
                _shadow_trades[trade_id] = entry
                if len(_shadow_trades) > max_shadow_trades:
                    oldest = sorted(_shadow_trades.values(), key=lambda x: x.get("entry_ts_utc", ""))[0]["trade_id"]
                    _shadow_trades.pop(oldest, None)

            poly_mid = poly_sum.mid
            deri_mark_btc = _safe_float(deribit_book.get("mark_price")) if isinstance(deribit_book, dict) else None
            if deri_mark_btc is None:
                deri_mark_btc = deri_sum.mid
            deri_mark_usd = (deri_mark_btc * deri_index) if (deri_mark_btc is not None and deri_index is not None) else None
            deri_bid_usd = (deri_bid_price_btc * deri_index) if (deri_bid_price_btc is not None and deri_index is not None) else None

            delta_poly_pnl_usd = None
            delta_deri_pnl_usd = None
            delta_net_pnl_usd = None
            delta_realizable_roi_pct = None
            delta_profit_progress = None
            delta_exit_trigger = False

            if _delta_position is not None:
                ps = _safe_float(_delta_position.get("poly_shares"))
                pc = _safe_float(_delta_position.get("poly_entry_cost_usd"))
                dn = _safe_float(_delta_position.get("deribit_contracts"))
                dc = _safe_float(_delta_position.get("deribit_entry_cost_usd"))
                total_cost = _safe_float(_delta_position.get("total_entry_cost_usd"))
                if ps is not None and poly_bid_price is not None and pc is not None:
                    delta_poly_pnl_usd = ps * float(poly_bid_price) - float(pc)
                if dn is not None and deri_bid_usd is not None and dc is not None:
                    delta_deri_pnl_usd = dn * float(deri_bid_usd) - float(dc)
                if delta_poly_pnl_usd is not None and delta_deri_pnl_usd is not None:
                    delta_net_pnl_usd = float(delta_poly_pnl_usd) + float(delta_deri_pnl_usd)
                if total_cost is not None and total_cost > 0 and delta_net_pnl_usd is not None:
                    delta_realizable_roi_pct = (float(delta_net_pnl_usd) / float(total_cost)) * 100.0
                    delta_profit_progress = max(0.0, min(1.0, (float(delta_realizable_roi_pct) / (float(delta_profit_target_pct) * 100.0)) if float(delta_profit_target_pct) > 0 else 0.0))
                    if float(delta_realizable_roi_pct) >= float(delta_profit_target_pct) * 100.0:
                        delta_exit_trigger = True
                        if not bool(_delta_position.get("exit_triggered")):
                            _delta_position["exit_triggered"] = True
                            _delta_position["exit_trigger_ts_utc"] = ts
                            print("达到 5% 盈利目标，触发模拟平仓")

            _delta_series.append(
                {
                    "ts_utc": ts,
                    "poly_ask": poly_ask_price,
                    "delta_ref_prob": delta_ref_prob,
                    "delta_gap": delta_gap,
                    "poly_pnl_usd": delta_poly_pnl_usd,
                    "deribit_pnl_usd": delta_deri_pnl_usd,
                    "net_pnl_usd": delta_net_pnl_usd,
                    "realizable_roi_pct": delta_realizable_roi_pct,
                    "exit_trigger": delta_exit_trigger,
                }
            )

            with _collector_lock:
                if isinstance(_collector_entry, dict):
                    _collector_entry = {
                        **_collector_entry,
                        "delta_trader_enabled": _delta_trader_enabled,
                        "delta_position": _delta_position,
                        "delta_position_poly_pnl_usd": delta_poly_pnl_usd,
                        "delta_position_deribit_pnl_usd": delta_deri_pnl_usd,
                        "delta_position_net_pnl_usd": delta_net_pnl_usd,
                        "delta_position_realizable_roi_pct": delta_realizable_roi_pct,
                        "delta_profit_progress": delta_profit_progress,
                        "delta_exit_trigger": delta_exit_trigger,
                        "delta_series": list(_delta_series)[-240:],
                    }

            for trade in list(_shadow_trades.values()):
                shares = float(trade["poly_shares"])
                n = float(trade["deribit_contracts"])
                cost = float(trade["initial_cost_usd"])
                mark_value = None
                real_value = None
                if poly_mid is not None and deri_mark_usd is not None:
                    mark_value = shares * float(poly_mid) + n * float(deri_mark_usd)
                if poly_bid_price is not None and deri_bid_usd is not None:
                    real_value = shares * float(poly_bid_price) + n * float(deri_bid_usd)

                mark_pnl = (mark_value - cost) if mark_value is not None else None
                mark_roi = ((mark_pnl / cost) * 100.0) if (mark_pnl is not None and cost > 0) else None
                real_pnl = (real_value - cost) if real_value is not None else None
                real_roi = ((real_pnl / cost) * 100.0) if (real_pnl is not None and cost > 0) else None

                row: dict[str, Any] = {
                    "ts_utc": ts,
                    "trade_id": trade["trade_id"],
                    "entry_ts_utc": trade["entry_ts_utc"],
                    "position_id": position_id,
                    "budget_usd": trade["budget_usd"],
                    "poly_ask_entry": trade["poly_ask_entry"],
                    "deribit_ask_premium_usd": trade["deribit_ask_premium_usd"],
                    "n_min": trade.get("n_min"),
                    "n_max": trade.get("n_max"),
                    "n": trade["deribit_contracts"],
                    "initial_cost_usd": cost,
                    "mark_value_usd": mark_value,
                    "mark_pnl_usd": mark_pnl,
                    "mark_roi_pct": mark_roi,
                    "realizable_value_usd": real_value,
                    "realizable_pnl_usd": real_pnl,
                    "realizable_roi_pct": real_roi,
                    "worst_case_roi_pct": trade.get("worst_case_roi_pct"),
                    "expected_roi_pct": trade.get("expected_roi_pct"),
                    "gap": gap,
                    "poly_best_bid": poly_sum.best_bid,
                    "poly_best_ask": poly_sum.best_ask,
                    "deribit_best_bid": deri_sum.best_bid,
                    "deribit_best_ask": deri_sum.best_ask,
                    "deribit_index_price": deri_index,
                    "deribit_delta": deri_delta,
                    "deribit_theta": deri_theta,
                }
                with _collector_lock:
                    _collector_rows.append(row)
                    _collector_last_error = None
                if os.getenv("PDA_COLLECTOR_WRITE_CSV", "false").lower() in {"1", "true", "yes"}:
                    _append_csv_row(csv_path, row)

            time.sleep(poll_s)
    except Exception as e:
        with _collector_lock:
            _collector_last_error = str(e)


@app.on_event("startup")
def _startup() -> None:
    enabled = os.getenv("PDA_COLLECTOR_ENABLED", "true").lower() in {"1", "true", "yes"}
    if not enabled:
        return
    t = threading.Thread(target=_collector_loop, name="pda-collector", daemon=True)
    t.start()


@app.get("/api/v1/targets")
def get_targets(
    date_iso: str = Query(default=SETTINGS.default_date_iso),
    strike: int = Query(default=SETTINGS.default_strike),
    currency: str = Query(default=SETTINGS.default_currency),
    option_type: str = Query(default=SETTINGS.default_deribit_option_type),
    poly_event_slug: str | None = Query(default=SETTINGS.polymarket_event_slug),
):
    expiry = _parse_date(date_iso)
    instrument = _deribit.get_instrument(currency=currency, expiry=expiry, strike=strike, option_type=option_type)

    if SETTINGS.polymarket_yes_token_id:
        poly_target = {
            "question": "",
            "market_id": "",
            "yes_token_id": SETTINGS.polymarket_yes_token_id,
            "no_token_id": None,
            "via": "env",
        }
    elif poly_event_slug:
        cache_key = (poly_event_slug, int(strike), "event_slug")
        cached = _resolved_poly_cache.get(cache_key)
        if cached:
            poly_target = cached
        else:
            resolved = _poly.resolve_from_event_slug(event_slug=poly_event_slug, strike=strike)
            poly_target = {
                "question": resolved.question,
                "market_id": resolved.market_id,
                "yes_token_id": resolved.yes_token_id,
                "no_token_id": resolved.no_token_id,
                "via": "event_slug",
                "event_slug": poly_event_slug,
            }
            _resolved_poly_cache[cache_key] = poly_target
    else:
        cache_key = (expiry.isoformat(), int(strike), SETTINGS.polymarket_market_search)
        cached = _resolved_poly_cache.get(cache_key)
        if cached:
            poly_target = cached
        else:
            resolved = _poly.search_market_for_btc_above(expiry=expiry, strike=strike, query=SETTINGS.polymarket_market_search)
            poly_target = {
                "question": resolved.question,
                "market_id": resolved.market_id,
                "yes_token_id": resolved.yes_token_id,
                "no_token_id": resolved.no_token_id,
                "via": "gamma_search",
            }
            _resolved_poly_cache[cache_key] = poly_target

    return {"polymarket": poly_target, "deribit": instrument.__dict__}

@app.get("/api/v1/collector/config")
def get_collector_config() -> dict[str, Any]:
    with _collector_lock:
        return {"config": _collector_config, "entry": _collector_entry, "last_error": _collector_last_error}


@app.get("/api/v1/collector/latest")
def get_collector_latest() -> dict[str, Any]:
    with _collector_lock:
        latest = _collector_rows[-1] if _collector_rows else None
        return {"latest": latest, "count": len(_collector_rows), "last_error": _collector_last_error}


@app.get("/api/v1/collector/rows")
def get_collector_rows(limit: int = Query(default=200, ge=1, le=3000)) -> dict[str, Any]:
    with _collector_lock:
        rows = list(_collector_rows)[-int(limit) :]
        return {"rows": rows, "count": len(_collector_rows), "last_error": _collector_last_error}


@app.get("/api/v1/delta_snapshots/rows")
def get_delta_snapshot_rows(limit: int = Query(default=200, ge=1, le=20000)) -> dict[str, Any]:
    with _collector_lock:
        rows = list(_delta_snapshots)[-int(limit) :]
        return {"rows": rows, "count": len(_delta_snapshots), "last_error": _collector_last_error}


@app.get("/api/v1/delta_snapshots/export.csv")
def export_delta_snapshot_csv() -> StreamingResponse:
    with _collector_lock:
        rows = list(_delta_snapshots)

    fieldnames = [
        "ts_utc",
        "poly_best_ask",
        "poly_best_bid",
        "delta_ref_prob",
        "deribit_best_ask",
        "deribit_best_bid",
        "deribit_index_price",
    ]

    def _iter() -> Any:
        yield "\ufeff".encode("utf-8")
        import io

        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=fieldnames)
        w.writeheader()
        yield s.getvalue().encode("utf-8")
        s.seek(0)
        s.truncate(0)
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})
            yield s.getvalue().encode("utf-8")
            s.seek(0)
            s.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="pda_delta_snapshots.csv"'}
    return StreamingResponse(_iter(), media_type="text/csv; charset=utf-8", headers=headers)

@app.get("/api/v1/collector/export.csv")
def export_collector_csv() -> StreamingResponse:
    with _collector_lock:
        rows = list(_collector_rows)

    fieldnames: list[str] = []
    for r in rows:
        for k in r.keys():
            if k not in fieldnames:
                fieldnames.append(k)

    def _iter() -> Any:
        yield "\ufeff".encode("utf-8")
        buf = []
        import io

        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=fieldnames)
        w.writeheader()
        yield s.getvalue().encode("utf-8")
        s.seek(0)
        s.truncate(0)
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})
            yield s.getvalue().encode("utf-8")
            s.seek(0)
            s.truncate(0)

    headers = {"Content-Disposition": 'attachment; filename="pda_collector.csv"'}
    return StreamingResponse(_iter(), media_type="text/csv; charset=utf-8", headers=headers)


@app.get("/api/v1/payoff")
def get_payoff(
    trade_id: str | None = Query(default=None),
    n_override: float | None = Query(default=None, gt=0.0),
    w_override: float | None = Query(default=None, gt=0.0),
    poly_ask_override: float | None = Query(default=None, gt=0.0, lt=1.0),
    deribit_premium_override: float | None = Query(default=None, gt=0.0),
    delta_s_override: float | None = Query(default=None, gt=0.0),
    strike_override: float | None = Query(default=None, gt=0.0),
    scan_lower: float = Query(default=2000.0, ge=100.0, le=10000.0),
    scan_upper: float = Query(default=1000.0, ge=100.0, le=10000.0),
    scan_step: float = Query(default=50.0, ge=1.0, le=5000.0),
) -> dict[str, Any]:
    with _collector_lock:
        entry = _collector_entry.copy() if isinstance(_collector_entry, dict) else None
        cfg = _collector_config.copy() if isinstance(_collector_config, dict) else None
        trade = _shadow_trades.get(trade_id) if trade_id else None
        last_err = _collector_last_error

    if trade:
        k = float(strike_override) if strike_override is not None else float(trade.get("strike") or SETTINGS.default_strike)
        w = float(w_override) if w_override is not None else float(trade.get("budget_usd") or 0.0)
        p = float(poly_ask_override) if poly_ask_override is not None else float(trade.get("poly_ask_entry") or 0.0)
        shares = (w / p) if p > 0 else 0.0
        n = float(n_override) if n_override is not None else float(trade.get("deribit_contracts") or 0.0)
        q = float(deribit_premium_override) if deribit_premium_override is not None else float(trade.get("deribit_ask_premium_usd") or 0.0)
        if n_override is not None or w_override is not None:
            fee_pct = float((cfg or {}).get("fees_pct_total") or 0.0)
            fees_usd = fee_pct * (w + n * q)
        else:
            fees_usd = float(trade.get("fees_usd") or 0.0)
        current_price = _safe_float(entry.get("deribit_index_price")) if entry else None
    else:
        if not entry:
            if poly_ask_override is None or deribit_premium_override is None or strike_override is None:
                raise HTTPException(status_code=503, detail={"code": "collector_not_ready", "last_error": last_err})
            k = float(strike_override)
            w = float(w_override) if w_override is not None else 0.0
            p = float(poly_ask_override)
            q = float(deribit_premium_override)
            if w <= 0:
                raise HTTPException(status_code=422, detail={"code": "w_override_required_when_no_collector"})
            shares = w / p if p > 0 else 0.0
            n = float(n_override) if n_override is not None else 0.0
            if n <= 0:
                raise HTTPException(status_code=422, detail={"code": "n_override_required_when_no_collector"})
            fee_pct = float((cfg or {}).get("fees_pct_total") or 0.0)
            fees_usd = fee_pct * (w + n * q)
            current_price = None
        else:
            n_range = entry.get("n_range") if isinstance(entry.get("n_range"), dict) else None
            if (not n_range or n_range.get("suggested_n") is None) and n_override is None:
                raise HTTPException(status_code=503, detail={"code": "n_range_not_available", "last_error": last_err})
            k = float(strike_override) if strike_override is not None else float((cfg or {}).get("strike") or SETTINGS.default_strike)
            w = float(w_override) if w_override is not None else float(entry.get("effective_usd") or 0.0)
            p = float(poly_ask_override) if poly_ask_override is not None else float(entry.get("poly_l1_ask") or 0.0)
            if p <= 0:
                raise HTTPException(status_code=503, detail={"code": "poly_l1_ask_not_available", "last_error": last_err})
            shares = w / p if p > 0 else 0.0
            q = float(deribit_premium_override) if deribit_premium_override is not None else float(entry.get("deribit_l1_ask_premium_usd") or 0.0)
            if q <= 0:
                raise HTTPException(status_code=503, detail={"code": "deribit_l1_premium_not_available", "last_error": last_err})
            if n_override is not None:
                n = float(n_override)
            else:
                n = float(n_range.get("suggested_n") or 0.0)
            fee_pct = float((cfg or {}).get("fees_pct_total") or 0.0)
            fees_usd = fee_pct * (w + n * q)
            current_price = _safe_float(entry.get("deribit_index_price"))

    ds_cfg = float((cfg or {}).get("delta_s_usd") or 500.0)
    ds = float(delta_s_override) if delta_s_override is not None else ds_cfg

    curve = payoff_curve(
        k=k,
        shares=shares,
        w=w,
        n=n,
        premium_usd=q,
        fees_usd=fees_usd,
        s_min=k - float(scan_lower),
        s_max=k + float(scan_upper),
        step=float(scan_step),
    )
    metrics = payoff_metrics(curve=curve, k=k, current_price=current_price)

    cost_total = w + n * q + fees_usd
    pnl_up = (shares - w) - (n * q) - fees_usd
    pnl_at_k = -cost_total
    lower_bep = metrics.get("lower_breakeven")
    safety_gap = (k - float(lower_bep)) if lower_bep is not None else None
    risk_reward = (pnl_up / abs(pnl_at_k)) if pnl_at_k != 0 else None
    plateau_roi = (pnl_up / cost_total) * 100.0 if cost_total > 0 else None
    plateau_positive = bool(pnl_up > 0)
    gold_lhs = (1.0 / p) - 1.0 if p > 0 else None
    gold_rhs = (n * q / w) if w > 0 else None
    gold_ok = bool(gold_lhs is not None and gold_rhs is not None and gold_lhs > gold_rhs)

    w_min = None
    w_max = None
    if p > 0 and p < 1 and q > 0 and n > 0:
        fee_pct = float((cfg or {}).get("fees_pct_total") or 0.0)
        a = (1.0 / p) - 1.0
        denom = a - fee_pct
        if denom > 0:
            w_min = (n * q * (1.0 + fee_pct)) / denom
        w_max = n * (ds / (1.0 + fee_pct) - q)

    return {
        "trade_id": trade_id,
        "strike": k,
        "budget_usd": w,
        "n": n,
        "fees_usd": fees_usd,
        "curve": [{"s": s, "pnl": pnl} for s, pnl in curve],
        "metrics": metrics,
        "planner": {
            "w_poly_usd": w,
            "shares": shares,
            "poly_ask_price": p,
            "premium_usd_per_contract": q,
            "delta_s_usd": ds,
            "cost_total_usd": cost_total,
            "pnl_up_usd": pnl_up,
            "pnl_up_roi_pct": plateau_roi,
            "pnl_at_k_usd": pnl_at_k,
            "lower_breakeven": lower_bep,
            "safety_gap_usd": safety_gap,
            "risk_reward_ratio": risk_reward,
            "plateau_positive": plateau_positive,
            "golden_lhs": gold_lhs,
            "golden_rhs": gold_rhs,
            "golden_ok": gold_ok,
            "w_min_plateau": w_min,
            "w_max_safe": w_max,
        },
    }


@app.get("/api/v1/orderbooks")
def get_orderbooks(
    date_iso: str = Query(default=SETTINGS.default_date_iso),
    strike: int = Query(default=SETTINGS.default_strike),
    currency: str = Query(default=SETTINGS.default_currency),
    option_type: str = Query(default=SETTINGS.default_deribit_option_type),
    depth: int = Query(default=SETTINGS.book_depth, ge=1, le=50),
    poly_event_slug: str | None = Query(default=SETTINGS.polymarket_event_slug),
):
    try:
        targets = get_targets(date_iso=date_iso, strike=strike, currency=currency, option_type=option_type, poly_event_slug=poly_event_slug)
        poly_token_id = targets["polymarket"]["yes_token_id"]
        instrument_name = targets["deribit"]["instrument_name"]
        poly_book_raw = _poly.fetch_order_book(token_id=poly_token_id, depth=depth)
        deribit_book_raw = _deribit.fetch_order_book(instrument_name=instrument_name, limit=depth)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    poly_bids = normalize_polymarket_levels(poly_book_raw.get("bids", []))
    poly_asks = normalize_polymarket_levels(poly_book_raw.get("asks", []))
    deri_bids = normalize_ccxt_levels(deribit_book_raw.get("bids", []))
    deri_asks = normalize_ccxt_levels(deribit_book_raw.get("asks", []))
    poly_bids.sort(key=lambda x: x["price"], reverse=True)
    poly_asks.sort(key=lambda x: x["price"])
    deri_bids.sort(key=lambda x: x["price"], reverse=True)
    deri_asks.sort(key=lambda x: x["price"])

    poly_bids_usd = add_usd_depth(poly_bids, 1.0)
    poly_asks_usd = add_usd_depth(poly_asks, 1.0)
    index_price_raw = deribit_book_raw.get("index_price")
    try:
        index_price = float(index_price_raw) if index_price_raw is not None else None
    except Exception:
        index_price = None
    deri_bids_usd = add_usd_depth(deri_bids, index_price)
    deri_asks_usd = add_usd_depth(deri_asks, index_price)

    return {
        "params": {"date": date_iso, "strike": strike, "currency": currency, "option_type": option_type, "depth": depth},
        "targets": targets,
        "polymarket": {
            "summary": summarize_levels(poly_bids_usd, poly_asks_usd).__dict__,
            "book": {"bids": poly_bids_usd, "asks": poly_asks_usd},
            "depth_usd": {
                "bids_total": (poly_bids_usd[-1]["cum_notional_usd"] if poly_bids_usd else 0.0),
                "asks_total": (poly_asks_usd[-1]["cum_notional_usd"] if poly_asks_usd else 0.0),
            },
            "raw_meta": {k: v for k, v in poly_book_raw.items() if k not in {"bids", "asks"}},
        },
        "deribit": {
            "summary": summarize_levels(deri_bids_usd, deri_asks_usd).__dict__,
            "book": {"bids": deri_bids_usd, "asks": deri_asks_usd},
            "depth_usd": {
                "index_price": index_price,
                "bids_total": (deri_bids_usd[-1]["cum_notional_usd"] if deri_bids_usd and index_price is not None else None),
                "asks_total": (deri_asks_usd[-1]["cum_notional_usd"] if deri_asks_usd and index_price is not None else None),
            },
            "raw_meta": {k: v for k, v in deribit_book_raw.items() if k not in {"bids", "asks"}},
        },
    }


def _render_levels(title: str, bids: list[dict[str, float]], asks: list[dict[str, float]]) -> str:
    rows: list[str] = []
    max_len = max(len(bids), len(asks))
    for i in range(max_len):
        b = bids[i] if i < len(bids) else None
        a = asks[i] if i < len(asks) else None
        b_usd = "" if b is None else _fmt_float(b.get("notional_usd"), decimals=2, thousands=True)
        a_usd = "" if a is None else _fmt_float(a.get("notional_usd"), decimals=2, thousands=True)
        b_cum = "" if b is None else _fmt_float(b.get("cum_notional_usd"), decimals=2, thousands=True)
        a_cum = "" if a is None else _fmt_float(a.get("cum_notional_usd"), decimals=2, thousands=True)
        rows.append(
            "<tr>"
            f"<td>{'' if b is None else _fmt_float(b['price'], decimals=4)}</td>"
            f"<td>{'' if b is None else _fmt_float(b['size'], decimals=4)}</td>"
            f"<td>{b_usd}</td>"
            f"<td>{b_cum}</td>"
            f"<td>{'' if a is None else _fmt_float(a['price'], decimals=4)}</td>"
            f"<td>{'' if a is None else _fmt_float(a['size'], decimals=4)}</td>"
            f"<td>{a_usd}</td>"
            f"<td>{a_cum}</td>"
            "</tr>"
        )

    return (
        f"<h3>{title}</h3>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<thead><tr><th>Bid Px</th><th>Bid Sz</th><th>Bid USD</th><th>Bid Cum USD</th><th>Ask Px</th><th>Ask Sz</th><th>Ask USD</th><th>Ask Cum USD</th></tr></thead>"
        "<tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    html = """
<html>
  <head>
    <meta charset="utf-8"/>
    <title>PDA Dashboard</title>
    <style>
      body { font-family: Arial, sans-serif; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ddd; padding: 6px; font-size: 12px; }
      th { background: #f5f5f5; }
      .row { display: flex; gap: 16px; }
      .card { border: 1px solid #ddd; padding: 10px; flex: 1; }
      .kpi { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 8px; }
      .kpi > div { border: 1px solid #eee; padding: 8px; }
      .k { color: #666; font-size: 11px; }
      .v { font-size: 14px; font-weight: 700; }
      .bad { color: #b00020; }
      .good { color: #006400; }
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
      .btn { display: inline-block; padding: 6px 10px; border: 1px solid #ddd; background: #fafafa; text-decoration: none; color: #333; border-radius: 4px; }
      .btn:hover { background: #f0f0f0; }
      .topbar { display:flex; gap:12px; align-items:center; padding:8px 10px; border:1px solid #ddd; background:#fcfcfc; margin-top:10px; }
      .pill { display:inline-flex; align-items:center; gap:8px; padding:4px 8px; border:1px solid #eee; border-radius:999px; background:#fff; font-size:12px; }
      .dot { width:10px; height:10px; border-radius:50%; background:#999; display:inline-block; }
      .dot.live { background:#2e7d32; }
      .dot.warn { background:#f9a825; }
      .dot.bad { background:#b00020; }
      .pulse { animation: pulse 1.2s ease-in-out infinite; }
      @keyframes pulse { 0% { transform: scale(0.85); opacity: 0.6; } 50% { transform: scale(1.2); opacity: 1; } 100% { transform: scale(0.85); opacity: 0.6; } }
      .newrow { background:#e8f5ff; animation: fadebg 1.8s ease-out; }
      @keyframes fadebg { 0% { background:#e8f5ff; } 100% { background:transparent; } }
      .bar { height: 14px; border: 1px solid #ddd; background: #f2f2f2; border-radius: 999px; overflow: hidden; }
      .bar > div { height: 100%; width: 0%; background: #999; }
      .barlabel { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-top:6px; }
      .barlabel .k { margin:0; }
      .barlabel .v { font-size: 12px; font-weight: 700; }
      .tip { position:absolute; display:none; pointer-events:none; background:rgba(255,255,255,0.96); border:1px solid #ddd; border-radius:6px; padding:6px 8px; font-size:12px; box-shadow:0 2px 10px rgba(0,0,0,0.08); white-space:nowrap; }
    </style>
  </head>
  <body>
    <h2>PDA 实时监控</h2>
    <div class="topbar">
      <span class="pill"><span id="hb" class="dot"></span><span id="status">INIT</span></span>
      <span class="pill">last_update: <span id="lastUpdate">-</span></span>
      <span class="pill">next_refresh_in: <span id="nextIn">-</span>s</span>
      <span class="pill">rows: <span id="rowCount">0</span></span>
      <span class="pill">shadow_trades: <span id="shadowCount">0</span></span>
      <span class="pill">fetch_ms: <span id="fetchMs">-</span></span>
      <span class="pill">render_ms: <span id="renderMs">-</span></span>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div><b>KPI</b> <span id="err" class="bad"></span></div>
      <div class="kpi" id="kpi"></div>
      <div id="capacityProbe" style="margin-top:10px;"></div>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div><b>Delta-Matching</b> <span class="k">（Entry_Readiness + Profit_Target + 利润扩散监控）</span></div>
        <div class="mono" id="deltaStatus"></div>
      </div>
      <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top:10px;">
        <div>
          <div class="barlabel"><span class="k">Entry_Readiness</span><span id="deltaGapText" class="v mono">-</span></div>
          <div class="bar"><div id="entryBar"></div></div>
        </div>
        <div>
          <div class="barlabel"><span class="k">Profit_Target</span><span id="deltaRoiText" class="v mono">-</span></div>
          <div class="bar"><div id="profitBar"></div></div>
        </div>
      </div>
      <div id="deltaNumbers" class="mono" style="margin-top:10px;"></div>
      <div id="deltaProbWrap" style="position:relative; margin-top:10px;">
        <svg id="deltaProbSvg" viewBox="0 0 1200 220" preserveAspectRatio="none" style="width:100%; height:220px;"></svg>
        <div id="deltaProbTip" class="tip"></div>
      </div>
      <canvas id="deltaChart" width="1200" height="220" style="margin-top:10px;"></canvas>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div><b>Delta Snapshots</b> <span class="k">（15s 采样：Poly / Deribit / Δ 概率对齐）</span></div>
        <div>
          <span class="pill">count: <span id="deltaSnapCount">0</span></span>
          <a class="btn" href="/api/v1/delta_snapshots/export.csv">下载 Excel（CSV）</a>
        </div>
      </div>
      <table style="margin-top:10px;">
        <thead>
          <tr>
            <th>ts_utc</th>
            <th>poly_best_ask</th>
            <th>poly_best_bid</th>
            <th>delta_ref_prob</th>
            <th>deribit_best_ask</th>
            <th>deribit_best_bid</th>
            <th>deribit_index_price</th>
          </tr>
        </thead>
        <tbody id="deltaSnapRows"></tbody>
      </table>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div><b>Manual Sandbox</b> <span class="k">（手动输入 Poly Ask / Deribit Premium，做套利模拟）</span></div>
        <div>
          <a class="btn" href="#" id="sandboxApply">应用到图表</a>
          <a class="btn" href="#" id="sandboxClear">清空</a>
        </div>
      </div>
      <div style="display:flex; gap:12px; flex-wrap:wrap; margin-top:10px;">
        <span class="pill">Poly Ask: <input id="sbPolyAsk" type="number" step="0.01" min="0" max="1" style="width:110px; padding:4px;" /></span>
        <span class="pill">Deri Premium (USD): <input id="sbDeriPrem" type="number" step="1" min="0" style="width:140px; padding:4px;" /></span>
        <span class="pill">ΔS: <input id="sbDeltaS" type="number" step="50" min="0" style="width:110px; padding:4px;" /></span>
        <span class="pill">N: <input id="sbN" type="number" step="0.1" min="0" style="width:90px; padding:4px;" value="0.1" /></span>
        <span class="pill">W(USD): <input id="sbW" type="number" step="10" min="0" style="width:120px; padding:4px;" /></span>
      </div>
      <div id="sandboxOut" class="mono" style="margin-top:10px;"></div>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div><b>Payoff Chart</b> <span class="k">selected:</span> <span id="payoffSelected" class="mono">CURRENT</span></div>
        <div>
          <span class="pill">Manual N: <input id="manualN" type="number" step="0.1" min="0" style="width:110px; padding:4px;" /></span>
          <span class="pill">Manual W(USD): <input id="manualW" type="number" step="10" min="0" style="width:120px; padding:4px;" /></span>
          <a class="btn" href="#" id="applyN">应用</a>
          <a class="btn" href="#" id="payoffReset">回到当前盘口</a>
        </div>
      </div>
      <div id="payoffMeta" class="mono" style="margin-top:8px;"></div>
      <canvas id="payoff" width="1200" height="260"></canvas>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div><b>Shadow ROI（Mark）曲线</b></div>
        <div>
          <a class="btn" href="/api/v1/collector/export.csv">下载 Excel（CSV）</a>
        </div>
      </div>
      <canvas id="chart" width="1200" height="220"></canvas>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div><b>最近记录</b></div>
      <table>
        <thead>
          <tr>
            <th>ts_utc</th>
            <th>trade_id</th>
            <th>entry_ts_utc</th>
            <th>mark_roi_pct</th>
            <th>realizable_roi_pct</th>
            <th>mark_pnl_usd</th>
            <th>realizable_pnl_usd</th>
            <th>worst_case_roi_pct</th>
            <th>expected_roi_pct</th>
            <th>gap</th>
            <th>n</th>
            <th>n_min</th>
            <th>n_max</th>
            <th>poly_best_bid</th>
            <th>poly_best_ask</th>
            <th>deribit_index</th>
            <th>deri_delta</th>
            <th>deri_theta</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <div class="card" style="margin-top: 12px;">
      <details>
        <summary><b>Config（展开查看）</b></summary>
        <pre id="cfg" class="mono"></pre>
      </details>
    </div>
    <script>
      const nfUsd = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const nfNum = new Intl.NumberFormat('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 6 });
      const nfPx = new Intl.NumberFormat('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 });
      const nfPct = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      function fmt(x) {
        if (x === null || x === undefined) return '';
        if (typeof x === 'number') return nfNum.format(x);
        return String(x);
      }
      function fmtUsd(x) {
        if (x === null || x === undefined) return '';
        const n = Number(x);
        if (!Number.isFinite(n)) return '';
        return nfUsd.format(n);
      }
      function fmtPx(x) {
        if (x === null || x === undefined) return '';
        const n = Number(x);
        if (!Number.isFinite(n)) return '';
        return nfPx.format(n);
      }
      function fmtPct(x) {
        if (x === null || x === undefined) return '';
        const n = Number(x);
        if (!Number.isFinite(n)) return '';
        return nfPct.format(n) + '%';
      }
      function setKpi(scan, latestRow) {
        const el = document.getElementById('kpi');
        el.innerHTML = '';
        if (!scan) return;
        const nRange = scan.n_range || {};
        const items = [
          ['position_id', scan.position_id],
          ['event_slug', scan.poly_event_slug],
          ['is_executable', String(scan.is_executable)],
          ['effective_usd', fmtUsd(scan.effective_usd)],
          ['poly_l1_ask', fmtPx(scan.poly_l1_ask)],
          ['poly_l1_ask_depth_usd', fmtUsd(scan.poly_l1_ask_depth_usd)],
          ['deribit_l1_ask_premium_usd', fmtUsd(scan.deribit_l1_ask_premium_usd)],
          ['deribit_l1_ask_depth_usd', fmtUsd(scan.deribit_l1_ask_depth_usd)],
          ['N_range', nRange && nRange.n_min !== undefined ? (`[${fmt(nRange.n_min)}, ${fmt(nRange.n_max)}]`) : ''],
          ['suggested_n', nRange && nRange.suggested_n !== undefined ? fmt(nRange.suggested_n) : ''],
          ['worst_case_roi_pct', nRange && nRange.worst_case_roi_pct !== undefined ? fmtPct(nRange.worst_case_roi_pct) : ''],
          ['expected_roi_pct', nRange && nRange.expected_roi_pct !== undefined ? fmtPct(nRange.expected_roi_pct) : ''],
          ['deadzone_width', nRange && nRange.deadzone_width_usd !== undefined ? fmtUsd(nRange.deadzone_width_usd) : ''],
          ['max_loss_usd', nRange && nRange.max_loss_usd !== undefined ? fmtUsd(nRange.max_loss_usd) : ''],
          ['lower_bep', nRange && nRange.lower_breakeven !== undefined ? fmt(nRange.lower_breakeven) : ''],
          ['upper_bep', nRange && nRange.upper_breakeven !== undefined ? fmt(nRange.upper_breakeven) : ''],
          ['pda_score', nRange && nRange.score !== undefined ? fmt(nRange.score) : ''],
          ['gap', fmt(scan.gap)],
          ['shadow_trades', String(scan.shadow_trades)]
        ];
        if (latestRow) {
          items.push(['last_trade_id', latestRow.trade_id]);
          items.push(['last_mark_roi', fmtPct(latestRow.mark_roi_pct)]);
          items.push(['last_real_roi', fmtPct(latestRow.realizable_roi_pct)]);
        }
        items.forEach(([k,v])=>{
          const d = document.createElement('div');
          const kk = document.createElement('div');
          kk.className = 'k';
          kk.textContent = k;
          const vv = document.createElement('div');
          vv.className = 'v';
          vv.textContent = v;
          d.appendChild(kk);
          d.appendChild(vv);
          el.appendChild(d);
        });
      }

      function renderCapacityProbe(scan) {
        const el = document.getElementById('capacityProbe');
        if (!el) return;
        const rows = scan && scan.capacity_probe ? scan.capacity_probe : [];
        if (!rows || !rows.length) {
          el.innerHTML = '';
          return;
        }
        const head = `
          <div style="display:flex; justify-content:space-between; align-items:center;">
            <div><b>Optimal Size Suggester</b> <span class="k">（Deribit N=0.1 步长反推 Poly W）</span></div>
          </div>
        `;
        const tableHead = `
          <table style="margin-top:8px;">
            <thead>
              <tr>
                <th>N</th>
                <th>W_min</th>
                <th>W_max</th>
                <th>W_reco</th>
                <th>ROI_up</th>
                <th>MaxLoss</th>
                <th>DeadZone</th>
                <th>Score</th>
              </tr>
            </thead>
            <tbody>
        `;
        const trs = rows.map(r => {
          const score = r.score === null || r.score === undefined ? '' : nfNum.format(Number(r.score));
          const roi = r.plateau_roi_pct === null || r.plateau_roi_pct === undefined ? '' : fmtPct(r.plateau_roi_pct);
          const cls = (Number(r.score) >= 1.5) ? 'good' : '';
          return `
            <tr style="cursor:pointer;" data-n="${r.n}" data-w="${r.w_recommended}">
              <td>${fmt(r.n)}</td>
              <td>${fmtUsd(r.w_min)}</td>
              <td>${fmtUsd(r.w_max)}</td>
              <td><span class="${cls}">${fmtUsd(r.w_recommended)}</span></td>
              <td>${roi}</td>
              <td>${fmtUsd(r.max_loss_usd)}</td>
              <td>${fmtUsd(r.deadzone_width_usd)}</td>
              <td><span class="${cls}">${score}</span></td>
            </tr>
          `;
        }).join('');
        el.innerHTML = head + tableHead + trs + '</tbody></table>';
        el.querySelectorAll('tr[data-n]').forEach(tr => {
          tr.onclick = () => {
            const n = tr.getAttribute('data-n');
            if (n) document.getElementById('manualN').value = n;
            const w = tr.getAttribute('data-w');
            if (w) document.getElementById('manualW').value = w;
            updatePayoff(window.__payoff.selected);
          };
        });
      }
      function drawChart(points) {
        const c = document.getElementById('chart');
        const ctx = c.getContext('2d');
        ctx.clearRect(0,0,c.width,c.height);
        if (!points.length) return;
        const xs = points.map((_,i)=>i);
        const ys = points.map(p=>p);
        const ymin = Math.min(...ys);
        const ymax = Math.max(...ys);
        const pad = 10;
        const w = c.width - pad*2;
        const h = c.height - pad*2;
        const ySpan = (ymax - ymin) || 1;
        ctx.strokeStyle = '#333';
        ctx.strokeRect(pad,pad,w,h);
        const y0 = pad + h - (h * ((0 - ymin)/ySpan));
        ctx.beginPath();
        ctx.moveTo(pad, y0);
        ctx.lineTo(pad + w, y0);
        ctx.strokeStyle = '#ddd';
        ctx.lineWidth = 1;
        ctx.stroke();
        const y10 = pad + h - (h * ((10 - ymin)/ySpan));
        ctx.beginPath();
        ctx.moveTo(pad, y10);
        ctx.lineTo(pad + w, y10);
        ctx.strokeStyle = '#ffcc00';
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.beginPath();
        ys.forEach((y,i)=>{
          const px = pad + (w * (i/(ys.length-1 || 1)));
          const py = pad + h - (h * ((y - ymin)/ySpan));
          if (i===0) ctx.moveTo(px,py); else ctx.lineTo(px,py);
        });
        ctx.strokeStyle = '#0066cc';
        ctx.lineWidth = 2;
        ctx.stroke();
        ctx.fillStyle = '#333';
        ctx.fillText('mark_roi_pct', pad+4, pad+12);
        ctx.fillText(fmt(ymin), pad+4, pad+h-2);
        ctx.fillText(fmt(ymax), pad+4, pad+24);
        ctx.fillText('0%', pad + w - 18, y0 - 2);
        ctx.fillText('10%', pad + w - 24, y10 - 2);
      }

      function drawDeltaChart(series) {
        const c = document.getElementById('deltaChart');
        if (!c) return;
        const ctx = c.getContext('2d');
        ctx.clearRect(0, 0, c.width, c.height);
        const rows = (series || []).filter(p => p && (p.net_pnl_usd !== null && p.net_pnl_usd !== undefined));
        if (!rows.length) return;
        const pad = 10;
        const w = c.width - pad * 2;
        const h = c.height - pad * 2;
        const poly = rows.map(r => Number(r.poly_pnl_usd || 0));
        const deri = rows.map(r => Number(r.deribit_pnl_usd || 0));
        const net = rows.map(r => Number(r.net_pnl_usd || 0));
        const all = poly.concat(deri).concat(net).filter(n => Number.isFinite(n));
        if (!all.length) return;
        const ymin = Math.min(...all);
        const ymax = Math.max(...all);
        const ySpan = (ymax - ymin) || 1;
        ctx.strokeStyle = '#333';
        ctx.strokeRect(pad, pad, w, h);
        const y0 = pad + h - (h * ((0 - ymin) / ySpan));
        ctx.beginPath();
        ctx.moveTo(pad, y0);
        ctx.lineTo(pad + w, y0);
        ctx.strokeStyle = '#ddd';
        ctx.lineWidth = 1;
        ctx.stroke();
        function yToPx(y) { return pad + h - (h * ((y - ymin) / ySpan)); }
        function xToPx(i) { return pad + (w * (i / (rows.length - 1 || 1))); }
        function strokeLine(arr, color, lw) {
          ctx.beginPath();
          arr.forEach((y, i) => {
            const px = xToPx(i);
            const py = yToPx(y);
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
          });
          ctx.strokeStyle = color;
          ctx.lineWidth = lw;
          ctx.stroke();
        }
        strokeLine(poly, '#0066cc', 1.5);
        strokeLine(deri, '#ff6f00', 1.5);
        strokeLine(net, '#2e7d32', 2.5);
        ctx.fillStyle = '#333';
        ctx.fillText('Poly PnL', pad + 4, pad + 12);
        ctx.fillStyle = '#ff6f00';
        ctx.fillText('Deri PnL', pad + 80, pad + 12);
        ctx.fillStyle = '#2e7d32';
        ctx.fillText('Net PnL', pad + 150, pad + 12);
        ctx.fillStyle = '#333';
        ctx.fillText(fmtUsd(ymin), pad + 4, pad + h - 2);
        ctx.fillText(fmtUsd(ymax), pad + 4, pad + 24);
      }

      function drawDeltaProbSvg(series) {
        const svg = document.getElementById('deltaProbSvg');
        if (!svg) return;
        const tip = document.getElementById('deltaProbTip');
        while (svg.firstChild) svg.removeChild(svg.firstChild);

        const data = (series || []).slice(-240).filter(p => p && (p.poly_ask !== null && p.poly_ask !== undefined) && (p.delta_ref_prob !== null && p.delta_ref_prob !== undefined));
        if (!data.length) return;

        const poly = data.map(r => Number(r.poly_ask)).map(v => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : NaN));
        const refp = data.map(r => Number(r.delta_ref_prob)).map(v => (Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : NaN));
        const all = poly.concat(refp).filter(v => Number.isFinite(v));
        if (!all.length) return;

        const minV = Math.min(...all);
        const maxV = Math.max(...all);
        let span = maxV - minV;
        if (!Number.isFinite(span) || span <= 0) span = 0.02;
        const padV = Math.max(0.005, span * 0.35);
        let ymin = Math.max(0.0, minV - padV);
        let ymax = Math.min(1.0, maxV + padV);
        if (ymax - ymin < 0.02) {
          const mid = (ymin + ymax) / 2;
          ymin = Math.max(0.0, mid - 0.01);
          ymax = Math.min(1.0, mid + 0.01);
        }
        const ySpan = (ymax - ymin) || 1;

        const W = 1200, H = 220;
        const padL = 70, padR = 18, padT = 18, padB = 26;
        const iw = W - padL - padR;
        const ih = H - padT - padB;

        function xOf(i) { return padL + iw * (i / (data.length - 1 || 1)); }
        function yOf(v) { return padT + ih - ih * ((v - ymin) / ySpan); }

        function el(name, attrs) {
          const n = document.createElementNS('http://www.w3.org/2000/svg', name);
          if (attrs) Object.keys(attrs).forEach(k => n.setAttribute(k, String(attrs[k])));
          return n;
        }

        svg.appendChild(el('rect', { x: 0, y: 0, width: W, height: H, fill: '#fff' }));
        svg.appendChild(el('rect', { x: padL, y: padT, width: iw, height: ih, fill: 'none', stroke: '#333', 'stroke-width': 1 }));

        const yTicks = 5;
        for (let i = 0; i <= yTicks; i++) {
          const v = ymin + (i * (ySpan / yTicks));
          const y = yOf(v);
          svg.appendChild(el('line', { x1: padL, y1: y, x2: padL + iw, y2: y, stroke: '#eee', 'stroke-width': 1 }));
          const t = el('text', { x: 6, y: y + 4, fill: '#666', 'font-size': 11, 'font-family': 'Arial' });
          t.textContent = v.toFixed(4);
          svg.appendChild(t);
        }

        const cPoly = '#0066cc';
        const cRef = '#f9a825';

        function pathFor(arr) {
          let d = '';
          let started = false;
          arr.forEach((v, i) => {
            if (!Number.isFinite(v)) return;
            const x = xOf(i);
            const y = yOf(v);
            if (!started) {
              d += `M${x.toFixed(2)},${y.toFixed(2)}`;
              started = true;
            } else {
              d += ` L${x.toFixed(2)},${y.toFixed(2)}`;
            }
          });
          return d;
        }

        svg.appendChild(el('path', { d: pathFor(poly), fill: 'none', stroke: cPoly, 'stroke-width': 2.5 }));
        svg.appendChild(el('path', { d: pathFor(refp), fill: 'none', stroke: cRef, 'stroke-width': 2.5 }));

        const title = el('text', { x: padL + 6, y: 14, fill: '#000', 'font-size': 12, 'font-family': 'Arial' });
        title.textContent = 'Prob Monitor';
        svg.appendChild(title);

        function lastFinite(arr) {
          for (let i = arr.length - 1; i >= 0; i--) if (Number.isFinite(arr[i])) return { i, v: arr[i] };
          return null;
        }
        const lp = lastFinite(poly);
        const lr = lastFinite(refp);

        const leg1 = el('text', { x: padL + 110, y: 14, fill: cPoly, 'font-size': 12, 'font-family': 'Arial' });
        leg1.textContent = `poly_ask ${lp ? lp.v.toFixed(4) : ''}`;
        svg.appendChild(leg1);
        const leg2 = el('text', { x: padL + 250, y: 14, fill: cRef, 'font-size': 12, 'font-family': 'Arial' });
        leg2.textContent = `(1-|Δ|) ${lr ? lr.v.toFixed(4) : ''}`;
        svg.appendChild(leg2);

        function dot(pt, color) {
          if (!pt) return;
          svg.appendChild(el('circle', { cx: xOf(pt.i), cy: yOf(pt.v), r: 4, fill: color, stroke: '#fff', 'stroke-width': 1 }));
        }
        dot(lp, cPoly);
        dot(lr, cRef);

        const cross = el('line', { x1: padL, y1: padT, x2: padL, y2: padT + ih, stroke: '#bbb', 'stroke-width': 1, 'stroke-dasharray': '4 3', visibility: 'hidden' });
        const p1 = el('circle', { cx: 0, cy: 0, r: 4, fill: cPoly, stroke: '#fff', 'stroke-width': 1, visibility: 'hidden' });
        const p2 = el('circle', { cx: 0, cy: 0, r: 4, fill: cRef, stroke: '#fff', 'stroke-width': 1, visibility: 'hidden' });
        svg.appendChild(cross);
        svg.appendChild(p1);
        svg.appendChild(p2);

        function showTip(show) {
          if (!tip) return;
          tip.style.display = show ? 'block' : 'none';
        }

        svg.onmouseleave = () => {
          cross.setAttribute('visibility', 'hidden');
          p1.setAttribute('visibility', 'hidden');
          p2.setAttribute('visibility', 'hidden');
          showTip(false);
        };

        svg.onmousemove = (e) => {
          const rect = svg.getBoundingClientRect();
          const x = (e.clientX - rect.left) * (W / rect.width);
          const clamped = Math.max(padL, Math.min(padL + iw, x));
          const idx = Math.max(0, Math.min(data.length - 1, Math.round(((clamped - padL) / iw) * (data.length - 1))));

          cross.setAttribute('x1', String(xOf(idx)));
          cross.setAttribute('x2', String(xOf(idx)));
          cross.setAttribute('visibility', 'visible');

          const v1 = poly[idx];
          const v2 = refp[idx];
          if (Number.isFinite(v1)) {
            p1.setAttribute('cx', String(xOf(idx)));
            p1.setAttribute('cy', String(yOf(v1)));
            p1.setAttribute('visibility', 'visible');
          } else {
            p1.setAttribute('visibility', 'hidden');
          }
          if (Number.isFinite(v2)) {
            p2.setAttribute('cx', String(xOf(idx)));
            p2.setAttribute('cy', String(yOf(v2)));
            p2.setAttribute('visibility', 'visible');
          } else {
            p2.setAttribute('visibility', 'hidden');
          }

          if (tip) {
            const ts = data[idx].ts_utc ? String(data[idx].ts_utc) : '';
            tip.textContent = `${ts}  poly_ask=${Number.isFinite(v1) ? v1.toFixed(4) : '-'}  (1-|Δ|)=${Number.isFinite(v2) ? v2.toFixed(4) : '-'}  gap=${(data[idx].delta_gap !== null && data[idx].delta_gap !== undefined) ? Number(data[idx].delta_gap).toFixed(5) : '-'}`;
            tip.style.left = Math.min(rect.width - 10, Math.max(10, (e.clientX - rect.left) + 12)) + 'px';
            tip.style.top = Math.min(rect.height - 10, Math.max(10, (e.clientY - rect.top) - 18)) + 'px';
            showTip(true);
          }
        };
      }

      function renderDeltaPanel(scan, cfg) {
        const status = document.getElementById('deltaStatus');
        const gapText = document.getElementById('deltaGapText');
        const roiText = document.getElementById('deltaRoiText');
        const entryBar = document.getElementById('entryBar');
        const profitBar = document.getElementById('profitBar');
        const nums = document.getElementById('deltaNumbers');
        if (!scan || !cfg) return;
        const th = Number(cfg.delta_gap_threshold || 0.01);
        const targetPct = Number(cfg.delta_profit_target_pct || 0.05) * 100.0;
        const gap = scan.delta_gap;
        const refProb = scan.delta_ref_prob;
        const polyAsk = scan.poly_l1_ask;
        const readiness = Number(scan.delta_entry_readiness);
        const roi = scan.delta_position_realizable_roi_pct;
        const pProg = Number(scan.delta_profit_progress);
        const enabled = Boolean(scan.delta_trader_enabled);
        status.textContent = enabled ? 'ENABLED' : 'DISABLED';
        if (gapText) {
          if (gap === null || gap === undefined) gapText.textContent = '-';
          else gapText.textContent = (`gap=${fmt(gap)} th=${fmt(th)}  poly_ask=${fmt(polyAsk)}  (1-|Δ|)=${fmt(refProb)}`);
        }
        if (roiText) roiText.textContent = (roi === null || roi === undefined) ? '-' : (`roi=${fmtPct(roi)} target=${fmtPct(targetPct)}`);
        if (entryBar) {
          const w = Number.isFinite(readiness) ? Math.max(0, Math.min(1, readiness)) * 100 : 0;
          entryBar.style.width = w.toFixed(1) + '%';
          entryBar.style.background = (w >= 95) ? '#2e7d32' : (w >= 70 ? '#f9a825' : '#999');
        }
        if (profitBar) {
          const w = Number.isFinite(pProg) ? Math.max(0, Math.min(1, pProg)) * 100 : 0;
          profitBar.style.width = w.toFixed(1) + '%';
          profitBar.style.background = (w >= 100) ? '#2e7d32' : '#0066cc';
        }
        if (nums) {
          const polyPnl = scan.delta_position_poly_pnl_usd;
          const deriPnl = scan.delta_position_deribit_pnl_usd;
          const netPnl = scan.delta_position_net_pnl_usd;
          const exec = Boolean(scan.delta_entry_executable);
          const liqOk = Boolean(scan.delta_liquidity_ok);
          const shares = scan.delta_poly_shares_needed;
          const cost = scan.delta_total_entry_cost_usd;
          const exitTrig = Boolean(scan.delta_exit_trigger);
          const lines = [
            `Entry executable: ${exec}  Liquidity ok: ${liqOk}`,
            `Suggested entry: Poly shares=${shares !== null && shares !== undefined ? fmt(shares) : '-'}  Total entry cost=${cost !== null && cost !== undefined ? fmtUsd(cost) : '-'}`,
            `PnL: Poly=${polyPnl !== null && polyPnl !== undefined ? fmtUsd(polyPnl) : '-'}  Deri=${deriPnl !== null && deriPnl !== undefined ? fmtUsd(deriPnl) : '-'}  Net=${netPnl !== null && netPnl !== undefined ? fmtUsd(netPnl) : '-'}`,
            exitTrig ? '达到 5% 盈利目标，触发模拟平仓' : ''
          ].filter(Boolean);
          nums.textContent = lines.join('\\n');
        }
        const series = scan.delta_series || [];
        drawDeltaProbSvg(series);
        drawDeltaChart(series);
      }

      window.__payoff = window.__payoff || { selected: null, lastTradeId: null };

      function drawPayoff(points, strike) {
        const c = document.getElementById('payoff');
        const ctx = c.getContext('2d');
        ctx.clearRect(0, 0, c.width, c.height);
        if (!points || !points.length) return;

        const pad = 24;
        const w = c.width - pad * 2;
        const h = c.height - pad * 2;

        const xs = points.map(p => p.x);
        const ys = points.map(p => p.y);
        let xmin = Math.min(...xs);
        let xmax = Math.max(...xs);
        let ymin = Math.min(...ys);
        let ymax = Math.max(...ys);

        if (typeof strike === 'number' && Number.isFinite(strike) && strike > 0) {
          xmin = strike * 0.9;
          xmax = strike * 1.1;
        }

        const yPad = Math.max(10, (ymax - ymin) * 0.05);
        ymin -= yPad;
        ymax += yPad;
        if (xmax === xmin) xmax = xmin + 1;
        if (ymax === ymin) ymax = ymin + 1;

        function xToPx(x) { return pad + (w * ((x - xmin) / (xmax - xmin))); }
        function yToPx(y) { return pad + h - (h * ((y - ymin) / (ymax - ymin))); }

        ctx.strokeStyle = '#333';
        ctx.lineWidth = 1;
        ctx.strokeRect(pad, pad, w, h);

        const y0 = yToPx(0);
        ctx.beginPath();
        ctx.moveTo(pad, y0);
        ctx.lineTo(pad + w, y0);
        ctx.strokeStyle = '#b00020';
        ctx.lineWidth = 1;
        ctx.stroke();

        if (typeof strike === 'number' && Number.isFinite(strike)) {
          const xk = xToPx(strike);
          ctx.save();
          ctx.setLineDash([6, 4]);
          ctx.beginPath();
          ctx.moveTo(xk, pad);
          ctx.lineTo(xk, pad + h);
          ctx.strokeStyle = '#666';
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.restore();
          ctx.fillStyle = '#666';
          ctx.fillText('K', xk + 4, pad + 12);
        }

        const xAxisY = pad + h;
        ctx.beginPath();
        ctx.moveTo(pad, xAxisY);
        ctx.lineTo(pad + w, xAxisY);
        ctx.strokeStyle = '#333';
        ctx.lineWidth = 1;
        ctx.stroke();

        const intFmt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });
        const leftTicks = 10;
        const rightTicks = 10;
        const stepPct = 0.01;
        const xStep = (typeof strike === 'number' && Number.isFinite(strike) && strike > 0) ? (strike * stepPct) : ((xmax - xmin) / (leftTicks + rightTicks));

        ctx.fillStyle = '#333';
        ctx.font = '10px Arial';
        ctx.textBaseline = 'top';

        function drawTick(x, label) {
          const px = xToPx(x);
          ctx.beginPath();
          ctx.moveTo(px, xAxisY);
          ctx.lineTo(px, xAxisY + 6);
          ctx.strokeStyle = '#333';
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.save();
          ctx.translate(px, xAxisY + 8);
          ctx.rotate(-Math.PI / 4);
          ctx.textAlign = 'left';
          ctx.fillText(label, 0, 0);
          ctx.restore();
        }

        if (typeof strike === 'number' && Number.isFinite(strike)) {
          for (let i = leftTicks; i >= 1; i--) {
            const x = strike - i * xStep;
            if (x < xmin - 1e-6) continue;
            drawTick(x, intFmt.format(Math.round(x)));
          }
          drawTick(strike, intFmt.format(Math.round(strike)));
          for (let i = 1; i <= rightTicks; i++) {
            const x = strike + i * xStep;
            if (x > xmax + 1e-6) continue;
            drawTick(x, intFmt.format(Math.round(x)));
          }
        } else {
          for (let i = 0; i <= leftTicks + rightTicks; i++) {
            const x = xmin + i * ((xmax - xmin) / (leftTicks + rightTicks));
            drawTick(x, intFmt.format(Math.round(x)));
          }
        }

        ctx.beginPath();
        points.forEach((p, i) => {
          const px = xToPx(p.x);
          const py = yToPx(p.y);
          if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        });
        ctx.strokeStyle = '#0066cc';
        ctx.lineWidth = 2;
        ctx.stroke();

        ctx.fillStyle = '#333';
        ctx.fillText(`PnL (USD)`, pad + 4, pad + 14);
        ctx.textAlign = 'left';
        ctx.fillText(`x: BTC expiry price`, pad + 4, pad + h + 18);
        ctx.fillText(fmtUsd(ymax), pad + 4, pad + 26);
        ctx.fillText(fmtUsd(ymin), pad + 4, pad + h - 10);
      }

      function renderPayoff(data) {
        const meta = document.getElementById('payoffMeta');
        const selected = document.getElementById('payoffSelected');
        const tradeId = data && data.trade_id ? data.trade_id : null;
        selected.textContent = tradeId || 'CURRENT';
        const m = (data && data.metrics) || {};
        const p = (data && data.planner) || {};
        function vOrDash(v, formatter) {
          if (v === null || v === undefined) return '-';
          const n = Number(v);
          if (Number.isFinite(n)) return formatter ? formatter(n) : String(n);
          const s = String(v);
          return s.length ? s : '-';
        }
        function clsPosNeg(v) {
          const n = Number(v);
          if (!Number.isFinite(n)) return '';
          if (n > 0) return 'good';
          if (n < 0) return 'bad';
          return '';
        }
        const strike = Number(data && data.strike);
        const nVal = Number(data && data.n);
        const budget = Number(data && data.budget_usd);
        const fees = Number(data && data.fees_usd);
        const costTotal = Number(p.cost_total_usd);
        const pnlUp = Number(p.pnl_up_usd);
        const pnlUpRoi = Number(p.pnl_up_roi_pct);
        const pnlAtK = Number(p.pnl_at_k_usd);
        const maxLoss = Number(m.max_loss_usd);
        const dzWidth = Number(m.deadzone_width_usd);

        const goldenOk = Boolean(p.golden_ok);
        const goldenLhs = p.golden_lhs;
        const goldenRhs = p.golden_rhs;

        meta.innerHTML = `
          <div style="display:grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px;">
            <div style="border:1px solid #eee; padding:10px;">
              <div class="k">输入</div>
              <div><span class="k">K</span> <span class="v">${vOrDash(strike, x => nfUsd.format(Math.round(x)))}</span></div>
              <div><span class="k">W (USD)</span> <span class="v">${vOrDash(budget, fmtUsd)}</span></div>
              <div><span class="k">N</span> <span class="v">${vOrDash(nVal, fmt)}</span></div>
              <div><span class="k">Fees (USD)</span> <span class="v">${vOrDash(fees, fmtUsd)}</span></div>
            </div>
            <div style="border:1px solid #eee; padding:10px;">
              <div class="k">右侧平原（S ≥ K）</div>
              <div><span class="k">Cost Total</span> <span class="v">${vOrDash(costTotal, fmtUsd)}</span></div>
              <div><span class="k">PnL_up</span> <span class="v ${clsPosNeg(pnlUp)}">${vOrDash(pnlUp, fmtUsd)}</span></div>
              <div><span class="k">ROI_up</span> <span class="v ${clsPosNeg(pnlUpRoi)}">${vOrDash(pnlUpRoi, fmtPct)}</span></div>
            </div>
            <div style="border:1px solid #eee; padding:10px;">
              <div class="k">死区与保护</div>
              <div><span class="k">PnL@K</span> <span class="v bad">${vOrDash(pnlAtK, fmtUsd)}</span></div>
              <div><span class="k">Max Loss</span> <span class="v bad">${vOrDash(maxLoss, fmtUsd)}</span></div>
              <div><span class="k">Dead Zone Width</span> <span class="v">${vOrDash(dzWidth, fmtUsd)}</span></div>
              <div><span class="k">Lower BEP</span> <span class="v">${vOrDash(m.lower_breakeven, fmt)}</span></div>
              <div><span class="k">Upper BEP</span> <span class="v">${vOrDash(m.upper_breakeven, fmt)}</span></div>
            </div>
          </div>
          <div style="margin-top:10px; border:1px solid #eee; padding:10px;">
            <div class="k">执行提示</div>
            <div><span class="k">Safety Gap (K - lower_bep)</span> <span class="v">${vOrDash(p.safety_gap_usd, fmtUsd)}</span></div>
            <div><span class="k">Risk/Reward</span> <span class="v ${clsPosNeg(p.risk_reward_ratio)}">${vOrDash(p.risk_reward_ratio, fmt)}</span></div>
            <div><span class="k">Golden Check</span>
              <span class="v ${goldenOk ? 'good' : 'bad'}">${goldenOk ? 'OK' : 'NO'}</span>
              <span class="k" style="margin-left:8px;">(1/p-1)=</span><span class="mono">${vOrDash(goldenLhs, fmt)}</span>
              <span class="k"> &gt; (N*q/W)=</span><span class="mono">${vOrDash(goldenRhs, fmt)}</span>
            </div>
          </div>
        `.trim();
        const pts = (data.curve || []).map(p => ({ x: Number(p.s), y: Number(p.pnl) })).filter(p => Number.isFinite(p.x) && Number.isFinite(p.y));
        drawPayoff(pts, Number(data.strike));
      }

      async function updatePayoff(tradeId) {
        try {
          const n = document.getElementById('manualN').value;
          const nParam = n ? ('&n_override=' + encodeURIComponent(n)) : '';
          const w = document.getElementById('manualW').value;
          const wParam = w ? ('&w_override=' + encodeURIComponent(w)) : '';
          const sbPoly = document.getElementById('sbPolyAsk') ? document.getElementById('sbPolyAsk').value : '';
          const sbDeri = document.getElementById('sbDeriPrem') ? document.getElementById('sbDeriPrem').value : '';
          const sbDs = document.getElementById('sbDeltaS') ? document.getElementById('sbDeltaS').value : '';
          const k = (typeof window.__strike === 'number' && Number.isFinite(window.__strike) && window.__strike > 0) ? window.__strike : 70000;
          const sandboxParams = (sbPoly && sbDeri)
            ? (`&poly_ask_override=${encodeURIComponent(sbPoly)}&deribit_premium_override=${encodeURIComponent(sbDeri)}&strike_override=${encodeURIComponent(String(k))}` + (sbDs ? `&delta_s_override=${encodeURIComponent(sbDs)}` : ''))
            : '';
          const strike = window.__strike;
          const scanParams = (typeof strike === 'number' && Number.isFinite(strike) && strike > 0)
            ? (`&scan_lower=${encodeURIComponent((strike*0.10).toFixed(0))}&scan_upper=${encodeURIComponent((strike*0.10).toFixed(0))}&scan_step=${encodeURIComponent((strike*0.01).toFixed(0))}`)
            : '';
          const q = tradeId
            ? ('?trade_id=' + encodeURIComponent(tradeId) + nParam + wParam + sandboxParams + scanParams)
            : ('?' + (nParam + wParam + sandboxParams + scanParams).replace(/^&/, ''));
          const res = await fetch('/api/v1/payoff' + q);
          const data = await res.json();
          if (!res.ok) {
            const msg = (data && data.detail) ? JSON.stringify(data.detail) : ('http_' + res.status);
            document.getElementById('payoffMeta').textContent = 'payoff_not_ready: ' + msg;
            return;
          }
          renderPayoff(data);
          window.__payoff.selected = tradeId || null;
        } catch (e) {
          document.getElementById('payoffMeta').textContent = 'payoff_fetch_error: ' + String(e);
        }
      }

      async function tick() {
        const t0 = performance.now();
        window.__meta = window.__meta || { pollMs: 5000, lastTickAt: 0, nextAt: 0 };
        const hb = document.getElementById('hb');
        const status = document.getElementById('status');
        hb.className = 'dot warn pulse';
        status.textContent = 'FETCHING';

        const [cfg, latest, rows, deltaSnaps] = await Promise.all([
          fetch('/api/v1/collector/config').then(r=>r.json()),
          fetch('/api/v1/collector/latest').then(r=>r.json()),
          fetch('/api/v1/collector/rows?limit=200').then(r=>r.json()),
          fetch('/api/v1/delta_snapshots/rows?limit=200').then(r=>r.json()),
        ]);
        const t1 = performance.now();
        document.getElementById('fetchMs').textContent = Math.round(t1 - t0);
        document.getElementById('cfg').textContent = JSON.stringify(cfg, null, 2);
        window.__strike = cfg && cfg.config ? Number(cfg.config.strike) : undefined;
        window.__feePct = cfg && cfg.config ? Number(cfg.config.fees_pct_total || 0) : 0;
        const le = (rows && rows.last_error) || (latest && latest.last_error) || (cfg && cfg.last_error);
        document.getElementById('err').textContent = le ? ('collector_error: ' + le) : '';
        setKpi(cfg.entry, latest.latest);
        renderCapacityProbe(cfg.entry);
        renderDeltaPanel(cfg.entry, cfg.config);
        document.getElementById('shadowCount').textContent = cfg && cfg.entry && cfg.entry.shadow_trades ? String(cfg.entry.shadow_trades) : '0';
        const tbody = document.getElementById('rows');
        window.__seen = window.__seen || new Set();
        window.__buf = window.__buf || [];
        const newly = new Set();
        (rows.rows || []).forEach(r=>{
          const k = r.ts_utc + '|' + r.trade_id;
          if (window.__seen.has(k)) return;
          window.__seen.add(k);
          window.__buf.push(r);
          newly.add(k);
        });
        window.__buf.sort((a,b)=> String(a.ts_utc).localeCompare(String(b.ts_utc)));
        const renderRows = window.__buf.slice(-500).slice().reverse();
        tbody.innerHTML = '';
        const r0 = performance.now();
        renderRows.slice(0, 200).forEach(r=>{
          const tr = document.createElement('tr');
          const k = r.ts_utc + '|' + r.trade_id;
          if (newly.has(k)) tr.className = 'newrow';
          tr.style.cursor = 'pointer';
          tr.onclick = () => {
            window.__payoff.selected = r.trade_id;
            document.getElementById('manualN').value = r.n ? String(r.n) : '';
            document.getElementById('manualW').value = r.budget_usd ? String(r.budget_usd) : '';
            updatePayoff(r.trade_id);
          };
          const cols = [
            r.ts_utc,
            fmt(r.trade_id),
            fmt(r.entry_ts_utc),
            fmtPct(r.mark_roi_pct),
            fmtPct(r.realizable_roi_pct),
            fmtUsd(r.mark_pnl_usd),
            fmtUsd(r.realizable_pnl_usd),
            fmtPct(r.worst_case_roi_pct),
            fmtPct(r.expected_roi_pct),
            fmt(r.gap),
            fmt(r.n),
            fmt(r.n_min),
            fmt(r.n_max),
            fmtPx(r.poly_best_bid),
            fmtPx(r.poly_best_ask),
            fmtUsd(r.deribit_index_price),
            fmt(r.deribit_delta),
            fmt(r.deribit_theta)
          ];
          cols.forEach(v=>{
            const td = document.createElement('td');
            td.textContent = v;
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        const chartPoints = window.__buf.slice(-300).map(r=>Number(r.mark_roi_pct || 0));
        drawChart(chartPoints);
        const r1 = performance.now();
        document.getElementById('renderMs').textContent = Math.round(r1 - r0);

        const dsBody = document.getElementById('deltaSnapRows');
        const dsCount = document.getElementById('deltaSnapCount');
        if (dsCount) dsCount.textContent = String(deltaSnaps && deltaSnaps.count ? deltaSnaps.count : 0);
        if (dsBody) {
          const ds = (deltaSnaps && deltaSnaps.rows) ? deltaSnaps.rows.slice().reverse() : [];
          dsBody.innerHTML = '';
          ds.slice(0, 200).forEach(r => {
            const tr = document.createElement('tr');
            const cols = [
              r.ts_utc,
              fmtPx(r.poly_best_ask),
              fmtPx(r.poly_best_bid),
              fmt(r.delta_ref_prob),
              fmtPx(r.deribit_best_ask),
              fmtPx(r.deribit_best_bid),
              fmtUsd(r.deribit_index_price),
            ];
            cols.forEach(v => {
              const td = document.createElement('td');
              td.textContent = v;
              tr.appendChild(td);
            });
            dsBody.appendChild(tr);
          });
        }

        window.__meta.lastTickAt = Date.now();
        window.__meta.nextAt = window.__meta.lastTickAt + window.__meta.pollMs;
        document.getElementById('lastUpdate').textContent = new Date(window.__meta.lastTickAt).toISOString();
        document.getElementById('rowCount').textContent = String(window.__buf.length);
        if (le) {
          hb.className = 'dot bad pulse';
          status.textContent = 'ERROR';
        } else {
          hb.className = 'dot live pulse';
          status.textContent = 'LIVE';
        }

        const manualN = document.getElementById('manualN').value;
        const manualW = document.getElementById('manualW').value;
        const sbPoly = document.getElementById('sbPolyAsk') ? document.getElementById('sbPolyAsk').value : '';
        const sbDeri = document.getElementById('sbDeriPrem') ? document.getElementById('sbDeriPrem').value : '';
        if (window.__payoff.selected) {
          updatePayoff(window.__payoff.selected);
        } else if (manualN || manualW || (sbPoly && sbDeri)) {
          updatePayoff(null);
        } else {
          const nr = cfg && cfg.entry && cfg.entry.n_range;
          if (nr && nr.suggested_n) updatePayoff(null);
          else document.getElementById('payoffMeta').textContent = 'payoff_not_ready: waiting_for_n_range (or use Manual N + 应用)';
        }
      }
      tick();
      window.__meta = window.__meta || { pollMs: 15000, lastTickAt: 0, nextAt: 0 };
      window.__meta.pollMs = 15000;
      setInterval(tick, window.__meta.pollMs);
      setInterval(() => {
        if (!window.__meta) return;
        const now = Date.now();
        const nextIn = window.__meta.nextAt ? Math.max(0, Math.ceil((window.__meta.nextAt - now) / 1000)) : 0;
        document.getElementById('nextIn').textContent = String(nextIn);
        const hb = document.getElementById('hb');
        if (hb && hb.className.indexOf('pulse') === -1) hb.className += ' pulse';
      }, 250);

      function updateSandboxOut() {
        const out = document.getElementById('sandboxOut');
        if (!out) return;
        const p = Number(document.getElementById('sbPolyAsk').value);
        const q = Number(document.getElementById('sbDeriPrem').value);
        const ds = Number(document.getElementById('sbDeltaS').value || 500);
        const n = Number(document.getElementById('sbN').value || 0.1);
        const w = Number(document.getElementById('sbW').value || 0);
        const feePct = Number(window.__feePct || 0);
        if (!(p > 0 && p < 1) || !(q > 0) || !(n > 0) || !(ds > 0)) {
          out.textContent = '';
          return;
        }
        const a = (1 / p) - 1;
        const denom = a - feePct;
        const wMin = denom > 0 ? (n * q * (1 + feePct)) / denom : null;
        const wMax = n * (ds / (1 + feePct) - q);
        const wOpt = (wMax && wMax > 0) ? wMax : null;
        const lines = [
          `Inputs: poly_ask=${fmtPx(p)} deri_premium_usd=${fmtUsd(q)} ΔS=${fmtUsd(ds)} N=${fmt(n)}`,
          `W_min (plateau>=0): ${wMin !== null ? fmtUsd(wMin) : '-'}`,
          `W_max (protected @K-ΔS): ${wMax > 0 ? fmtUsd(wMax) : '-'}`,
          `W_opt (recommend): ${wOpt !== null ? fmtUsd(wOpt) : '-'}`,
          `Tip: 点击“应用到图表”会把 N/W 写入右侧 Manual N/W 并画曲线`
        ];
        out.textContent = lines.join('\\n');
      }

      ['sbPolyAsk','sbDeriPrem','sbDeltaS','sbN','sbW'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener('input', updateSandboxOut);
      });

      document.getElementById('sandboxApply').onclick = (e) => {
        e.preventDefault();
        updateSandboxOut();
        const p = Number(document.getElementById('sbPolyAsk').value);
        const q = Number(document.getElementById('sbDeriPrem').value);
        const ds = Number(document.getElementById('sbDeltaS').value || 500);
        const n = Number(document.getElementById('sbN').value || 0.1);
        if (!(p > 0 && p < 1) || !(q > 0) || !(n > 0) || !(ds > 0)) return;
        const feePct = Number(window.__feePct || 0);
        const wMax = n * (ds / (1 + feePct) - q);
        const w = Number(document.getElementById('sbW').value || 0);
        document.getElementById('manualN').value = String(n);
        if (w > 0) document.getElementById('manualW').value = String(w);
        else if (wMax > 0) document.getElementById('manualW').value = String(Math.floor(wMax));
        window.__payoff.selected = null;
        updatePayoff(null);
      };

      document.getElementById('sandboxClear').onclick = (e) => {
        e.preventDefault();
        ['sbPolyAsk','sbDeriPrem','sbDeltaS','sbN','sbW'].forEach(id => {
          const el = document.getElementById(id);
          if (el) el.value = (id === 'sbN') ? '0.1' : '';
        });
        document.getElementById('sandboxOut').textContent = '';
        updatePayoff(window.__payoff.selected);
      };

      document.getElementById('payoffReset').onclick = (e) => {
        e.preventDefault();
        window.__payoff.selected = null;
        document.getElementById('manualN').value = '';
        document.getElementById('manualW').value = '';
        updatePayoff(null);
      };

      document.getElementById('applyN').onclick = (e) => {
        e.preventDefault();
        updatePayoff(window.__payoff.selected);
      };
    </script>
  </body>
</html>
    """.strip()
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
def index(
    date_iso: str = Query(default=SETTINGS.default_date_iso),
    strike: int = Query(default=SETTINGS.default_strike),
    currency: str = Query(default=SETTINGS.default_currency),
    option_type: str = Query(default=SETTINGS.default_deribit_option_type),
    depth: int = Query(default=10, ge=1, le=50),
    poly_event_slug: str | None = Query(default=SETTINGS.polymarket_event_slug),
):
    data = get_orderbooks(
        date_iso=date_iso,
        strike=strike,
        currency=currency,
        option_type=option_type,
        depth=depth,
        poly_event_slug=poly_event_slug,
    )
    poly_target = data["targets"]["polymarket"]
    deribit_target = data["targets"]["deribit"]

    poly_bids = data["polymarket"]["book"]["bids"]
    poly_asks = data["polymarket"]["book"]["asks"]
    deri_bids = data["deribit"]["book"]["bids"]
    deri_asks = data["deribit"]["book"]["asks"]

    poly_sum = data["polymarket"]["summary"]
    deri_sum = data["deribit"]["summary"]
    poly_depth = data["polymarket"]["depth_usd"]
    deribit_depth = data["deribit"]["depth_usd"]

    html = (
        "<html><head><meta charset='utf-8'/>"
        "<title>PDA Orderbooks</title></head><body>"
        "<h2>PoliDeribit-Arb (PDA) - Orderbook Viewer</h2>"
        f"<div><b>Params</b>: date={date_iso} strike={strike} currency={currency} option_type={option_type} depth={depth}</div>"
        "<hr/>"
        "<h3>Polymarket</h3>"
        f"<div>question={poly_target.get('question','')}</div>"
        f"<div>yes_token_id={poly_target.get('yes_token_id')}</div>"
        f"<div>best_bid={_fmt_float(poly_sum.get('best_bid'),decimals=4)} best_ask={_fmt_float(poly_sum.get('best_ask'),decimals=4)} mid={_fmt_float(poly_sum.get('mid'),decimals=4)} spread={_fmt_float(_safe_float(poly_sum.get('best_ask')) - _safe_float(poly_sum.get('best_bid')) if (poly_sum.get('best_ask') is not None and poly_sum.get('best_bid') is not None) else None,decimals=4)}</div>"
        f"<div>depth_usd: bids_total={_fmt_float(poly_depth.get('bids_total'),decimals=2,thousands=True)} asks_total={_fmt_float(poly_depth.get('asks_total'),decimals=2,thousands=True)}</div>"
        + _render_levels("Polymarket Book (Yes)", poly_bids, poly_asks)
        + "<hr/>"
        "<h3>Deribit</h3>"
        f"<div>instrument={deribit_target.get('instrument_name')}</div>"
        f"<div>best_bid={_fmt_float(deri_sum.get('best_bid'),decimals=4)} best_ask={_fmt_float(deri_sum.get('best_ask'),decimals=4)} mid={_fmt_float(deri_sum.get('mid'),decimals=4)} spread={_fmt_float(_safe_float(deri_sum.get('best_ask')) - _safe_float(deri_sum.get('best_bid')) if (deri_sum.get('best_ask') is not None and deri_sum.get('best_bid') is not None) else None,decimals=4)}</div>"
        f"<div>index_price={_fmt_float(deribit_depth.get('index_price'),decimals=2,thousands=True)} depth_usd: bids_total={_fmt_float(deribit_depth.get('bids_total'),decimals=2,thousands=True)} asks_total={_fmt_float(deribit_depth.get('asks_total'),decimals=2,thousands=True)}</div>"
        + _render_levels("Deribit Option Book (Put for < strike)", deri_bids, deri_asks)
        + "<hr/>"
        "<div><a href='/dashboard'>Dashboard: /dashboard</a></div>"
        "<div><a href='/api/v1/orderbooks'>JSON: /api/v1/orderbooks</a></div>"
        "<div><a href='/api/v1/targets'>JSON: /api/v1/targets</a></div>"
        "</body></html>"
    )
    return HTMLResponse(content=html)
