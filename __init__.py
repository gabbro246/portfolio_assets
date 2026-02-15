from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    CHANGE_WINDOWS_DAYS,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_VALUE_MULTIPLIER,
    DOMAIN,
    PLATFORMS,
    SOURCE_BOERSE_FRANKFURT,
    SOURCE_BINANCE,
    SOURCE_WIENERBOERSE_OEKB,
    SUPPORTED_KINDS,
)
from .coordinator import PortfolioDataCoordinator

ASSET_SCHEMA = vol.Schema(
    {
        vol.Required("asset_id"): cv.slug,
        vol.Required("name"): cv.string,
        vol.Required("source"): vol.In([SOURCE_BINANCE, SOURCE_BOERSE_FRANKFURT, SOURCE_WIENERBOERSE_OEKB]),
        vol.Required("kind"): vol.In(sorted(SUPPORTED_KINDS)),
        vol.Required("instrument"): cv.string,
        vol.Optional("amount_unit", default=""): cv.string,
        vol.Optional("mic", default="XETR"): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("update_interval", default=DEFAULT_UPDATE_INTERVAL): cv.positive_int,
                vol.Optional("value_multiplier", default=DEFAULT_VALUE_MULTIPLIER): vol.Coerce(float),
                vol.Optional("change_windows_days", default=list(CHANGE_WINDOWS_DAYS)): vol.All(
                    cv.ensure_list,
                    [cv.positive_int],
                ),
                vol.Optional("assets", default=[]): vol.All(cv.ensure_list, [ASSET_SCHEMA]),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _normalize_change_windows(value: Any) -> list[int]:
    if value is None:
        return list(CHANGE_WINDOWS_DAYS)

    if isinstance(value, (int, float, str)):
        value = [value]

    if not isinstance(value, (list, tuple)):
        return list(CHANGE_WINDOWS_DAYS)

    out: list[int] = []
    seen: set[int] = set()
    for v in value:
        try:
            n = int(v)
        except (TypeError, ValueError):
            continue
        if n <= 0:
            continue
        if n in seen:
            continue
        seen.add(n)
        out.append(n)

    return out if out else list(CHANGE_WINDOWS_DAYS)


def _normalize_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {
            "update_interval": DEFAULT_UPDATE_INTERVAL,
            "value_multiplier": float(DEFAULT_VALUE_MULTIPLIER),
            "change_windows_days": list(CHANGE_WINDOWS_DAYS),
            "assets": [],
        }

    try:
        update_interval = int(data.get("update_interval", DEFAULT_UPDATE_INTERVAL))
    except (TypeError, ValueError):
        update_interval = DEFAULT_UPDATE_INTERVAL

    try:
        value_multiplier = float(data.get("value_multiplier", DEFAULT_VALUE_MULTIPLIER))
    except (TypeError, ValueError):
        value_multiplier = float(DEFAULT_VALUE_MULTIPLIER)

    change_windows_days = _normalize_change_windows(data.get("change_windows_days", list(CHANGE_WINDOWS_DAYS)))

    assets_in = data.get("assets", [])
    assets_out: list[dict[str, Any]] = []

    if isinstance(assets_in, list):
        for a in assets_in:
            if not isinstance(a, dict):
                continue
            assets_out.append(
                {
                    "asset_id": a.get("asset_id"),
                    "name": a.get("name"),
                    "source": a.get("source"),
                    "kind": a.get("kind"),
                    "instrument": a.get("instrument"),
                    "amount_unit": a.get("amount_unit", ""),
                    "mic": a.get("mic", "XETR"),
                }
            )

    return {
        "update_interval": update_interval,
        "value_multiplier": value_multiplier,
        "change_windows_days": change_windows_days,
        "assets": assets_out,
    }


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.data.setdefault(DOMAIN, {})

    if DOMAIN not in config:
        return True

    normalized = _normalize_config(config[DOMAIN])

    entries = hass.config_entries.async_entries(DOMAIN)
    if entries:
        entry = entries[0]

        if dict(entry.data) != normalized:
            hass.config_entries.async_update_entry(entry, data=normalized)

            async def _reload_after_start(_event) -> None:
                await hass.config_entries.async_reload(entry.entry_id)

            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _reload_after_start)

        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_IMPORT},
            data=normalized,
        )
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = PortfolioDataCoordinator(hass=hass, entry_data=dict(entry.data))
    coordinator.set_assets_from_entry_data(dict(entry.data))
    entry.runtime_data = coordinator

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok
