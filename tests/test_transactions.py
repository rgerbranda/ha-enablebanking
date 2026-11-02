"""Offline regression tests; small HA boundary doubles need no HA installation.

Run with python -m unittest discover -s tests -v.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "enablebanking"


class Generic:
    def __class_getitem__(cls, item):
        return cls


@dataclass(frozen=True, kw_only=True)
class Description:
    key: str
    translation_key: str | None = None
    native_unit_of_measurement: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    suggested_display_precision: int | None = None
    icon: str | None = None


class Entity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


def load_modules():
    """Import real production modules with only the HA boundary substituted."""
    modules = {}

    def stub(name, **attrs):
        module = types.ModuleType(name)
        module.__dict__.update(attrs)
        modules[name] = module
        return module

    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
        "homeassistant.util",
        "eb_test",
    ):
        stub(name).__path__ = []
    stub("homeassistant.components.persistent_notification")
    stub("homeassistant.config_entries", ConfigEntry=Generic)
    stub("homeassistant.core", CALLBACK_TYPE=object, HomeAssistant=object, callback=lambda f: f)
    stub("homeassistant.helpers.event", async_track_time_change=Mock())
    stub("homeassistant.helpers.storage", Store=Generic)
    stub(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=Generic,
        CoordinatorEntity=type("CoordinatorEntity", (Entity, Generic), {}),
    )
    stub("homeassistant.util.dt", utcnow=lambda: datetime.now(UTC))
    stub("homeassistant.exceptions", HomeAssistantError=Exception)
    stub("homeassistant.const", CURRENCY_EURO="EUR")
    stub(
        "homeassistant.components.sensor",
        SensorDeviceClass=types.SimpleNamespace(MONETARY="monetary"),
        SensorStateClass=types.SimpleNamespace(TOTAL="total"),
        SensorEntity=object,
        SensorEntityDescription=Description,
    )
    stub("homeassistant.helpers.entity_registry")
    stub("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    stub("homeassistant.helpers.typing", StateType=object)
    stub(
        "homeassistant.helpers.device_registry",
        DeviceEntryType=types.SimpleNamespace(SERVICE="service"),
        DeviceInfo=dict,
    )
    stub("homeassistant.helpers.entity", EntityDescription=Description)
    modules["homeassistant.util"].slugify = lambda s: s.lower()
    stub("eb_test.jwt_helper", jwt_seconds_remaining=lambda _: 9999, mint_jwt=Mock())
    loaded = {}
    with patch.dict(sys.modules, modules):
        for name in (
            "const",
            "errors",
            "models",
            "transactions",
            "api",
            "coordinator",
            "entity",
            "sensor",
        ):
            spec = importlib.util.spec_from_file_location(f"eb_test.{name}", ROOT / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            loaded[name] = module
    return loaded


M = load_modules()
NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)


def row(ref="a", amount="12.34", **extra):
    return {
        "status": "BOOK",
        "entry_reference": ref,
        "transaction_id": "transient",
        "transaction_amount": {"amount": amount, "currency": "EUR"},
        "booking_date": "2026-11-01",
        "credit_debit_indicator": "DBIT",
        "creditor": {"name": "Shop"},
        "remittance_information": ["Payment", "shop"],
        **extra,
    }


def account():
    return M["models"].AccountBalance(
        "uid", "stable", "BE123", "Fintro", None, "EUR", 100, None, None
    )


def coordinator(cached=None):
    c = object.__new__(M["coordinator"].EnableBankingCoordinator)
    c.config_entry = types.SimpleNamespace(entry_id="entry", data={})
    c.hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_fire=Mock()))
    c.client = types.SimpleNamespace(
        async_get_all_balances=AsyncMock(return_value=({"stable": account()}, set())),
        async_get_account_transactions=AsyncMock(return_value=[row()]),
    )
    c._cached = {"stable": cached} if cached else {}
    c._legacy_by_uid = {}
    c._store = types.SimpleNamespace(async_save=AsyncMock())
    c._async_maybe_renew_jwt = AsyncMock()
    c._maybe_warn_expiry = Mock()
    c.last_error = ""
    c.last_refresh = None
    return c


class TransactionTests(unittest.IsolatedAsyncioTestCase):
    def test_normalisation_and_unstable_id(self):
        normalise = M["transactions"].normalise_transactions
        item = normalise([row()])[0]
        self.assertEqual(item["amount"], -12.34)
        self.assertEqual(item["counterparty"], "Shop")
        self.assertEqual(item["description"], "Payment shop")
        a = normalise([row(None)])[0]
        b = normalise([row(None, transaction_id="changed")])[0]
        self.assertEqual(a["id"], b["id"])
        credit = normalise([row(credit_debit_indicator="CRDT", debtor={"name": "Employer"})])[0]
        self.assertEqual(credit["amount"], 12.34)
        self.assertEqual(credit["counterparty"], "Employer")
        self.assertEqual(normalise([row(status="PDNG"), row(amount="NaN"), row(amount="bad")]), [])

    async def test_pagination_empty_page_and_repeated_key(self):
        client = object.__new__(M["api"].EnableBankingClient)
        client._request = AsyncMock(
            side_effect=[
                {"transactions": [], "continuation_key": "next"},
                {"transactions": [row()]},
            ]
        )
        self.assertEqual(len(await client.async_get_account_transactions("uid", "2026-01-01")), 1)
        self.assertEqual(
            client._request.call_args.kwargs["params"],
            {"date_from": "2026-01-01", "continuation_key": "next"},
        )
        client._request = AsyncMock(return_value={"transactions": [], "continuation_key": "loop"})
        with self.assertRaises(M["errors"].EnableBankingAPIError):
            await client.async_get_account_transactions("uid")

    async def test_baseline_restart_new_events_and_persist_before_fire(self):
        c = coordinator()
        await c._async_update_data()
        c.hass.bus.async_fire.assert_not_called()
        saved = c._store.async_save.call_args.args[0]
        restored = M["coordinator"]._balance_from_stored(saved["accounts"]["stable"])
        restarted = coordinator(restored)
        restarted.client.async_get_account_transactions.return_value = [
            row(transaction_id="rotated")
        ]
        await restarted._async_update_data()
        restarted.hass.bus.async_fire.assert_not_called()
        restarted.client.async_get_account_transactions.return_value = [row("new"), row()]

        def assert_persisted(*args):
            stored = restarted._store.async_save.call_args.args[0]
            self.assertIn("new", stored["accounts"]["stable"]["seen_transaction_ids"])

        restarted.hass.bus.async_fire.side_effect = assert_persisted
        await restarted._async_update_data()
        restarted.hass.bus.async_fire.assert_called_once()
        event = restarted.hass.bus.async_fire.call_args.args
        self.assertEqual(event[0], "enablebanking_new_transaction")
        self.assertEqual(event[1]["amount"], -12.34)
        await restarted._async_update_data()
        restarted.hass.bus.async_fire.assert_called_once()

    async def test_failure_retains_transactions_and_recovery_not_baseline(self):
        c = coordinator()
        c.client.async_get_account_transactions.side_effect = M[
            "errors"
        ].EnableBankingRateLimitError()
        await c._async_update_data()
        self.assertFalse(c._cached["stable"].transactions_initialized)
        c.client.async_get_account_transactions.side_effect = None
        await c._async_update_data()
        c.hass.bus.async_fire.assert_not_called()
        c.client.async_get_account_transactions.side_effect = M[
            "errors"
        ].EnableBankingConnectionError()
        await c._async_update_data()
        self.assertEqual(c._cached["stable"].transactions[0]["id"], "a")
        self.assertEqual(c._cached["stable"].balance, 100)

    async def test_ten_latest_empty_window_and_sensor(self):
        c = coordinator()
        c.client.async_get_account_transactions.return_value = [
            row(str(i), booking_date=f"2026-10-{i:02}") for i in range(1, 16)
        ]
        await c._async_update_data()
        self.assertEqual(len(c._cached["stable"].transactions), 10)
        c.client.async_get_account_transactions.return_value = []
        c.data = await c._async_update_data()
        self.assertEqual(c._cached["stable"].transactions[0]["id"], "15")
        sensor = M["sensor"].EnableBankingTransactionSensor(c, "stable")
        self.assertEqual(sensor.native_value, -12.34)
        self.assertEqual(sensor.native_unit_of_measurement, "EUR")
        self.assertEqual(sensor.entity_description.state_class, None)
        self.assertEqual(len(sensor.extra_state_attributes["transactions"]), 10)
        self.assertTrue(sensor.available)

    async def test_backoff_skips_transaction_request(self):
        cached = account()
        cached.rate_limited_until = datetime.now(UTC) + timedelta(hours=1)
        c = coordinator(cached)
        await c._async_update_data()
        c.client.async_get_account_transactions.assert_not_awaited()

    async def test_failed_persistence_does_not_publish(self):
        c = coordinator()
        await c._async_update_data()
        c.client.async_get_account_transactions.return_value = [row("new")]
        c._store.async_save.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            await c._async_update_data()
        c.hass.bus.async_fire.assert_not_called()

    async def test_restart_hydrates_sensor_without_a_poll(self):
        c = coordinator()
        await c._async_update_data()
        stored = c._store.async_save.call_args.args[0]
        restarted = coordinator()
        restarted._store.async_load = AsyncMock(return_value=stored)
        restarted.async_set_updated_data = lambda data: setattr(restarted, "data", data)
        await restarted.async_load_cache()
        sensor = M["sensor"].EnableBankingTransactionSensor(restarted, "stable")
        self.assertEqual(sensor.native_value, -12.34)
        restarted.client.async_get_account_transactions.assert_not_awaited()

    async def test_legacy_cache(self):
        raw = {"account_id": "uid", "balance": 100}
        restored = M["coordinator"]._balance_from_stored(raw)
        self.assertFalse(restored.transactions_initialized)
        self.assertEqual(restored.transactions, [])


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
