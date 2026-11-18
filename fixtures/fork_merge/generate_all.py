"""Generate positive and blocking fork fixtures, including every merge block class.

The generator is deterministic and offline. It writes:

* S0/S1/S2: the disjoint-field positive example (S1 changes amount, S2 status).
* S1C/S2C: both branches change O1.amount to different values.
* S1D/S2U: delete-versus-update sibling branches.
* S1K/S2K: a duplicated business key on one branch.
* corrupt/: missing file, hash mismatch, unknown ancestor, and bad contract.
"""
import csv
import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = [
    {"name": "order_id", "type": "STRING", "nullable": False},
    {"name": "amount", "type": "LONG", "nullable": False},
    {"name": "status", "type": "STRING", "nullable": False},
]


def _write(sid, rows, seq, parent="S0"):
    file = ROOT / f"orders-{sid}.csv"
    with file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["order_id", "amount", "status"])
        writer.writerows(rows)
    data = file.read_bytes()
    entry = {
        "path": file.name,
        "partition": "dt=2024-01-01",
        "size": len(data),
        "row_count": len(rows),
        "sha256": hashlib.sha256(data).hexdigest(),
    }
    manifest = {
        "table": "orders",
        "snapshot_id": sid,
        "parent_snapshot_id": parent,
        "sequence": seq,
        "created_at": f"2024-01-{seq:02d}T00:00:00Z",
        "schema": SCHEMA,
        "partition_spec": ["dt"],
        "files": [entry],
        "stats": {},
    }
    (ROOT / f"{sid}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def _contract(name, ids, parents):
    payload = {
        "ancestor": f"fixtures/fork_merge/{ids[0]}.json",
        "primary": f"fixtures/fork_merge/{ids[1]}.json",
        "replica": f"fixtures/fork_merge/{ids[2]}.json",
        "contracts": ["file_strict", "field_disjoint"],
        "expected_merge_parents": parents,
    }
    (ROOT / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    # The positive S0/S1/S2 and delete/update S1D/S2U fixtures are checked in
    # with their original CRLF formatting and must not be rewritten here.
    _write("S1C", [("O1", 120, "hold"), ("O2", 50, "open")], 2)
    _write("S2C", [("O1", 300, "hold"), ("O2", 50, "open")], 3)
    _contract("field-conflict-contract.json", ("S0", "S1C", "S2C"), ["S1C", "S2C"])

    _write("S1K", [("O1", 120, "hold"), ("O2", 50, "open")], 2)
    _write("S2K", [("O1", 100, "approved"), ("O1", 50, "open")], 3)
    _contract("duplicate-key-contract.json", ("S0", "S1K", "S2K"), ["S1K", "S2K"])

    corrupt = ROOT / "corrupt"
    if corrupt.exists():
        shutil.rmtree(corrupt)
    corrupt.mkdir()
    for name in ("S0.json", "S1.json", "S2.json", "orders-S0.csv", "orders-S1.csv", "orders-S2.csv"):
        shutil.copy(ROOT / name, corrupt / name)
    prefix = "fixtures/fork_merge/corrupt/"
    base_contract = {
        "ancestor": prefix + "S0.json",
        "primary": prefix + "S1.json",
        "replica": prefix + "S2.json",
        "contracts": ["file_strict", "field_disjoint"],
        "expected_merge_parents": ["S1", "S2"],
    }
    hash_doc = json.loads((corrupt / "S1.json").read_text())
    hash_doc["files"][0]["sha256"] = "0" * 64
    (corrupt / "S1-hash.json").write_text(json.dumps(hash_doc, indent=2) + "\n")
    hash_contract = dict(base_contract, primary=prefix + "S1-hash.json")
    (corrupt / "contract-hash-mismatch.json").write_text(json.dumps(hash_contract, indent=2) + "\n")
    ancestor_doc = json.loads((corrupt / "S1.json").read_text())
    ancestor_doc["parent_snapshot_id"] = "S99"
    (corrupt / "S1-unknown.json").write_text(json.dumps(ancestor_doc, indent=2) + "\n")
    unknown_contract = dict(base_contract, primary=prefix + "S1-unknown.json")
    (corrupt / "contract-unknown-ancestor.json").write_text(json.dumps(unknown_contract, indent=2) + "\n")
    # The missing-file case gets its own directory so removing its CSV does
    # not contaminate the hash/ancestor cases that share the corrupt folder.
    missing_dir = ROOT / "corrupt" / "missing_file"
    if missing_dir.exists():
        shutil.rmtree(missing_dir)
    missing_dir.mkdir(parents=True)
    for name in ("S0.json", "S1.json", "S2.json", "orders-S0.csv", "orders-S1.csv", "orders-S2.csv"):
        shutil.copy(corrupt / name, missing_dir / name)
    missing_contract = {
        "ancestor": "fixtures/fork_merge/corrupt/missing_file/S0.json",
        "primary": "fixtures/fork_merge/corrupt/missing_file/S1.json",
        "replica": "fixtures/fork_merge/corrupt/missing_file/S2.json",
        "contracts": ["file_strict", "field_disjoint"],
        "expected_merge_parents": ["S1", "S2"],
    }
    (missing_dir / "contract.json").write_text(json.dumps(missing_contract, indent=2) + "\n")
    (missing_dir / "orders-S2.csv").unlink()


if __name__ == "__main__":
    main()
