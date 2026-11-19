"""Deterministic offline settlement fixtures built on the gateway fixture.

These helpers only mutate a freshly generated local workdir; they never touch
the network or the source tree.  Positive and negative variants share the same
O100/M100/USD identity so settlement tests can exercise authorization,
clearing, and evidence anomalies on identical inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lakemirror.gateway_fixture import generate_gateway_fixture
from lakemirror.util import write_json

EARLY = "2024-03-01T10:00:00Z"
LATE = "2024-03-01T13:00:00Z"


def positive_workspace(tmp: str | Path) -> dict[str, str]:
    """advance_then_dispute: G100 visible at 10:00, G101 chargeback at 11:00."""
    root = Path(tmp).resolve() / "settle-positive"
    return generate_gateway_fixture("advance_then_dispute", root)


def negative_gateway_workspace(tmp: str | Path, scenario: str) -> dict[str, str]:
    """Invalid gateway evidence variants (duplicate receipt / bad parent)."""
    root = Path(tmp).resolve() / f"settle-gateway-{scenario}"
    return generate_gateway_fixture(scenario, root)


def set_loss_bearer(fixture: dict[str, str], contract_id: str, bearer: str) -> None:
    path = Path(fixture["terms"])
    terms = json.loads(path.read_text(encoding="utf-8"))
    for contract in terms["contracts"]:
        if contract["id"] == contract_id:
            contract["loss_bearer"] = bearer
    write_json(path, terms)


def corrupt_primary_events(fixture: dict[str, str]) -> Path:
    target = next((Path(fixture["workdir"]) / "lake" / "primary" / "tables" / "events").rglob("*.jsonl"))
    target.write_text("{broken jsonl\n", encoding="utf-8")
    return target


def corrupt_replica_events(fixture: dict[str, str]) -> Path:
    target = next((Path(fixture["workdir"]) / "lake" / "replica" / "tables" / "events").rglob("*.jsonl"))
    target.write_text("{broken jsonl\n", encoding="utf-8")
    return target


def ledger_doc(fixture: dict[str, str]) -> dict[str, Any]:
    path = Path(fixture["workdir"]) / "settlement_ledger.json"
    return json.loads(path.read_text(encoding="utf-8"))
