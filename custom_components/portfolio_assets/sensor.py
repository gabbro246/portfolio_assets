from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CHANGE_WINDOWS_DAYS, DOMAIN

SIGNAL_AMOUNT_UPDATED = f"{DOMAIN}_amount_updated"

_CHANGE_BASELINE_CACHE_TTL = timedelta(minutes=30)


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


def _value_entity_id_for_asset(asset: AssetDef) -> str:
    return f"sensor.{asset.kind}_{asset.asset_id}_value"


def _value_entity_id_for_group(group_kind: str) -> str:
    if group_kind == "crypto":
        return "sensor.portfolio_crypto_value"
    if group_kind == "etf":
        return "sensor.portfolio_etf_value"
    if group_kind == "fund":
        return "sensor.portfolio_fund_value"
    return f"sensor.portfolio_{group_kind}_value"


def _value_entity_id_for_total() -> str:
    return "sensor.portfolio_total_value"


def _currency_from_coordinator(coordinator: Any) -> str:
    currency = getattr(coordinator, "currency", None)
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()

    hass = getattr(coordinator, "hass", None)
    config = getattr(hass, "config", None)
    currency = getattr(config, "currency", None)
    if isinstance(currency, str) and currency.strip():
        return currency.strip().upper()

    return "EUR"


def _get_windows_from_coordinator(coordinator: Any) -> tuple[int, ...]:
    value = getattr(coordinator, "change_windows_days", None)
    if isinstance(value, tuple) and all(isinstance(x, int) and x > 0 for x in value):
        return value
    if isinstance(value, list):
        out: list[int] = []
        seen: set[int] = set()
        for v in value:
            if not isinstance(v, int) or v <= 0 or v in seen:
                continue
            seen.add(v)
            out.append(v)
        if out:
            return tuple(out)
    return tuple(CHANGE_WINDOWS_DAYS)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    assets = _asset_defs_from_entry(entry)
    windows = _get_windows_from_coordinator(coordinator)

    statistic_ids: set[str] = {_value_entity_id_for_asset(a) for a in assets}
    statistic_ids |= {
        "sensor.portfolio_crypto_value",
        "sensor.portfolio_etf_value",
        "sensor.portfolio_fund_value",
        "sensor.portfolio_total_value",
    }

    setattr(coordinator, "_change_statistic_ids", statistic_ids)
    if not hasattr(coordinator, "_change_baseline_cache"):
        setattr(coordinator, "_change_baseline_cache", {})

    entities: list[SensorEntity] = []
    for a in assets:
        entities.append(PortfolioPriceSensor(coordinator=coordinator, asset=a))
        entities.append(PortfolioValueSensor(coordinator=coordinator, asset=a))

    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="crypto"))
    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="etf"))
    entities.append(PortfolioGroupValueSensor(coordinator=coordinator, assets=assets, group_kind="fund"))
    entities.append(PortfolioTotalValueSensor(coordinator=coordinator, assets=assets))

    for a in assets:
        for days in windows:
            entities.append(PortfolioAssetChangeSensor(coordinator=coordinator, asset=a, window_days=days))
            entities.append(PortfolioAssetDeltaSensor(coordinator=coordinator, asset=a, window_days=days))

    for group_kind in ("crypto", "etf", "fund"):
        for days in windows:
            entities.append(
                PortfolioGroupChangeSensor(
                    coordinator=coordinator,
                    assets=assets,
                    group_kind=group_kind,
                    window_days=days,
                )
            )
            entities.append(
                PortfolioGroupDeltaSensor(
                    coordinator=coordinator,
                    assets=assets,
                    group_kind=group_kind,
                    window_days=days,
                )
            )

    for days in windows:
        entities.append(PortfolioTotalChangeSensor(coordinator=coordinator, assets=assets, window_days=days))
        entities.append(PortfolioTotalDeltaSensor(coordinator=coordinator, assets=assets, window_days=days))

    async_add_entities(entities)


