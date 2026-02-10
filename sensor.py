from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

SIGNAL_AMOUNT_UPDATED = f"{DOMAIN}_amount_updated"


@dataclass(frozen=True, slots=True)
class AssetDef:
    asset_id: str
    name: str
    kind: str
    source: str
    instrument: str
    mic: str


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
        source = str(raw.get("source", "")).strip()
        instrument = str(raw.get("instrument", "")).strip()
        mic = str(raw.get("mic", "XETR")).strip()

        if not asset_id or not name or not source or not instrument:
            continue

        out.append(
            AssetDef(
                asset_id=asset_id,
                name=name,
                kind=kind,
                source=source,
                instrument=instrument,
                mic=mic,
            )
        )

    return out


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    assets = _asset_defs_from_entry(entry)

    entities: list[SensorEntity] = []
    for a in assets:
        entities.append(PortfolioPriceSensor(coordinator=coordinator, asset=a))
        entities.append(PortfolioValueSensor(coordinator=coordinator, asset=a))

    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="crypto"))
    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="etf"))
    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="fund"))
    entities.append(PortfolioTotalValueSensor(coordinator=coordinator, assets=assets))

    async_add_entities(entities)


class _PortfolioBaseSensor(CoordinatorEntity, SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR"

    def __init__(self, coordinator: Any, asset: AssetDef) -> None:
        super().__init__(coordinator)
        self._asset = asset

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, asset.asset_id)},
            name=asset.name,
            manufacturer="Portfolio Assets",
            model=asset.kind,
        )

    def _row(self) -> dict[str, Any] | None:
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return None
        row = data.get(self._asset.asset_id)
        return row if isinstance(row, dict) else None

    def _get_price(self) -> float | None:
        row = self._row()
        if not row:
            return None
        price = row.get("price")
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    def _get_updated_at(self) -> str | None:
        row = self._row()
        if not row:
            return None
        updated_at = row.get("updated_at")
        return str(updated_at) if updated_at is not None else None

    def _get_source(self) -> str | None:
        row = self._row()
        if not row:
            return None
        source = row.get("source")
        return str(source) if source is not None else None

    def _get_quote_url(self) -> str | None:
        row = self._row()
        if not row:
            return None
        url = row.get("quote_url")
        return str(url) if url else None

    def _amounts_dict(self) -> dict[str, float] | None:
        amounts = getattr(self.coordinator, "amounts", None)
        return amounts if isinstance(amounts, dict) else None

    def _get_amount_optional(self) -> float | None:
        amounts = self._amounts_dict()
        if not amounts:
            return None
        if self._asset.asset_id not in amounts:
            return None
        try:
            return float(amounts[self._asset.asset_id])
        except (TypeError, ValueError):
            return None

    def _get_value_multiplier(self) -> float:
        try:
            m = float(getattr(self.coordinator, "value_multiplier", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return m

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return self._get_price() is not None


class PortfolioPriceSensor(_PortfolioBaseSensor):
    _attr_icon = "mdi:tag"

    def __init__(self, coordinator: Any, asset: AssetDef) -> None:
        super().__init__(coordinator, asset)

        object_id = f"{asset.kind}_{asset.asset_id}_price"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = "Price"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

    @property
    def native_value(self) -> float | None:
        return self._get_price()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "asset_id": self._asset.asset_id,
            "kind": self._asset.kind,
            "source": self._get_source(),
            "updated_at": self._get_updated_at(),
            "instrument": self._asset.instrument,
        }
        if self._asset.source == "boerse_frankfurt":
            attrs["mic"] = self._asset.mic
        if self._asset.source == "wienerboerse_oekb":
            q = self._get_quote_url()
            if q:
                attrs["quote_url"] = q
        return attrs


class PortfolioValueSensor(_PortfolioBaseSensor):
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator: Any, asset: AssetDef) -> None:
        super().__init__(coordinator, asset)

        object_id = f"{asset.kind}_{asset.asset_id}_value"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = "Value"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

        self._unsub_amount: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_amount = async_dispatcher_connect(
            self.hass,
            SIGNAL_AMOUNT_UPDATED,
            self._handle_amount_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_amount is not None:
            self._unsub_amount()
            self._unsub_amount = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_amount_updated(self, asset_id: str) -> None:
        if asset_id != self._asset.asset_id:
            return
        self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        price = self._get_price()
        if price is None:
            return None

        amount = self._get_amount_optional()
        if amount is None:
            return None

        m = self._get_value_multiplier()
        return round(price * amount * m, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "asset_id": self._asset.asset_id,
            "kind": self._asset.kind,
            "amount": self._get_amount_optional(),
            "price": self._get_price(),
            "source": self._get_source(),
            "updated_at": self._get_updated_at(),
            "instrument": self._asset.instrument,
            "quote_url": self._get_quote_url(),
        }


class _PortfolioTotalsBase(CoordinatorEntity, SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR"
    _attr_icon = "mdi:sigma"

    def __init__(self, coordinator: Any, assets: list[AssetDef]) -> None:
        super().__init__(coordinator)
        self._assets = assets
        self._unsub_amount: Any = None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "portfolio")},
            name="Portfolio",
            manufacturer="Portfolio Assets",
            model="portfolio",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_amount = async_dispatcher_connect(
            self.hass,
            SIGNAL_AMOUNT_UPDATED,
            self._handle_any_amount_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_amount is not None:
            self._unsub_amount()
            self._unsub_amount = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_any_amount_updated(self, asset_id: str) -> None:
        self.async_write_ha_state()

    def _amounts_dict(self) -> dict[str, float] | None:
        amounts = getattr(self.coordinator, "amounts", None)
        return amounts if isinstance(amounts, dict) else None

    def _get_amount_optional(self, asset_id: str) -> float | None:
        amounts = self._amounts_dict()
        if not amounts:
            return None
        if asset_id not in amounts:
            return None
        try:
            return float(amounts[asset_id])
        except (TypeError, ValueError):
            return None

    def _get_price(self, asset_id: str) -> float | None:
        data = getattr(self.coordinator, "data", None)
        if not isinstance(data, dict):
            return None

        row = data.get(asset_id)
        if not isinstance(row, dict):
            return None

        price = row.get("price")
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    def _get_value_multiplier(self) -> float:
        try:
            m = float(getattr(self.coordinator, "value_multiplier", 1.0))
        except (TypeError, ValueError):
            return 1.0
        return m

    def _calc_total(self, assets: list[AssetDef]) -> tuple[float | None, dict[str, Any]]:
        included = [a.asset_id for a in assets]
        missing_amounts: list[str] = []
        missing_prices: list[str] = []
        breakdown: dict[str, float] = {}

        if not assets:
            return None, {"included_assets": included, "missing_amounts": [], "missing_prices": [], "breakdown": {}}

        for a in assets:
            if self._get_amount_optional(a.asset_id) is None:
                missing_amounts.append(a.asset_id)

        if missing_amounts:
            return None, {
                "included_assets": included,
                "missing_amounts": missing_amounts,
                "missing_prices": [],
                "breakdown": {},
            }

        for a in assets:
            if self._get_price(a.asset_id) is None:
                missing_prices.append(a.asset_id)

        if missing_prices:
            return None, {
                "included_assets": included,
                "missing_amounts": [],
                "missing_prices": missing_prices,
                "breakdown": {},
            }

        m = self._get_value_multiplier()

        total = 0.0
        for a in assets:
            price = float(self._get_price(a.asset_id))  # safe due to check above
            amount = float(self._get_amount_optional(a.asset_id))  # safe due to check above
            value = price * amount * m
            total += value
            breakdown[a.asset_id] = round(value, 2)

        return round(total, 2), {
            "included_assets": included,
            "missing_amounts": [],
            "missing_prices": [],
            "breakdown": breakdown,
        }

    @property
    def available(self) -> bool:
        return super().available


class PortfolioGroupValueSensor(_PortfolioTotalsBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef], group_kind: str) -> None:
        self._group_kind = group_kind
        group_assets = [a for a in assets if a.kind == group_kind]
        super().__init__(coordinator, group_assets)

        if group_kind == "crypto":
            object_id = "portfolio_crypto_value"
            self._attr_name = "Crypto Value"
            self._attr_icon = "mdi:currency-btc"
        elif group_kind == "etf":
            object_id = "portfolio_etf_value"
            self._attr_name = "ETF Value"
            self._attr_icon = "mdi:chart-line"
        elif group_kind == "fund":
            object_id = "portfolio_fund_value"
            self._attr_name = "Fund Value"
            self._attr_icon = "mdi:bank-outline"
        else:
            object_id = f"portfolio_{group_kind}_value"
            self._attr_name = f"{group_kind} Value"

        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

    @property
    def native_value(self) -> float | None:
        value, _attrs = self._calc_total(self._assets)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _value, attrs = self._calc_total(self._assets)
        attrs["group_kind"] = self._group_kind
        return attrs


class PortfolioTotalValueSensor(_PortfolioTotalsBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef]) -> None:
        super().__init__(coordinator, assets)

        object_id = "portfolio_total_value"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = "Total Value"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

    @property
    def native_value(self) -> float | None:
        value, _attrs = self._calc_total(self._assets)
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        _value, attrs = self._calc_total(self._assets)
        return attrs
