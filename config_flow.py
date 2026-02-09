from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries

from .const import DOMAIN


def _normalize_assets(data: dict) -> dict:
    update_interval = int(data.get("update_interval", 1800))
    assets = data.get("assets", [])

    norm_assets: list[dict] = []
    if isinstance(assets, list):
        for a in assets:
            if not isinstance(a, dict):
                continue
            norm_assets.append(
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

    return {"update_interval": update_interval, "assets": norm_assets}


class PortfolioAssetsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({}),
            )

        return self.async_create_entry(title="Portfolio Assets", data={"update_interval": 1800, "assets": []})

    async def async_step_import(self, user_input):
        await self.async_set_unique_id(DOMAIN)

        normalized = _normalize_assets(user_input or {})

        current_entries = self._async_current_entries()
        if current_entries:
            entry = current_entries[0]
            self.hass.config_entries.async_update_entry(entry, data=normalized)
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="already_configured")

        return self.async_create_entry(title="Portfolio Assets", data=normalized)
