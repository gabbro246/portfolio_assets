from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

SIGNAL_AMOUNT_UPDATED = f"{DOMAIN}_amount_updated"


@dataclass(frozen=True, slots=True)
class AssetDef:
    asset_id: str
    name: str
    kind: str
    amount_unit: str


def _asset_defs_from_entry(entry: ConfigEntry) -> list[AssetDef]:
    data = entry.data or {}
    assets = data.get("assets", [])
    if not isinstance(assets, list):
        return []

    out: list[AssetDef] = []
    for raw in assets:
        if not isinstance(raw, dict):
            continue

        asset_id = str(raw.get("asset_id", "")).strip()
        name = str(raw.get("name", "")).strip()
        kind = str(raw.get("kind", "asset")).strip()
        amount_unit = str(raw.get("amount_unit", "")).strip()

        if not asset_id or not name:
            continue

        out.append(
            AssetDef(
                asset_id=asset_id,
                name=name,
                kind=kind,
                amount_unit=amount_unit,
            )
        )

    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    entities: list[PortfolioAmountNumber] = []
    for a in _asset_defs_from_entry(entry):
        entities.append(PortfolioAmountNumber(coordinator=coordinator, asset=a))

    async_add_entities(entities)


class PortfolioAmountNumber(RestoreEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_icon = "mdi:counter"

    _attr_native_min_value = 0.0
    _attr_native_max_value = 1_000_000_000.0
    _attr_native_step = 0.0001

    def __init__(self, coordinator: Any, asset: AssetDef) -> None:
        self._coordinator = coordinator
        self._asset = asset

        object_id = f"{asset.kind}_{asset.asset_id}_amount"

        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = "Amount"

        self._attr_suggested_object_id = object_id
        self.entity_id = f"number.{object_id}"

        self._attr_native_unit_of_measurement = asset.amount_unit or None

        # Show 0 in the UI initially, but do not push it into coordinator.amounts yet
        # That avoids value sensors calculating 0 during startup
        self._attr_native_value = 0.0

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, asset.asset_id)},
            name=asset.name,
            manufacturer="Portfolio Assets",
            model=asset.kind,
        )

        self._ensure_amount_store()

    def _ensure_amount_store(self) -> None:
        if not hasattr(self._coordinator, "amounts"):
            setattr(self._coordinator, "amounts", {})

        amounts = getattr(self._coordinator, "amounts")
        if not isinstance(amounts, dict):
            setattr(self._coordinator, "amounts", {})

    def _set_amount(self, value: float) -> None:
        amounts: dict[str, float] = getattr(self._coordinator, "amounts")
        amounts[self._asset.asset_id] = float(value)

    def _clamp(self, value: float) -> float:
        if value < float(self._attr_native_min_value):
            return float(self._attr_native_min_value)
        if value > float(self._attr_native_max_value):
            return float(self._attr_native_max_value)
        return float(value)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                value = float(last_state.state)
            except (TypeError, ValueError):
                value = 0.0
        else:
            value = 0.0

        value = self._clamp(value)

        self._attr_native_value = value
        self._set_amount(value)

        async_dispatcher_send(self.hass, SIGNAL_AMOUNT_UPDATED, self._asset.asset_id)
        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        value = self._clamp(float(value))

        self._attr_native_value = value
        self._set_amount(value)

        async_dispatcher_send(self.hass, SIGNAL_AMOUNT_UPDATED, self._asset.asset_id)
        self.async_write_ha_state()
