from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BINANCE_API_BASE,
    BINANCE_TICKER_PRICE_PATH,
    BOERSE_FRANKFURT_QUOTE_URL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    HTTP_TIMEOUT,
    SOURCE_BINANCE,
    SOURCE_BOERSE_FRANKFURT,
    SOURCE_WIENERBOERSE_OEKB,
    WIENERBOERSE_OEKB_LIST_URL,
    WIENERBOERSE_OEKB_QUOTE_URL_PREFIX,
)

_LOGGER = logging.getLogger(__name__)

_PER_PAGE = 50
_MAX_PAGES_HARD_LIMIT = 120

_NUMBER_RE = re.compile(r"(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d+)|\d+(?:[.,]\d+)?)")


class PortfolioDataCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any] | None = None) -> None:
        self._session = async_get_clientsession(hass)
        self._assets: list[dict[str, Any]] = []
        self.amounts: dict[str, float] = {}

        # ISIN -> (id_notation, quote_url)
        self._oekb_cache: dict[str, tuple[str, str]] = {}

        interval_seconds = DEFAULT_UPDATE_INTERVAL
        if isinstance(entry_data, dict):
            interval_seconds = _safe_int(entry_data.get("update_interval"), DEFAULT_UPDATE_INTERVAL)
            assets = entry_data.get("assets")
            if isinstance(assets, list):
                self._assets = [a for a in assets if isinstance(a, dict)]

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval_seconds),
        )

    def set_assets_from_entry_data(self, entry_data: dict[str, Any]) -> None:
        assets = entry_data.get("assets")
        if isinstance(assets, list):
            self._assets = [a for a in assets if isinstance(a, dict)]
        else:
            self._assets = []

        interval_seconds = _safe_int(entry_data.get("update_interval"), DEFAULT_UPDATE_INTERVAL)
        self.update_interval = timedelta(seconds=interval_seconds)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        now = dt_util.utcnow().isoformat()
        assets = list(self._assets)

        binance_assets = [a for a in assets if a.get("source") == SOURCE_BINANCE]
        boerse_assets = [a for a in assets if a.get("source") == SOURCE_BOERSE_FRANKFURT]
        oekb_assets = [a for a in assets if a.get("source") == SOURCE_WIENERBOERSE_OEKB]

        data: dict[str, dict[str, Any]] = {}
        for a in assets:
            asset_id = str(a.get("asset_id", "")).strip()
            if asset_id:
                data.setdefault(asset_id, {})

        if binance_assets:
            try:
                prices_by_symbol = await self._fetch_binance_prices(binance_assets)
            except Exception as err:
                raise UpdateFailed(f"Binance update failed: {err}") from err

            for a in binance_assets:
                asset_id = str(a.get("asset_id", "")).strip()
                symbol = str(a.get("instrument", "")).strip()
                if not asset_id or not symbol:
                    continue
                price = prices_by_symbol.get(symbol)
                if price is None:
                    continue
                data.setdefault(asset_id, {})
                data[asset_id]["price"] = price
                data[asset_id]["updated_at"] = now
                data[asset_id]["source"] = SOURCE_BINANCE

        if boerse_assets:
            tasks_bf: list[asyncio.Task[tuple[str, float | None]]] = []
            for a in boerse_assets:
                asset_id = str(a.get("asset_id", "")).strip()
                isin = str(a.get("instrument", "")).strip()
                mic = str(a.get("mic", "XETR")).strip() or "XETR"
                if not asset_id or not isin:
                    continue
                tasks_bf.append(asyncio.create_task(self._fetch_boerse_frankfurt_price(asset_id, isin, mic)))

            try:
                results = await asyncio.gather(*tasks_bf) if tasks_bf else []
            except Exception as err:
                raise UpdateFailed(f"Boerse Frankfurt update failed: {err}") from err

            for asset_id, price in results:
                if price is None:
                    continue
                data.setdefault(asset_id, {})
                data[asset_id]["price"] = price
                data[asset_id]["updated_at"] = now
                data[asset_id]["source"] = SOURCE_BOERSE_FRANKFURT

        if oekb_assets:
            tasks_oekb: list[asyncio.Task[tuple[str, float | None, str | None]]] = []
            for a in oekb_assets:
                asset_id = str(a.get("asset_id", "")).strip()
                instrument = str(a.get("instrument", "")).strip()
                if not asset_id or not instrument:
                    continue
                tasks_oekb.append(asyncio.create_task(self._fetch_wienerboerse_oekb_price(asset_id, instrument)))

            try:
                results = await asyncio.gather(*tasks_oekb) if tasks_oekb else []
            except Exception as err:
                raise UpdateFailed(f"Wienerboerse OeKB update failed: {err}") from err

            for asset_id, price, quote_url in results:
                data.setdefault(asset_id, {})
                data[asset_id]["updated_at"] = now
                data[asset_id]["source"] = SOURCE_WIENERBOERSE_OEKB
                if quote_url:
                    data[asset_id]["quote_url"] = quote_url
                if price is None:
                    continue
                data[asset_id]["price"] = price

        return data

    async def _fetch_binance_prices(self, binance_assets: list[dict[str, Any]]) -> dict[str, float]:
        symbols: list[str] = []
        for a in binance_assets:
            sym = a.get("instrument")
            if isinstance(sym, str) and sym.strip():
                symbols.append(sym.strip())

        symbols = sorted(set(symbols))
        if not symbols:
            return {}

        symbols_json = json.dumps(symbols, separators=(",", ":"))
        params = {"symbols": symbols_json}

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant/portfolio_assets",
        }

        url = f"{BINANCE_API_BASE}{BINANCE_TICKER_PRICE_PATH}"
        async with asyncio.timeout(HTTP_TIMEOUT):
            resp = await self._session.get(url, params=params, headers=headers)
            try:
                resp.raise_for_status()
                payload = await resp.json()
            finally:
                resp.release()

        out: dict[str, float] = {}
        if isinstance(payload, list):
            for row in payload:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("symbol")
                price = row.get("price")
                if not isinstance(symbol, str):
                    continue
                try:
                    out[symbol] = float(price)
                except (TypeError, ValueError):
                    continue

        return out

    async def _fetch_boerse_frankfurt_price(self, asset_id: str, isin: str, mic: str) -> tuple[str, float | None]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant/portfolio_assets",
        }

        params = {"isin": isin, "mic": mic}

        async with asyncio.timeout(HTTP_TIMEOUT):
            resp = await self._session.get(BOERSE_FRANKFURT_QUOTE_URL, params=params, headers=headers)
            try:
                resp.raise_for_status()
                payload = await resp.json()
            finally:
                resp.release()

        if not isinstance(payload, dict):
            return asset_id, None

        try:
            return asset_id, float(payload.get("lastPrice"))
        except (TypeError, ValueError):
            return asset_id, None

    async def _fetch_wienerboerse_oekb_price(
        self,
        asset_id: str,
        instrument: str,
    ) -> tuple[str, float | None, str | None]:
        headers = {
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": "HomeAssistant/portfolio_assets",
        }

        # instrument can be a full quote URL
        if instrument.startswith("http://") or instrument.startswith("https://"):
            quote_url = instrument
            html = await self._fetch_text(quote_url, headers=headers)
            price = _parse_oekb_price_from_html(html)
            return asset_id, price, quote_url

        # otherwise treat as ISIN
        isin = instrument

        # 1) Try direct quote by ISIN (may redirect to canonical URL)
        direct_url = f"{WIENERBOERSE_OEKB_QUOTE_URL_PREFIX}?ISIN={isin}"
        try:
            html, final_url = await self._fetch_text_with_final_url(direct_url, headers=headers)
            price = _parse_oekb_price_from_html(html)
            if price is not None:
                _maybe_cache_oekb_from_url(self._oekb_cache, isin, final_url)
                return asset_id, price, final_url
        except Exception as err:
            _LOGGER.debug("OeKB direct-by-ISIN failed for %s: %s", isin, err)

        # 2) Resolve canonical quote URL via list pages
        quote_url = await self._resolve_oekb_quote_url(isin, headers=headers)
        if not quote_url:
            _LOGGER.debug("OeKB: could not resolve quote url for ISIN %s", isin)
            return asset_id, None, None

        html = await self._fetch_text(quote_url, headers=headers)
        price = _parse_oekb_price_from_html(html)
        return asset_id, price, quote_url

    async def _resolve_oekb_quote_url(self, isin: str, headers: dict[str, str]) -> str | None:
        cached = self._oekb_cache.get(isin)
        if cached:
            return cached[1]

        html_first = await self._fetch_oekb_list_page(page=1, headers=headers)
        found = _extract_oekb_quote_params(html_first, isin)
        if found:
            id_notation, chash = found
            url = f"{WIENERBOERSE_OEKB_QUOTE_URL_PREFIX}?ID_NOTATION={id_notation}&ISIN={isin}&cHash={chash}"
            self._oekb_cache[isin] = (id_notation, url)
            return url

        total_hits = _extract_oekb_total_hits(html_first)
        total_pages = None
        if total_hits is not None:
            total_pages = max(1, math.ceil(total_hits / _PER_PAGE))

        max_pages = min(total_pages or _MAX_PAGES_HARD_LIMIT, _MAX_PAGES_HARD_LIMIT)

        for page in range(2, max_pages + 1):
            try:
                html = await self._fetch_oekb_list_page(page=page, headers=headers)
            except Exception as err:
                _LOGGER.debug("OeKB list page fetch failed page=%s err=%s", page, err)
                continue

            found = _extract_oekb_quote_params(html, isin)
            if not found:
                continue

            id_notation, chash = found
            quote_url = f"{WIENERBOERSE_OEKB_QUOTE_URL_PREFIX}?ID_NOTATION={id_notation}&ISIN={isin}&cHash={chash}"
            self._oekb_cache[isin] = (id_notation, quote_url)
            return quote_url

        return None

    async def _fetch_oekb_list_page(self, page: int, headers: dict[str, str]) -> str:
        params = {
            "c13206-page": page,
            "c13206-sort": "isin",
            "per-page": _PER_PAGE,
        }
        async with asyncio.timeout(HTTP_TIMEOUT):
            resp = await self._session.get(WIENERBOERSE_OEKB_LIST_URL, params=params, headers=headers)
            try:
                resp.raise_for_status()
                return await resp.text()
            finally:
                resp.release()

    async def _fetch_text(self, url: str, headers: dict[str, str]) -> str:
        async with asyncio.timeout(HTTP_TIMEOUT):
            resp = await self._session.get(url, headers=headers)
            try:
                resp.raise_for_status()
                return await resp.text()
            finally:
                resp.release()

    async def _fetch_text_with_final_url(self, url: str, headers: dict[str, str]) -> tuple[str, str]:
        async with asyncio.timeout(HTTP_TIMEOUT):
            resp = await self._session.get(url, headers=headers)
            try:
                resp.raise_for_status()
                text = await resp.text()
                return text, str(resp.url)
            finally:
                resp.release()