async def _async_get_day_mean_for_statistic_id(
    hass: HomeAssistant,
    coordinator: Any,
    statistic_id: str,
    window_days: int,
) -> float | None:
    cache: dict[tuple[int, str], tuple[Any, dict[str, float]]] = getattr(coordinator, "_change_baseline_cache", {})
    all_ids: set[str] = getattr(coordinator, "_change_statistic_ids", {statistic_id})

    now_local = dt_util.now()
    day_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=window_days)

    key = (window_days, day_start_local.date().isoformat())
    cached = cache.get(key)

    if cached is not None:
        fetched_at, values = cached
        try:
            age = dt_util.utcnow() - fetched_at
        except Exception:
            age = _CHANGE_BASELINE_CACHE_TTL + timedelta(seconds=1)

        if age <= _CHANGE_BASELINE_CACHE_TTL and statistic_id in values:
            return values.get(statistic_id)

    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        day_start_local,
        day_start_local,
        set(all_ids),
        "day",
        None,
        {"mean"},
    )

    values: dict[str, float] = {}
    if isinstance(stats, dict):
        for sid, rows in stats.items():
            if not isinstance(rows, list) or not rows:
                continue
            row0 = rows[0]
            if not isinstance(row0, dict):
                continue
            mean = row0.get("mean")
            if mean is None:
                continue
            try:
                values[sid] = float(mean)
            except (TypeError, ValueError):
                continue

    cache[key] = (dt_util.utcnow(), values)
    setattr(coordinator, "_change_baseline_cache", cache)

    return values.get(statistic_id)


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
            price = float(self._get_price(a.asset_id))
            amount = float(self._get_amount_optional(a.asset_id))
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
            self._attr_icon = "mdi:bank"
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


class PortfolioAssetChangeSensor(_PortfolioBaseSensor):
    _attr_icon = "mdi:trending-up"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = None

    def __init__(self, coordinator: Any, asset: AssetDef, window_days: int) -> None:
        super().__init__(coordinator, asset)
        self._window_days = int(window_days)

        object_id = f"{asset.kind}_{asset.asset_id}_change_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = f"Change {self._window_days}d"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

        self._target_statistic_id = _value_entity_id_for_asset(asset)
        self._unsub_amount: Any = None
        self._attr_native_value: float | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_amount = async_dispatcher_connect(
            self.hass,
            SIGNAL_AMOUNT_UPDATED,
            self._handle_amount_updated,
        )
        self.hass.async_create_task(self._async_recompute())

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_amount is not None:
            self._unsub_amount()
            self._unsub_amount = None
        await super().async_will_remove_from_hass()

    @callback
    def _handle_amount_updated(self, asset_id: str) -> None:
        if asset_id != self._asset.asset_id:
            return
        self.hass.async_create_task(self._async_recompute())

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_recompute())

    async def _async_recompute(self) -> None:
        current_value = self._current_value()
        if current_value is None:
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        baseline = await _async_get_day_mean_for_statistic_id(
            self.hass,
            self.coordinator,
            self._target_statistic_id,
            self._window_days,
        )

        if baseline is None or not self._baseline_is_usable(baseline):
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        self._attr_native_value = self._calculate_period_value(current_value, baseline)
        self.async_write_ha_state()

    def _baseline_is_usable(self, baseline: float) -> bool:
        return baseline != 0

    def _calculate_period_value(self, current_value: float, baseline: float) -> float:
        return round((current_value - baseline) / baseline * 100.0, 2)

    def _current_value(self) -> float | None:
        price = self._get_price()
        if price is None:
            return None
        amount = self._get_amount_optional()
        if amount is None:
            return None
        m = self._get_value_multiplier()
        return float(round(price * amount * m, 2))

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "asset_id": self._asset.asset_id,
            "kind": self._asset.kind,
            "window_days": self._window_days,
            "baseline_statistic_id": self._target_statistic_id,
        }


class PortfolioAssetDeltaSensor(PortfolioAssetChangeSensor):
    _attr_entity_registry_enabled_default = True
    _attr_icon = "mdi:delta"

    def __init__(self, coordinator: Any, asset: AssetDef, window_days: int) -> None:
        super().__init__(coordinator, asset, window_days)

        object_id = f"{asset.kind}_{asset.asset_id}_delta_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = f"Delta {self._window_days}d"
        self._attr_suggested_object_id = object_id
        self._attr_native_unit_of_measurement = _currency_from_coordinator(coordinator)
        self.entity_id = f"sensor.{object_id}"

    def _baseline_is_usable(self, baseline: float) -> bool:
        return True

    def _calculate_period_value(self, current_value: float, baseline: float) -> float:
        return round(current_value - baseline, 2)


