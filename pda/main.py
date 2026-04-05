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
from fastapi.responses import HTMLResponse

from .clients import DeribitClient, PolymarketClient
from .config import SETTINGS
from .orderbook import (
    add_usd_depth,
    normalize_ccxt_levels,
    normalize_polymarket_levels,
    simulate_buy_by_budget,
    simulate_sell_qty,
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
    global _collector_config, _collector_entry, _collector_last_error

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
            "poll_s": poll_s,
            "depth": depth,
            "csv_path": csv_path,
        }
        _collector_entry = None
        _collector_last_error = None

    try:
        expiry = _parse_date(date_iso)
        instrument_name = _deribit.get_instrument(currency=currency, expiry=expiry, strike=strike, option_type=option_type).instrument_name
        resolved = _poly.resolve_from_event_slug(event_slug=event_slug, strike=strike)
        token_id = resolved.yes_token_id

        poly_book = _poly.fetch_order_book(token_id=token_id, depth=depth)
        deribit_book = _deribit.fetch_order_book(instrument_name=instrument_name, limit=depth)

        poly_bids = normalize_polymarket_levels(poly_book.get("bids", []))
        poly_asks = normalize_polymarket_levels(poly_book.get("asks", []))
        poly_bids.sort(key=lambda x: x["price"], reverse=True)
        poly_asks.sort(key=lambda x: x["price"])

        deri_bids = normalize_ccxt_levels(deribit_book.get("bids", []))
        deri_asks = normalize_ccxt_levels(deribit_book.get("asks", []))
        deri_bids.sort(key=lambda x: x["price"], reverse=True)
        deri_asks.sort(key=lambda x: x["price"])

        deri_index = _safe_float(deribit_book.get("index_price"))
        deri_budget_btc = (deribit_budget_usd / deri_index) if deri_index else 0.0

        poly_entry = simulate_buy_by_budget(poly_asks, budget_native=poly_budget_usd, usd_multiplier=1.0)
        deri_entry = simulate_buy_by_budget(deri_asks, budget_native=deri_budget_btc, usd_multiplier=deri_index)

        initial_cost_usd = float(poly_entry.notional_usd or 0.0) + float(deri_entry.notional_usd or 0.0)

        with _collector_lock:
            _collector_entry = {
                "ts_utc": _utc_now_iso(),
                "position_id": position_id,
                "poly_yes_token_id": token_id,
                "poly_shares": poly_entry.qty,
                "poly_avg_ask": poly_entry.avg_price,
                "deribit_instrument": instrument_name,
                "deribit_contracts": deri_entry.qty,
                "deribit_avg_ask_btc": deri_entry.avg_price,
                "deribit_index_price": deri_index,
                "initial_cost_usd": initial_cost_usd,
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

            poly_exit = simulate_sell_qty(poly_bids, qty_to_sell=float(_collector_entry["poly_shares"]), usd_multiplier=1.0)
            deri_exit = simulate_sell_qty(deri_bids, qty_to_sell=float(_collector_entry["deribit_contracts"]), usd_multiplier=deri_index)
            current_value_usd = float(poly_exit.notional_usd or 0.0) + float(deri_exit.notional_usd or 0.0)
            pnl_usd = current_value_usd - float(_collector_entry["initial_cost_usd"])
            roi = (pnl_usd / float(_collector_entry["initial_cost_usd"])) if float(_collector_entry["initial_cost_usd"]) > 0 else 0.0
            gap = (poly_sum.mid - deribit_prob_above) if (poly_sum.mid is not None and deribit_prob_above is not None) else None

            poly_buy_1500 = simulate_buy_by_budget(poly_asks, budget_native=1500.0, usd_multiplier=1.0)
            poly_buy_5000 = simulate_buy_by_budget(poly_asks, budget_native=5000.0, usd_multiplier=1.0)

            note = "normal"
            if roi >= 0.10:
                note = "target_reached"

            row: dict[str, Any] = {
                "ts_utc": ts,
                "position_id": position_id,
                "invest_usd": float(_collector_entry["initial_cost_usd"]),
                "value_now_usd": current_value_usd,
                "pnl_usd": pnl_usd,
                "roi_pct": roi * 100.0,
                "gap": gap,
                "note": note,
                "poly_event_slug": event_slug,
                "poly_yes_token_id": token_id,
                "poly_best_bid": poly_sum.best_bid,
                "poly_best_ask": poly_sum.best_ask,
                "poly_spread": poly_spread,
                "poly_exit_avg_bid": poly_exit.avg_price,
                "poly_buy_avg_1500": poly_buy_1500.avg_price,
                "poly_buy_slip_1500": (poly_buy_1500.avg_price - poly_sum.best_ask) if (poly_buy_1500.avg_price is not None and poly_sum.best_ask is not None) else None,
                "poly_buy_avg_5000": poly_buy_5000.avg_price,
                "poly_buy_slip_5000": (poly_buy_5000.avg_price - poly_sum.best_ask) if (poly_buy_5000.avg_price is not None and poly_sum.best_ask is not None) else None,
                "deribit_instrument": instrument_name,
                "deribit_index_price": deri_index,
                "deribit_best_bid": deri_sum.best_bid,
                "deribit_best_ask": deri_sum.best_ask,
                "deribit_spread_btc": deri_spread_btc,
                "deribit_spread_usd": (deri_spread_btc * deri_index) if (deri_spread_btc is not None and deri_index is not None) else None,
                "deribit_exit_avg_bid_btc": deri_exit.avg_price,
                "deribit_delta": deri_delta,
                "deribit_theta": deri_theta,
                "deribit_prob_above": deribit_prob_above,
            }

            with _collector_lock:
                _collector_rows.append(row)
                _collector_last_error = None
            if os.getenv("PDA_COLLECTOR_WRITE_CSV", "true").lower() in {"1", "true", "yes"}:
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
        b_usd = "" if b is None else b.get("notional_usd")
        a_usd = "" if a is None else a.get("notional_usd")
        rows.append(
            "<tr>"
            f"<td>{'' if b is None else b['price']}</td>"
            f"<td>{'' if b is None else b['size']}</td>"
            f"<td>{b_usd}</td>"
            f"<td>{'' if a is None else a['price']}</td>"
            f"<td>{'' if a is None else a['size']}</td>"
            f"<td>{a_usd}</td>"
            "</tr>"
        )

    return (
        f"<h3>{title}</h3>"
        "<table border='1' cellpadding='6' cellspacing='0'>"
        "<thead><tr><th>Bid Px</th><th>Bid Sz</th><th>Bid USD</th><th>Ask Px</th><th>Ask Sz</th><th>Ask USD</th></tr></thead>"
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
      .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace; }
    </style>
  </head>
  <body>
    <h2>PDA 实时监控</h2>
    <div class="row">
      <div class="card">
        <div><b>Latest</b></div>
        <div id="latest" class="mono"></div>
      </div>
      <div class="card">
        <div><b>Config</b></div>
        <div id="cfg" class="mono"></div>
      </div>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div><b>ROI 曲线</b></div>
      <canvas id="chart" width="1200" height="220"></canvas>
    </div>
    <div class="card" style="margin-top: 12px;">
      <div><b>最近记录</b></div>
      <table>
        <thead>
          <tr>
            <th>ts_utc</th>
            <th>value_now_usd</th>
            <th>pnl_usd</th>
            <th>roi_pct</th>
            <th>gap</th>
            <th>poly_spread</th>
            <th>poly_buy_slip_1500</th>
            <th>poly_buy_slip_5000</th>
            <th>deribit_index</th>
            <th>deri_spread_usd</th>
            <th>deri_delta</th>
            <th>deri_theta</th>
            <th>note</th>
          </tr>
        </thead>
        <tbody id="rows"></tbody>
      </table>
    </div>
    <script>
      function fmt(x) {
        if (x === null || x === undefined) return '';
        if (typeof x === 'number') return x.toFixed(6);
        return String(x);
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
        ctx.fillText('roi_pct', pad+4, pad+12);
        ctx.fillText(fmt(ymin), pad+4, pad+h-2);
        ctx.fillText(fmt(ymax), pad+4, pad+24);
      }
      async function tick() {
        const [cfg, latest, rows] = await Promise.all([
          fetch('/api/v1/collector/config').then(r=>r.json()),
          fetch('/api/v1/collector/latest').then(r=>r.json()),
          fetch('/api/v1/collector/rows?limit=200').then(r=>r.json()),
        ]);
        document.getElementById('cfg').textContent = JSON.stringify(cfg, null, 2);
        document.getElementById('latest').textContent = JSON.stringify(latest, null, 2);
        const tbody = document.getElementById('rows');
        tbody.innerHTML = '';
        const rs = (rows.rows || []).slice().reverse().slice(0, 50);
        rs.forEach(r=>{
          const tr = document.createElement('tr');
          const cols = [
            r.ts_utc, r.value_now_usd, r.pnl_usd, r.roi_pct, r.gap, r.poly_spread,
            r.poly_buy_slip_1500, r.poly_buy_slip_5000, r.deribit_index_price, r.deribit_spread_usd,
            r.deribit_delta, r.deribit_theta, r.note
          ];
          cols.forEach(v=>{
            const td = document.createElement('td');
            td.textContent = fmt(v);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        const chartPoints = (rows.rows || []).map(r=>Number(r.roi_pct || 0));
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

    html = (
        "<html><head><meta charset='utf-8'/>"
        "<title>PDA Orderbooks</title></head><body>"
        "<h2>PoliDeribit-Arb (PDA) - Orderbook Viewer</h2>"
        f"<div><b>Params</b>: date={date_iso} strike={strike} currency={currency} option_type={option_type} depth={depth}</div>"
        "<hr/>"
        "<h3>Polymarket</h3>"
        f"<div>question={poly_target.get('question','')}</div>"
        f"<div>yes_token_id={poly_target.get('yes_token_id')}</div>"
        f"<div>best_bid={poly_sum.get('best_bid')} best_ask={poly_sum.get('best_ask')} mid={poly_sum.get('mid')}</div>"
        + _render_levels("Polymarket Book (Yes)", poly_bids, poly_asks)
        + "<hr/>"
        "<h3>Deribit</h3>"
        f"<div>instrument={deribit_target.get('instrument_name')}</div>"
        f"<div>best_bid={deri_sum.get('best_bid')} best_ask={deri_sum.get('best_ask')} mid={deri_sum.get('mid')}</div>"
        + _render_levels("Deribit Option Book (Put for < strike)", deri_bids, deri_asks)
        + "<hr/>"
        "<div><a href='/api/v1/orderbooks'>JSON: /api/v1/orderbooks</a></div>"
        "<div><a href='/api/v1/targets'>JSON: /api/v1/targets</a></div>"
        "</body></html>"
    )
    return HTMLResponse(content=html)
