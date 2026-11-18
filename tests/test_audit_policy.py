from __future__ import annotations

import json
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from lakemirror.audit_game_evidence import inspect_audit_calibration
from lakemirror.audit_policy import (
    AuditPolicyError,
    build_policy,
    execute_draw,
    publish_receipt,
    verify_receipt,
)
from lakemirror.audit_policy_preview import preview_audit_policy

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "audit_game" / "calibration.json"
ROOT = Path(__file__).resolve().parents[1]
HALF = Fraction(1, 2)
GOLDEN_POLICY = {"byte_sha_recheck": "0", "schema_diff": "4/17",
                 "row_provenance": "10/17", "snapshot_chain": "3/17"}


def mutated_fixture(tmp_path: Path, mutate) -> Path:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_worst_case_contract_exact_minimax_with_dual_proof() -> None:
    evidence = inspect_audit_calibration(FIXTURE)
    result = build_policy(evidence, "worst_case_guarantee")
    assert result["policy"] == GOLDEN_POLICY
    assert result["by_attack_mode"] == {mode: "43/85" for mode in evidence["attack_modes"]}
    decision = result["decision"]
    assert decision["worst_case_yield"] == "43/85"
    assert decision["worst_attack_modes"] == evidence["attack_modes"]
    assert decision["exceeds_threshold"] is True
    assert Fraction(decision["worst_case_yield"]) - HALF == Fraction(1, 170)
    dual = decision["dual"]
    assert dual["adversary_distribution"] == {
        "FILE_SHA256_MISMATCH": "9/34", "SCHEMA_TYPE_MISMATCH": "6/17", "SNAPSHOT_FORK": "13/34"}
    assert sum(Fraction(w) for w in dual["adversary_distribution"].values()) == 1
    proof = decision["proof"]
    assert proof["primal_value"] == proof["dual_value"] == "43/85"
    assert proof["values_equal"] is True
    matrix = [[Fraction(rate) for rate in row["rates"]] for row in evidence["calibration"]]
    adversary = [Fraction(w) for w in dual["adversary_distribution"].values()]
    assert max(sum(adversary[j] * row[j] for j in range(3)) for row in matrix) == Fraction(43, 85)


def test_prior_contract_pure_best_is_blind_to_schema_attack() -> None:
    result = build_policy(inspect_audit_calibration(FIXTURE), "prior_weighted_yield")
    assert result["policy"] == {"byte_sha_recheck": "1", "schema_diff": "0",
                                "row_provenance": "0", "snapshot_chain": "0"}
    decision = result["decision"]
    assert decision["prior_weighted_yield"] == "37/50"
    assert decision["worst_case_yield"] == "0"
    assert decision["worst_attack_modes"] == ["SCHEMA_TYPE_MISMATCH"]
    assert decision["exceeds_threshold"] is False


def test_contracts_disagree_on_same_window(tmp_path: Path) -> None:
    prior = publish_receipt(FIXTURE, "prior_weighted_yield", "window-1", "seed-a", tmp_path / "a.json")
    robust = publish_receipt(FIXTURE, "worst_case_guarantee", "window-1", "seed-b", tmp_path / "b.json")
    assert prior["policy"] != robust["policy"]
    assert prior["by_attack_mode"]["SCHEMA_TYPE_MISMATCH"] == "0"
    assert robust["by_attack_mode"]["SCHEMA_TYPE_MISMATCH"] == "43/85"


def test_pure_actions_worst_columns_and_failed_heuristics() -> None:
    evidence = inspect_audit_calibration(FIXTURE)
    pures = [["1", "0", "0", "0"], ["0", "1", "0", "0"], ["0", "0", "1", "0"], ["0", "0", "0", "1"]]
    assert [preview_audit_policy(evidence, w)["worst_case_yield"] for w in pures] == ["0", "0", "2/5", "1/5"]
    uniform = preview_audit_policy(evidence, ["1/4"] * 4)
    assert uniform["worst_case_yield"] == "2/5"
    assert Fraction(uniform["worst_case_yield"]) < HALF
    with pytest.raises(ValueError, match="probability"):
        preview_audit_policy(evidence, ["1", "1", "1", "1"])
    ignoring_low_prior = preview_audit_policy(evidence, ["1/2", "1/2", "0", "0"])
    assert ignoring_low_prior["by_attack_mode"]["FILE_SHA256_MISMATCH"] == "1/2"
    assert ignoring_low_prior["worst_case_yield"] == "2/5"
    assert Fraction(ignoring_low_prior["worst_case_yield"]) < Fraction(43, 85)


