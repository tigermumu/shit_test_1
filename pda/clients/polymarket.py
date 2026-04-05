from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

import requests


@dataclass(frozen=True)
class ResolvedPolymarketTarget:
    question: str
    market_id: str
    yes_token_id: str
    no_token_id: str | None


class PolymarketClient:
    def __init__(self, gamma_api_base: str, clob_api_base: str, timeout_s: float = 15.0) -> None:
        self._gamma_api_base = gamma_api_base.rstrip("/")
        self._clob_api_base = clob_api_base.rstrip("/")
        self._timeout_s = timeout_s
        self._session = requests.Session()

    def _gamma_get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self._gamma_api_base}/{path.lstrip('/')}"
        r = self._session.get(url, params=params, timeout=self._timeout_s)
        r.raise_for_status()
        return r.json()

    def search_market_for_btc_above(
        self,
        expiry: date,
        strike: int,
        query: str,
    ) -> ResolvedPolymarketTarget:
        raw = self._gamma_get("/markets", params={"search": query, "limit": 50})
        if isinstance(raw, list):
            markets = raw
        elif isinstance(raw, dict):
            markets = raw.get("markets") or raw.get("data") or raw.get("results") or []
        else:
            markets = []
        if not isinstance(markets, list):
            markets = []

        wanted_date = expiry.isoformat()
        wanted_strike = str(int(strike))
        date_patterns = [
            wanted_date,
            expiry.strftime("%b").lower() + " " + str(expiry.day),
            expiry.strftime("%b %d").lower(),
            expiry.strftime("%B").lower() + " " + str(expiry.day),
        ]

        def matches_question(q: str) -> bool:
            s = q.lower()
            if "btc" not in s:
                return False
            if wanted_strike not in re.sub(r"[,_\s]", "", s):
                return False
            return any(p in s for p in date_patterns)

        candidates: list[dict[str, Any]] = []
        for m in markets:
            if not isinstance(m, dict):
                continue
            if m.get("closed") is True:
                continue
            if m.get("active") is False:
                continue
            q = m.get("question")
            if isinstance(q, str) and matches_question(q):
                candidates.append(m)

        if not candidates:
            for m in markets:
                if not isinstance(m, dict):
                    continue
                q = m.get("question")
                if not isinstance(q, str):
                    continue
                s = q.lower()
                if "btc" in s and wanted_strike in re.sub(r"[,_\s]", "", s):
                    candidates.append(m)

        if not candidates:
            raise RuntimeError("Gamma market search returned no matching candidates; set PDA_POLY_YES_TOKEN_ID to bypass search")

        selected = candidates[0]
        token_ids = selected.get("clobTokenIds")
        yes_token_id: str | None = None
        no_token_id: str | None = None
        if isinstance(token_ids, str):
            if token_ids.strip().startswith("["):
                try:
                    parsed = json.loads(token_ids)
                    ids = [str(x) for x in parsed] if isinstance(parsed, list) else []
                except Exception:
                    ids = []
            else:
                ids = [t.strip() for t in token_ids.split(",") if t.strip()]
        elif isinstance(token_ids, list):
            ids = [str(x) for x in token_ids]
        else:
            ids = []

        if len(ids) >= 2:
            yes_token_id = ids[0]
            no_token_id = ids[1]
        elif len(ids) == 1:
            yes_token_id = ids[0]

        if not yes_token_id:
            raise RuntimeError("Selected Polymarket market has no clobTokenIds; set PDA_POLY_YES_TOKEN_ID to proceed")

        return ResolvedPolymarketTarget(
            question=str(selected.get("question") or ""),
            market_id=str(selected.get("id") or ""),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )

    def resolve_from_event_slug(self, event_slug: str, strike: int) -> ResolvedPolymarketTarget:
        raw = self._gamma_get("/events", params={"slug": event_slug})
        events = raw if isinstance(raw, list) else raw.get("events") if isinstance(raw, dict) else []
        if not isinstance(events, list) or not events:
            raise RuntimeError("Gamma event slug not found")

        event = events[0]
        markets = event.get("markets") if isinstance(event, dict) else []
        if not isinstance(markets, list) or not markets:
            raise RuntimeError("Gamma event has no markets")

        wanted_strike = str(int(strike))
        wanted_strike_compact = wanted_strike

        def score(m: dict[str, Any]) -> int:
            q = str(m.get("question") or "").lower()
            compact = re.sub(r"[,_\s$]", "", q)
            s = 0
            if wanted_strike_compact in compact:
                s += 50
            if "above" in q:
                s += 20
            if "bitcoin" in q or "btc" in q:
                s += 10
            if m.get("closed") is True:
                s -= 100
            if m.get("active") is False:
                s -= 100
            return s

        candidates: list[dict[str, Any]] = [m for m in markets if isinstance(m, dict)]
        candidates.sort(key=score, reverse=True)
        if not candidates:
            raise RuntimeError("No candidate markets in event")

        selected = candidates[0]
        token_ids = selected.get("clobTokenIds")
        yes_token_id: str | None = None
        no_token_id: str | None = None
        if isinstance(token_ids, str):
            if token_ids.strip().startswith("["):
                try:
                    parsed = json.loads(token_ids)
                    ids = [str(x) for x in parsed] if isinstance(parsed, list) else []
                except Exception:
                    ids = []
            else:
                ids = [t.strip() for t in token_ids.split(",") if t.strip()]
        elif isinstance(token_ids, list):
            ids = [str(x) for x in token_ids]
        else:
            ids = []

        if len(ids) >= 2:
            yes_token_id = ids[0]
            no_token_id = ids[1]
        elif len(ids) == 1:
            yes_token_id = ids[0]

        if not yes_token_id:
            raise RuntimeError("Selected market has no clobTokenIds; set PDA_POLY_YES_TOKEN_ID to proceed")

        return ResolvedPolymarketTarget(
            question=str(selected.get("question") or ""),
            market_id=str(selected.get("id") or ""),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )

    def fetch_order_book(self, token_id: str, depth: int = 15) -> dict[str, Any]:
        r = self._session.get(f"{self._clob_api_base}/book", params={"token_id": token_id}, timeout=self._timeout_s)
        if r.status_code >= 400:
            r.raise_for_status()
        book = r.json()

        bids = book.get("bids") if isinstance(book, dict) else None
        asks = book.get("asks") if isinstance(book, dict) else None
        out_bids = list(bids or [])
        out_asks = list(asks or [])

        def price_of(level: Any) -> float:
            if isinstance(level, dict):
                p = level.get("price")
            elif isinstance(level, (list, tuple)) and len(level) >= 1:
                p = level[0]
            else:
                p = None
            try:
                return float(p)
            except Exception:
                return float("-inf")

        out_bids.sort(key=price_of, reverse=True)

        def price_of_ask(level: Any) -> float:
            if isinstance(level, dict):
                p = level.get("price")
            elif isinstance(level, (list, tuple)) and len(level) >= 1:
                p = level[0]
            else:
                p = None
            try:
                return float(p)
            except Exception:
                return float("inf")

        out_asks.sort(key=price_of_ask)

        out: dict[str, Any] = {"bids": out_bids[:depth], "asks": out_asks[:depth]}
        if isinstance(book, dict):
            for k in ("timestamp", "market", "asset_id", "tick_size"):
                if k in book:
                    out[k] = book[k]
        return out