class _PortfolioChangeBase(_PortfolioTotalsBase):
    _attr_icon = "mdi:trending-up"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = None

    def __init__(self, coordinator: Any, assets: list[AssetDef], window_days: int, target_statistic_id: str) -> None:
        super().__init__(coordinator, assets)
        self._window_days = int(window_days)
        self._target_statistic_id = target_statistic_id
        self._attr_native_value: float | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        self.hass.async_create_task(self._async_recompute())

    @callback
    def _handle_any_amount_updated(self, asset_id: str) -> None:
        self.hass.async_create_task(self._async_recompute())

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_recompute())

    async def _async_recompute(self) -> None:
        current_value, _attrs = self._calc_total(self._assets)
        if current_value is None:
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        baseline = await _async_get_day_mean_for_statistic_id(
            self.hass,
            self.coordinator,
            self._target_statistic_id,
            self._window_days,
        )

        if baseline is None or not self._baseline_is_usable(baseline):
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        self._attr_native_value = self._calculate_period_value(current_value, baseline)
        self.async_write_ha_state()

    def _baseline_is_usable(self, baseline: float) -> bool:
        return baseline != 0

    def _calculate_period_value(self, current_value: float, baseline: float) -> float:
        return round((current_value - baseline) / baseline * 100.0, 2)

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "window_days": self._window_days,
            "baseline_statistic_id": self._target_statistic_id,
        }


class PortfolioGroupChangeSensor(_PortfolioChangeBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef], group_kind: str, window_days: int) -> None:
        self._group_kind = group_kind
        group_assets = [a for a in assets if a.kind == group_kind]

        target_statistic_id = _value_entity_id_for_group(group_kind)
        super().__init__(coordinator, group_assets, window_days, target_statistic_id)

        object_id = f"portfolio_{group_kind}_change_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

        if group_kind == "crypto":
            self._attr_name = f"Crypto Change {self._window_days}d"
        elif group_kind == "etf":
            self._attr_name = f"ETF Change {self._window_days}d"
        elif group_kind == "fund":
            self._attr_name = f"Fund Change {self._window_days}d"
        else:
            self._attr_name = f"{group_kind} Change {self._window_days}d"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["group_kind"] = self._group_kind
        return attrs


class PortfolioTotalChangeSensor(_PortfolioChangeBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef], window_days: int) -> None:
        target_statistic_id = _value_entity_id_for_total()
        super().__init__(coordinator, assets, window_days, target_statistic_id)

        object_id = f"portfolio_total_change_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = f"Total Change {self._window_days}d"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"


class _PortfolioDeltaBase(_PortfolioChangeBase):
    _attr_entity_registry_enabled_default = True
    _attr_icon = "mdi:delta"

    def __init__(self, coordinator: Any, assets: list[AssetDef], window_days: int, target_statistic_id: str) -> None:
        super().__init__(coordinator, assets, window_days, target_statistic_id)
        self._attr_native_unit_of_measurement = _currency_from_coordinator(coordinator)

    def _baseline_is_usable(self, baseline: float) -> bool:
        return True

    def _calculate_period_value(self, current_value: float, baseline: float) -> float:
        return round(current_value - baseline, 2)


class PortfolioGroupDeltaSensor(_PortfolioDeltaBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef], group_kind: str, window_days: int) -> None:
        self._group_kind = group_kind
        group_assets = [a for a in assets if a.kind == group_kind]

        target_statistic_id = _value_entity_id_for_group(group_kind)
        super().__init__(coordinator, group_assets, window_days, target_statistic_id)

        object_id = f"portfolio_{group_kind}_delta_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"

        if group_kind == "crypto":
            self._attr_name = f"Crypto Delta {self._window_days}d"
        elif group_kind == "etf":
            self._attr_name = f"ETF Delta {self._window_days}d"
        elif group_kind == "fund":
            self._attr_name = f"Fund Delta {self._window_days}d"
        else:
            self._attr_name = f"{group_kind} Delta {self._window_days}d"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes
        attrs["group_kind"] = self._group_kind
        return attrs


class PortfolioTotalDeltaSensor(_PortfolioDeltaBase):
    def __init__(self, coordinator: Any, assets: list[AssetDef], window_days: int) -> None:
        target_statistic_id = _value_entity_id_for_total()
        super().__init__(coordinator, assets, window_days, target_statistic_id)

        object_id = f"portfolio_total_delta_{self._window_days}d"
        self._attr_unique_id = f"{DOMAIN}_{object_id}"
        self._attr_name = f"Total Delta {self._window_days}d"
        self._attr_suggested_object_id = object_id
        self.entity_id = f"sensor.{object_id}"