def test_exact_half_threshold_is_not_exceeded(tmp_path: Path) -> None:
    hits = [[10, 0, 5], [0, 10, 5], [5, 5, 5], [0, 0, 10]]
    path = mutated_fixture(tmp_path, lambda d: [row.update(hits=h) for row, h in zip(d["calibration_hits"], hits)])
    result = build_policy(inspect_audit_calibration(path), "worst_case_guarantee")
    assert result["decision"]["worst_case_yield"] == "1/2"
    assert Fraction(result["decision"]["worst_case_yield"]) == HALF
    assert result["decision"]["exceeds_threshold"] is False


def test_perturbed_calibration_changes_support_without_hardcoded_scores(tmp_path: Path) -> None:
    path = mutated_fixture(tmp_path, lambda d: d["calibration_hits"][0].update(hits=[10, 5, 4]))
    evidence = inspect_audit_calibration(path)
    result = build_policy(evidence, "worst_case_guarantee")
    assert result["policy"] != GOLDEN_POLICY
    assert result["decision"]["worst_case_yield"] != "43/85"
    support = {check for check, weight in result["policy"].items() if Fraction(weight) > 0}
    assert support != {"schema_diff", "row_provenance", "snapshot_chain"}
    matrix = [[Fraction(rate) for rate in row["rates"]] for row in evidence["calibration"]]
    policy = [Fraction(result["policy"][row["check"]]) for row in evidence["calibration"]]
    yields = [sum(policy[i] * matrix[i][j] for i in range(4)) for j in range(3)]
    assert min(yields) == Fraction(result["decision"]["worst_case_yield"])
    dual = result["decision"]["dual"]
    adversary = [Fraction(w) for w in dual["adversary_distribution"].values()]
    assert max(sum(adversary[j] * matrix[i][j] for j in range(3)) for i in range(4)) == min(yields)
    assert result["decision"]["proof"]["values_equal"] is True


def test_optimal_ties_resolve_deterministically(tmp_path: Path) -> None:
    path = mutated_fixture(tmp_path, lambda d: [row.update(hits=[5, 5, 5]) for row in d["calibration_hits"]])
    first = build_policy(inspect_audit_calibration(path), "worst_case_guarantee")
    second = build_policy(inspect_audit_calibration(path), "worst_case_guarantee")
    assert first == second
    assert first["decision"]["worst_case_yield"] == "1/2"
    assert sum(Fraction(w) for w in first["policy"].values()) == 1


@pytest.mark.parametrize("mutate", [
    lambda d: d["calibration_hits"][0].update(hits=[11, 0, 4]),
    lambda d: d["calibration_hits"][1].update(hits=[0, -1, 4]),
    lambda d: d["attack_modes"].__setitem__(1, "NOT_A_CONFLICT_CODE"),
    lambda d: d["calibration_hits"][1].update(check="byte_sha_recheck"),
    lambda d: d.update(case_prior_counts=[7, 2, 2]),
    lambda d: d.update(one_check_per_window=False),
])
def test_bad_calibration_rejected(tmp_path: Path, mutate) -> None:
    path = mutated_fixture(tmp_path, mutate)
    with pytest.raises(AuditPolicyError, match="calibration"):
        publish_receipt(path, "worst_case_guarantee", "window-bad", "seed", tmp_path / "r.json")


def test_unknown_contract_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuditPolicyError, match="unknown audit contract"):
        build_policy(inspect_audit_calibration(FIXTURE), "execute_everything")
    with pytest.raises(AuditPolicyError, match="unknown audit contract"):
        publish_receipt(FIXTURE, "execute_everything", "window-x", "seed", tmp_path / "r.json")


