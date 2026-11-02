"""Normalise booked transactions and identify them across sessions."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any


def normalise_transactions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return booked entries newest first; never identify by transient transaction_id."""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("status") != "BOOK":
            continue
        amount = row.get("transaction_amount")
        if not isinstance(amount, dict):
            continue
        try:
            value = float(amount["amount"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        indicator = row.get("credit_debit_indicator")
        if indicator not in ("DBIT", "CRDT"):
            continue
        value = -abs(value) if indicator == "DBIT" else abs(value)
        party = row.get("creditor" if indicator == "DBIT" else "debtor")
        party_account = row.get("creditor_account" if indicator == "DBIT" else "debtor_account")
        description = row.get("remittance_information", [])
        if isinstance(description, list):
            description = " ".join(str(part) for part in description)
        entry: dict[str, Any] = {
            "amount": value,
            "currency": amount.get("currency"),
            "date": row.get("booking_date") or row.get("value_date"),
            "value_date": row.get("value_date"),
            "description": str(description or ""),
            "counterparty": party.get("name") if isinstance(party, dict) else party,
            "counterparty_account": party_account,
            "entry_reference": row.get("entry_reference"),
        }
        # Some banks omit entry_reference. A content fingerprint is best effort:
        # indistinguishable same-day entries cannot reliably be told apart.
        identity = (
            entry["entry_reference"]
            or hashlib.sha256(
                json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
        )
        entry["id"] = str(identity)
        entry["transaction_id"] = row.get("transaction_id")
        result[entry["id"]] = entry
    return sorted(result.values(), key=lambda item: (item["date"] or "", item["id"]), reverse=True)
