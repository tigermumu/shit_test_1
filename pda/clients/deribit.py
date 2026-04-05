from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import ccxt
import requests


@dataclass(frozen=True)
class DeribitInstrument:
    instrument_name: str
    currency: str
    expiry: date
    strike: int
    option_type: str


class DeribitClient:
    def __init__(self, exchange_id: str = "deribit", timeout_s: float = 15.0) -> None:
        exchange_class = getattr(ccxt, exchange_id)
        self._exchange = exchange_class({"enableRateLimit": True})
        self._session = requests.Session()
        self._timeout_s = timeout_s

    @staticmethod
    def format_instrument_name(currency: str, expiry: date, strike: int, option_type: str) -> str:
        exp = expiry.strftime("%d%b%y").upper()
        opt = option_type.upper()
        if opt not in {"C", "P"}:
            raise ValueError("option_type must be 'C' or 'P'")
        return f"{currency.upper()}-{exp}-{int(strike)}-{opt}"

    def get_instrument(self, currency: str, expiry: date, strike: int, option_type: str) -> DeribitInstrument:
        name = self.format_instrument_name(currency=currency, expiry=expiry, strike=strike, option_type=option_type)
        return DeribitInstrument(
            instrument_name=name,
            currency=currency.upper(),
            expiry=expiry,
            strike=int(strike),
            option_type=option_type.upper(),
        )

    def fetch_order_book(self, instrument_name: str, limit: int = 15) -> dict:
        try:
            r = self._session.get(
                "https://www.deribit.com/api/v2/public/get_order_book",
                params={"depth": int(limit), "instrument_name": instrument_name},
                timeout=self._timeout_s,
            )
            r.raise_for_status()
            data = r.json()
            result = data.get("result") if isinstance(data, dict) else None
            if isinstance(result, dict):
                return {
                    "bids": result.get("bids") or [],
                    "asks": result.get("asks") or [],
                    "timestamp": result.get("timestamp"),
                    "instrument_name": result.get("instrument_name") or instrument_name,
                    "index_price": result.get("index_price"),
                    "mark_price": result.get("mark_price"),
                    "best_bid_price": result.get("best_bid_price"),
                    "best_ask_price": result.get("best_ask_price"),
                    "greeks": result.get("greeks"),
                }
        except Exception:
            pass
        return self._exchange.fetch_order_book(symbol=instrument_name, limit=limit)
