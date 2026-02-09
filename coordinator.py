from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BINANCE_TICKER_PRICE_ENDPOINT,
    BOERSE_FRANKFURT_QUOTE_URL_TEMPLATE,
    DEFAULT_MIC,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    LOGGER,
    SOURCE_BINANCE,
    SOURCE_BOERSE_FRANKFURT,
)


class PortfolioDataCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    def __init__(self, hass: HomeAssistant, entry_data: dict[str, Any] | None = None) -> None:
        self._session = async_get_clientsession(hass)
        self._assets: list[dict[str, Any]] = []
        self.amounts: dict[str, float] = {}

        interval_seconds = DEFAULT_UPDATE_INTERVAL_SECONDS
        if isinstance(entry_data, dict):
            interval_seconds = _safe_int(entry_data.get("update_interval"), DEFAULT_UPDATE_INTERVAL_SECONDS)
            assets = entry_data.get("assets")
            if isinstance(assets, list):
                self._assets = [a for a in assets if isinstance(a, dict)]

        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=_interval_from_seconds(interval_seconds),
        )

    def set_assets_from_entry_data(self, entry_data: dict[str, Any]) -> None:
        assets = entry_data.get("assets")
        if isinstance(assets, list):
            self._assets = [a for a in assets if isinstance(a, dict)]
        else:
            self._assets = []

        interval_seconds = _safe_int(entry_data.get("update_interval"), DEFAULT_UPDATE_INTERVAL_SECONDS)
        self.update_interval = _interval_from_seconds(interval_seconds)

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        now = dt_util.utcnow().isoformat()
        assets = list(self._assets)

        binance_assets = [a for a in assets if a.get("source") == SOURCE_BINANCE]
        boerse_assets = [a for a in assets if a.get("source") == SOURCE_BOERSE_FRANKFURT]

        prices_by_symbol: dict[str, float] = {}
        if binance_assets:
            try:
                prices_by_symbol = await self._fetch_binance_prices(binance_assets)
            except Exception as err:
                raise UpdateFailed(f"Binance update failed: {err}") from err

        boerse_results: list[tuple[str, float | None, str]] = []
        if boerse_assets:
            tasks: list[asyncio.Task[tuple[str, float | None, str]]] = []
            for a in boerse_assets:
                asset_id = str(a.get("asset_id", "")).strip()
                isin = str(a.get("instrument", "")).strip()
                mic = str(a.get("mic") or DEFAULT_MIC).strip() or DEFAULT_MIC
                if not asset_id or not isin:
                    continue
                tasks.append(asyncio.create_task(self._fetch_boerse_price(asset_id, isin, mic)))

            if tasks:
                try:
                    boerse_results = await asyncio.gather(*tasks)
                except Exception as err:
                    raise UpdateFailed(f"Boerse Frankfurt update failed: {err}") from err

        data: dict[str, dict[str, Any]] = {}

        for a in assets:
            asset_id = str(a.get("asset_id", "")).strip()
            if asset_id:
                data.setdefault(asset_id, {})

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

        for asset_id, price, mic in boerse_results:
            if price is None:
                continue
            data.setdefault(asset_id, {})
            data[asset_id]["price"] = price
            data[asset_id]["updated_at"] = now
            data[asset_id]["source"] = SOURCE_BOERSE_FRANKFURT
            data[asset_id]["mic"] = mic

        return data

    async def _fetch_binance_prices(self, binance_assets: list[dict[str, Any]]) -> dict[str, float]:
        symbols: list[str] = []
        for a in binance_assets:
            symbol = a.get("instrument")
            if isinstance(symbol, str) and symbol.strip():
                symbols.append(symbol.strip())

        symbols = sorted(set(symbols))
        if not symbols:
            return {}

        symbols_json = json.dumps(symbols, separators=(",", ":"))
        params = {"symbols": symbols_json}

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant/portfolio_assets",
        }

        async with asyncio.timeout(10):
            resp = await self._session.get(BINANCE_TICKER_PRICE_ENDPOINT, params=params, headers=headers)
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

    async def _fetch_boerse_price(self, asset_id: str, isin: str, mic: str) -> tuple[str, float | None, str]:
        url = BOERSE_FRANKFURT_QUOTE_URL_TEMPLATE.format(isin=isin, mic=mic)

        headers = {
            "Accept": "application/json",
            "User-Agent": "HomeAssistant/portfolio_assets",
        }

        async with asyncio.timeout(10):
            resp = await self._session.get(url, headers=headers)
            try:
                resp.raise_for_status()
                payload = await resp.json()
            finally:
                resp.release()

        if not isinstance(payload, dict):
            return asset_id, None, mic

        price_raw = payload.get("lastPrice")
        try:
            return asset_id, float(price_raw), mic
        except (TypeError, ValueError):
            return asset_id, None, mic


def _safe_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _interval_from_seconds(seconds: int) -> timedelta:
    if seconds <= 0:
        return DEFAULT_UPDATE_INTERVAL
    return timedelta(seconds=seconds)
