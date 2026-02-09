from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

LOGGER = logging.getLogger(__package__)

DOMAIN: Final = "portfolio_assets"

PLATFORMS: Final[list[str]] = ["sensor", "number"]

DEFAULT_UPDATE_INTERVAL_SECONDS: Final = 1800
DEFAULT_UPDATE_INTERVAL: Final = timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS)

CURRENCY_EUR: Final = "EUR"

SOURCE_BINANCE: Final = "binance"
SOURCE_BOERSE_FRANKFURT: Final = "boerse_frankfurt"

DEFAULT_MIC: Final = "XETR"

BINANCE_TICKER_PRICE_ENDPOINT: Final = "https://data-api.binance.vision/api/v3/ticker/price"

BOERSE_FRANKFURT_QUOTE_URL_TEMPLATE: Final = (
    "https://api.boerse-frankfurt.de/v1/data/quote_box/single?isin={isin}&mic={mic}"
)
