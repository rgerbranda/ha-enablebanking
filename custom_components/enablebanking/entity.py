"""Base entity for the Enable Banking integration."""

from __future__ import annotations

import hashlib

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ASPSP_COUNTRY, CONF_ASPSP_NAME, CONF_PSU_TYPE, DOMAIN
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator


def account_unique_id(entry_id: str, stable_id: str, key: str) -> str:
    """Build a stable per-account entity unique_id.

    ``stable_id`` (Enable Banking's ``identification_hash``) can contain ``/``,
    ``+`` and ``=``; hash it to a compact hex token so the unique_id is clean
    and stays identical across sessions. Both entity creation and the one-time
    migration in ``sensor.py`` must use this helper so their ids agree.
    """
    token = hashlib.sha256(stable_id.encode()).hexdigest()[:16]
    return f"{entry_id}_{token}_{key}"


class EnableBankingEntity(CoordinatorEntity[EnableBankingCoordinator]):
    """Base entity for Enable Banking sensors."""

    _attr_has_entity_name = True
    _attr_attribution = "Data via Enable Banking AIS (PSD2)"

    def __init__(
        self,
        coordinator: EnableBankingCoordinator,
        description: EntityDescription,
        stable_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._stable_id = stable_id
        self._attr_unique_id = account_unique_id(
            coordinator.config_entry.entry_id, stable_id, description.key
        )

        self._attr_device_info = bank_device_info(coordinator.config_entry)


def bank_device_info(entry: EnableBankingConfigEntry) -> DeviceInfo:
    """Use the same bank device for balance sensors and transaction events."""
    aspsp_name = entry.data.get(CONF_ASPSP_NAME, "Enable Banking")
    country = entry.data.get(CONF_ASPSP_COUNTRY, "")
    psu_type = entry.data.get(CONF_PSU_TYPE, "")
    model_parts = [p for p in (country, psu_type) if p]
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=aspsp_name,
        manufacturer=aspsp_name,
        model=" · ".join(model_parts) if model_parts else "Account",
        entry_type=DeviceEntryType.SERVICE,
    )
