"""Transaction event entities for Enable Banking accounts."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import EnableBankingConfigEntry, EnableBankingCoordinator
from .entity import account_unique_id, bank_device_info

PARALLEL_UPDATES = 0
EVENT_TRANSACTION = "transaction"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnableBankingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Discover account entities at startup and on subsequent balance polls."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_accounts() -> None:
        stable_ids = coordinator.cached_stable_ids()
        if coordinator.data is not None:
            stable_ids.update(coordinator.data.accounts)
        entities: list[EnableBankingTransactionEvent] = []
        registry = er.async_get(hass)
        for stable_id in sorted(stable_ids - known):
            # Retire only the sensor created by the previous implementation.
            # Event entities use another domain and therefore cannot reuse it.
            old_unique = account_unique_id(entry.entry_id, stable_id, "last_transaction")
            old_entity_id = registry.async_get_entity_id("sensor", DOMAIN, old_unique)
            if old_entity_id:
                registry.async_remove(old_entity_id)
            entities.append(EnableBankingTransactionEvent(coordinator, stable_id))
            known.add(stable_id)
        if entities:
            async_add_entities(entities)

    _add_accounts()
    entry.async_on_unload(coordinator.async_add_listener(_add_accounts))


class EnableBankingTransactionEvent(EventEntity):
    """Publish one booked transaction at a time, with no rolling history."""

    _attr_has_entity_name = True
    _attr_attribution = "Data via Enable Banking AIS (PSD2)"
    _attr_translation_key = "transaction"
    _attr_icon = "mdi:bank-transfer"

    def __init__(self, coordinator: EnableBankingCoordinator, stable_id: str) -> None:
        """Keep the account identity stable across bank reauthorisations."""
        self._attr_event_types = [EVENT_TRANSACTION]
        self._entry_id = coordinator.config_entry.entry_id
        self._stable_id = stable_id
        self._attr_unique_id = account_unique_id(self._entry_id, stable_id, "transaction")
        self._attr_device_info = bank_device_info(coordinator.config_entry)
        account = coordinator.cached_account(stable_id)
        if account is None and coordinator.data is not None:
            account = coordinator.data.accounts.get(stable_id)
        label = (account.iban or account.name) if account else stable_id
        self._attr_translation_placeholders = {"account": label}

    async def async_added_to_hass(self) -> None:
        """Restore the last event, then subscribe until the entity is removed."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.hass.bus.async_listen(f"{DOMAIN}_new_transaction", self._handle_transaction)
        )

    @callback
    def _handle_transaction(self, event: Event) -> None:
        """Ignore other accounts and write only on a new transaction event."""
        if (
            event.data.get("entry_id") != self._entry_id
            or event.data.get("stable_id") != self._stable_id
        ):
            return
        self._trigger_event(EVENT_TRANSACTION, dict(event.data))
        self.async_write_ha_state()
