from __future__ import annotations

DOMAIN = "portfolio_assets"

PLATFORMS: list[str] = ["sensor", "number"]

DEFAULT_UPDATE_INTERVAL = 1800

# Multiplies Value sensors only (price and amount stay unchanged)
DEFAULT_VALUE_MULTIPLIER = 1.0

SOURCE_BINANCE = "binance"
SOURCE_BOERSE_FRANKFURT = "boerse_frankfurt"
SOURCE_WIENERBOERSE_OEKB = "wienerboerse_oekb"

SUPPORTED_SOURCES: set[str] = {
    SOURCE_BINANCE,
    SOURCE_BOERSE_FRANKFURT,
    SOURCE_WIENERBOERSE_OEKB,
}

SUPPORTED_KINDS: set[str] = {"crypto", "etf", "fund"}

HTTP_TIMEOUT = 20

BINANCE_API_BASE = "https://data-api.binance.vision"
BINANCE_TICKER_PRICE_PATH = "/api/v3/ticker/price"

BOERSE_FRANKFURT_QUOTE_URL = "https://api.boerse-frankfurt.de/v1/data/quote_box/single"

WIENERBOERSE_OEKB_LIST_URL = "https://www.wienerborse.at/fondsdaten-oekb/"
WIENERBOERSE_OEKB_QUOTE_URL_PREFIX = "https://www.wienerborse.at/marktdaten/fondsdaten-der-oekb/preisdaten/"
