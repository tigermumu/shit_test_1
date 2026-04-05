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
    global _collector_config, _collector_entry, _collector_last_error, _shadow_trades, _shadow_trade_seq

    poll_s = float(os.getenv("PDA_COLLECTOR_POLL_S", "15"))
    depth = int(os.getenv("PDA_COLLECTOR_DEPTH", str(SETTINGS.book_depth)))
    csv_path = os.getenv("PDA_COLLECTOR_CSV", str(Path.cwd() / "data" / "collector.csv"))

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
            "poll_s": poll_s,
            "depth": depth,
            "csv_path": csv_path,
        }
        _collector_entry = None
        _collector_last_error = None
        _shadow_trades = {}
        _shadow_trade_seq = 0

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

            effective_usd = None
            if poly_ask_depth_usd is not None and deri_ask_depth_usd is not None:
                effective_usd = min(float(max_capital_usd), float(poly_ask_depth_usd), float(deri_ask_depth_usd))

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
                    delta_s_usd=float(delta_s_usd),
                    target_profit_pct=float(target_profit_pct),
                    deribit_max_contracts=deri_ask_contracts,
                    prob_above=deribit_prob_above,
                )

            executable = bool(
                nres
                and nres.is_executable
                and effective_usd is not None
                and effective_usd >= 100.0
                and (poly_spread_pct is None or poly_spread_pct <= 0.05)
                and (deri_spread_pct is None or deri_spread_pct <= 0.05)
                and (drift_hours is None or drift_hours <= 2.0)
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
                    "effective_usd": effective_usd,
                    "poly_l1_ask": poly_ask_price,
                    "poly_l1_ask_depth_usd": poly_ask_depth_usd,
                    "deribit_l1_ask_btc": deri_ask_price_btc,
                    "deribit_l1_ask_premium_usd": deri_ask_premium_usd,
                    "deribit_l1_ask_depth_usd": deri_ask_depth_usd,
                    "n_range": None if not nres else nres.__dict__,
                    "is_executable": executable,
                    "time_drift_hours": drift_hours,
                    "shadow_trades": len(_shadow_trades),
                }

            if executable and nres and nres.suggested_n is not None and poly_ask_price is not None and deri_ask_premium_usd is not None and effective_usd is not None:
                _shadow_trade_seq += 1
                trade_id = f"{position_id}|{_shadow_trade_seq}"
                shares = float(effective_usd) / float(poly_ask_price)
                n = float(nres.suggested_n)
                cost = float(effective_usd) + n * float(deri_ask_premium_usd)
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
                    "n_min": nres.n_min,
                    "n_max": nres.n_max,
                    "worst_case_roi_pct": nres.worst_case_roi_pct,
                    "expected_roi_pct": nres.expected_roi_pct,
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
    </style>
  </head>
  <body>
    <h2>PDA 实时监控</h2>
    <div class="card" style="margin-top: 12px;">
      <div><b>KPI</b> <span id="err" class="bad"></span></div>
      <div class="kpi" id="kpi"></div>
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
      async function tick() {
        const [cfg, latest, rows] = await Promise.all([
          fetch('/api/v1/collector/config').then(r=>r.json()),
          fetch('/api/v1/collector/latest').then(r=>r.json()),
          fetch('/api/v1/collector/rows?limit=200').then(r=>r.json()),
        ]);
        document.getElementById('cfg').textContent = JSON.stringify(cfg, null, 2);
        const le = (rows && rows.last_error) || (latest && latest.last_error) || (cfg && cfg.last_error);
        document.getElementById('err').textContent = le ? ('collector_error: ' + le) : '';
        setKpi(cfg.entry, latest.latest);
        const tbody = document.getElementById('rows');
        window.__seen = window.__seen || new Set();
        window.__buf = window.__buf || [];
        (rows.rows || []).forEach(r=>{
          const k = r.ts_utc + '|' + r.trade_id;
          if (window.__seen.has(k)) return;
          window.__seen.add(k);
          window.__buf.push(r);
        });
        window.__buf.sort((a,b)=> String(a.ts_utc).localeCompare(String(b.ts_utc)));
        const renderRows = window.__buf.slice(-500).slice().reverse();
        tbody.innerHTML = '';
        renderRows.slice(0, 200).forEach(r=>{
          const tr = document.createElement('tr');
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
      }
      tick();
      setInterval(tick, 5000);
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
