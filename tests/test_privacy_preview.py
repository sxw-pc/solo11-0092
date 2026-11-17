from __future__ import annotations

from pathlib import Path

from lakemirror.privacy_evidence import inspect_privacy_table
from lakemirror.privacy_preview import evaluate_candidate_mask, preview_primary_suppression

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "privacy_table"


def test_primary_only_exact_margins_recover_both_small_cells() -> None:
    evidence = inspect_privacy_table(FIXTURES / "contract.json")
    preview = preview_primary_suppression(evidence, "exact_margins")
    assert preview["primary_mask"] == ["R1:C1", "R2:C3"]
    assert preview["candidate_values"] == {"R1:C1": [1], "R2:C3": [1]}
    assert preview["uniquely_recovered_by_margins"] == ["R1:C1", "R2:C3"]
    assert preview["would_leak_if_source_sha_published"] is True
    assert preview["publishable"] is False


def test_rounded_primary_preview_changes_after_verified_correction() -> None:
    base = inspect_privacy_table(FIXTURES / "contract.json")
    corrected = inspect_privacy_table(FIXTURES / "corrected-contract.json")
    before = preview_primary_suppression(base, "rounded_margins")
    after = preview_primary_suppression(corrected, "rounded_margins")
    assert before["candidate_values"] == {"R1:C1": [0, 1], "R2:C3": [0, 1, 2]}
    assert before["uniquely_recovered_by_margins"] == []
    assert after["candidate_values"] == {"R1:C1": [1], "R2:C3": [1, 2]}
    assert after["uniquely_recovered_by_margins"] == ["R1:C1"]
    assert before["source_sha_dictionary_matches"] == after["source_sha_dictionary_matches"] == 1
    assert before["publishable"] is after["publishable"] is False


def test_candidate_mask_diagnostics_show_exact_and_rounded_disclosure_difference() -> None:
    evidence = inspect_privacy_table(FIXTURES / "contract.json")
    primary = ["R1:C1", "R2:C3"]
    exact_primary = evaluate_candidate_mask(evidence, "exact_margins", primary)
    rounded_primary = evaluate_candidate_mask(evidence, "rounded_margins", primary)
    assert exact_primary["protects_sensitive_cells"] is False
    assert rounded_primary["protects_sensitive_cells"] is True
    assert exact_primary["sensitive_values"] == {"R1:C1": [1], "R2:C3": [1]}
    exact_four = evaluate_candidate_mask(evidence, "exact_margins", ["R1:C1", "R1:C3", "R2:C1", "R2:C3"])
    assert exact_four["candidate_count"] == 7
    assert exact_four["sensitive_values"] == {"R1:C1": list(range(7)), "R2:C3": list(range(7))}
    assert exact_four["protects_sensitive_cells"] is True
    for extra in ["R1:C2", "R1:C3", "R2:C1", "R2:C2"]:
        assert evaluate_candidate_mask(evidence, "exact_margins", primary + [extra])["protects_sensitive_cells"] is False
    assert exact_four["publishable"] is False


def test_corrected_mask_diagnostic_rejects_reused_primary_pair() -> None:
    corrected = inspect_privacy_table(FIXTURES / "corrected-contract.json")
    result = evaluate_candidate_mask(corrected, "rounded_margins", ["R1:C1", "R2:C3"])
    assert result["sensitive_values"] == {"R1:C1": [1], "R2:C3": [1, 2]}
    assert result["protects_sensitive_cells"] is False