def test_receipt_records_commitment_snapshot_fixture_and_single_action(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    published = publish_receipt(FIXTURE, "worst_case_guarantee", "window-7", "seed-secret", path)
    assert published["snapshot_id"] == "synthetic-lake-audit-2026-01"
    assert len(published["fixture_sha256"]) == 64
    assert published["contract"] == "worst_case_guarantee"
    assert published["one_check_per_window"] is True
    assert published["seed"] is None and len(published["commitment"]) == 64
    assert sum(Fraction(w) for w in published["policy"].values()) == 1
    executed = execute_draw(path, FIXTURE, "seed-secret")
    assert executed["status"] == "executed"
    assert executed["action"] in {"schema_diff", "row_provenance", "snapshot_chain"}
    assert executed["seed"] == "seed-secret"
    assert 0 <= Fraction(executed["draw_uniform"]) < 1
    result = verify_receipt(path, FIXTURE, require_contract="worst_case_guarantee")
    assert result["valid"] is True and result["action"] == executed["action"]
    other = tmp_path / "other.json"
    publish_receipt(FIXTURE, "worst_case_guarantee", "window-7", "seed-secret", other)
    assert execute_draw(other, FIXTURE, "seed-secret")["action"] == executed["action"]


def test_wrong_seed_double_draw_and_republish_fail(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    publish_receipt(FIXTURE, "worst_case_guarantee", "window-8", "seed-secret", path)
    with pytest.raises(AuditPolicyError, match="commitment"):
        execute_draw(path, FIXTURE, "wrong-seed")
    execute_draw(path, FIXTURE, "seed-secret")
    with pytest.raises(AuditPolicyError, match="already executed"):
        execute_draw(path, FIXTURE, "seed-secret")
    with pytest.raises(AuditPolicyError, match="already exists"):
        publish_receipt(FIXTURE, "worst_case_guarantee", "window-9", "seed", path)


def test_calibration_replay_and_cross_contract_reuse_fail(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    publish_receipt(FIXTURE, "worst_case_guarantee", "window-9", "seed", path)
    stale = mutated_fixture(tmp_path, lambda d: d["calibration_hits"][0].update(hits=[9, 1, 4]))
    with pytest.raises(AuditPolicyError, match="stale"):
        verify_receipt(path, stale)
    with pytest.raises(AuditPolicyError, match="stale"):
        execute_draw(path, stale, "seed")
    with pytest.raises(AuditPolicyError, match="contract"):
        verify_receipt(path, FIXTURE, require_contract="prior_weighted_yield")
    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["contract"] = "prior_weighted_yield"
    path.write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    with pytest.raises(AuditPolicyError, match="fingerprint"):
        verify_receipt(path, FIXTURE)


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "lakemirror", *args],
                          cwd=str(ROOT), text=True, capture_output=True, timeout=120)


def test_cli_publish_draw_verify_roundtrip_and_failures(tmp_path: Path) -> None:
    receipt = tmp_path / "receipt.json"
    published = run_cli("audit-policy", "--calibration", str(FIXTURE), "--contract", "worst_case_guarantee",
                        "--window", "w-cli", "--seed", "s-1", "--out", str(receipt))
    assert published.returncode == 0, published.stderr
    assert json.loads(published.stdout)["status"] == "published"
    drawn = run_cli("audit-draw", "--receipt", str(receipt), "--calibration", str(FIXTURE), "--seed", "s-1")
    assert drawn.returncode == 0, drawn.stderr
    assert json.loads(drawn.stdout)["action"] in {"schema_diff", "row_provenance", "snapshot_chain"}
    verified = run_cli("audit-verify", "--receipt", str(receipt), "--calibration", str(FIXTURE),
                       "--contract", "worst_case_guarantee")
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True
    stale = mutated_fixture(tmp_path, lambda d: d["calibration_hits"][2].update(hits=[8, 4, 3]))
    replayed = run_cli("audit-verify", "--receipt", str(receipt), "--calibration", str(stale))
    assert replayed.returncode == 2
    assert "AUDIT_CALIBRATION_STALE" in replayed.stderr
    bad_seed = run_cli("audit-draw", "--receipt", str(receipt), "--calibration", str(FIXTURE), "--seed", "no")
    assert bad_seed.returncode == 2
    assert "AUDIT_RECEIPT_REUSED" in bad_seed.stderr