def _maybe_cache_oekb_from_url(cache: dict[str, tuple[str, str]], isin: str, url: str) -> None:
    m = re.search(r"ID_NOTATION=(\d+).*?cHash=([0-9a-fA-F]+)", url)
    if not m:
        return
    cache[isin] = (m.group(1), url)


def _extract_oekb_total_hits(html: str) -> int | None:
    m = re.search(r"Ihre Suche ergab\s+([\d\.]+)\s+Treffer", html)
    if m:
        try:
            return int(m.group(1).replace(".", ""))
        except ValueError:
            return None

    m = re.search(r"(?:Your search (?:yielded|returned))\s+([\d,]+)", html, flags=re.IGNORECASE)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            return None

    return None


def _extract_oekb_quote_params(html: str, isin: str) -> tuple[str, str] | None:
    pattern = re.compile(
        r"ID_NOTATION=(\d+)(?:&|&amp;)ISIN="
        + re.escape(isin)
        + r"(?:&|&amp;)cHash=([0-9a-fA-F]+)"
    )
    m = pattern.search(html)
    if not m:
        return None
    return m.group(1), m.group(2)


def _safe_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _parse_decimal_number(text: str) -> float | None:
    s = text.strip().replace("\xa0", " ").replace(" ", "")

    last_dot = s.rfind(".")
    last_comma = s.rfind(",")

    if last_dot != -1 and last_comma != -1:
        if last_dot > last_comma:
            s = s.replace(",", "")
        else:
            s = s.replace(".", "")
            s = s.replace(",", ".")
    elif last_comma != -1:
        s = s.replace(".", "")
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")

    try:
        return float(s)
    except ValueError:
        return None


def _parse_oekb_price_from_html(html: str) -> float | None:
    anchors = [
        r"Rücknahmepreis",
        r"Redemption\s*Value",
        r"Ausgabepreis",
        r"Issuer\s*Price",
        r"Schlusspreis",
        r"Last\s*Close",
        r"Net\s*Asset\s*Value",
        r"\bNAV\b",
    ]

    for label in anchors:
        idx = re.search(label, html, flags=re.IGNORECASE)
        if not idx:
            continue

        start = max(idx.start() - 200, 0)
        end = min(idx.end() + 1400, len(html))
        window = html[start:end]

        num = _NUMBER_RE.search(window)
        if not num:
            continue

        value = _parse_decimal_number(num.group(1))
        if value is not None:
            return value

    num = _NUMBER_RE.search(html)
    if num:
        return _parse_decimal_number(num.group(1))

    return None
