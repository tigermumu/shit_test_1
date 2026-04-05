from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    gamma_api_base: str = os.getenv("PDA_GAMMA_API", "https://gamma-api.polymarket.com")
    clob_api_base: str = os.getenv("PDA_CLOB_API", "https://clob.polymarket.com")
    deribit_exchange_id: str = os.getenv("PDA_DERIBIT_EXCHANGE_ID", "deribit")

    default_currency: str = os.getenv("PDA_DEFAULT_CCY", "BTC")
    default_date_iso: str = os.getenv("PDA_DEFAULT_DATE", "2026-04-10")
    default_strike: int = int(os.getenv("PDA_DEFAULT_STRIKE", "70000"))
    default_deribit_option_type: str = os.getenv("PDA_DEFAULT_OPTION_TYPE", "P")

    polymarket_yes_token_id: str | None = os.getenv("PDA_POLY_YES_TOKEN_ID")
    polymarket_event_slug: str | None = os.getenv("PDA_POLY_EVENT_SLUG", "bitcoin-above-on-april-10")
    polymarket_market_search: str = os.getenv(
        "PDA_POLY_MARKET_SEARCH",
        "BTC Price at 2026-04-10 > 70000",
    )

    request_timeout_s: float = float(os.getenv("PDA_REQUEST_TIMEOUT_S", "15"))
    book_depth: int = int(os.getenv("PDA_BOOK_DEPTH", "15"))


SETTINGS = Settings()
