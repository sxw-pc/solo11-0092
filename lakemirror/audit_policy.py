"""Ex-ante audit check policy contracts with exact rational minimax receipts.

Each audit window allows exactly one of four synthetic checks. A policy is a
probability distribution published before the window; exactly one check is
then drawn and executed. Two contracts are available:

- ``worst_case_guarantee``: maximize the minimum detection yield across the
  three synthetic attack columns, certified by an exact rational dual bound.
- ``prior_weighted_yield``: maximize the prior-weighted average yield, which
  permits a pure choice and may leave a rare attack column at zero.

Calibration hit rates are a synthetic fixture payoff table, not a statistical
guarantee about real attack detection probabilities.
"""
from __future__ import annotations

import json
from fractions import Fraction
from itertools import combinations
from pathlib import Path
from typing import Any, Iterator

from .audit_game_evidence import AuditGameEvidenceError, inspect_audit_calibration
from .util import LakeMirrorError, fingerprint, sha256_text, write_json

CONTRACT_WORST_CASE = "worst_case_guarantee"
CONTRACT_PRIOR_YIELD = "prior_weighted_yield"
CONTRACTS = (CONTRACT_WORST_CASE, CONTRACT_PRIOR_YIELD)
THRESHOLD = Fraction(1, 2)
RECEIPT_KIND = "audit_policy_receipt"


class AuditPolicyError(LakeMirrorError):
    """Raised when an audit policy, receipt, or calibration check fails."""


def _solve_square(equations: list[list[Fraction]], rhs: list[Fraction]) -> list[Fraction] | None:
    size = len(equations)
    aug = [list(row) + [rhs[index]] for index, row in enumerate(equations)]
    for col in range(size):
        pivot = next((row for row in range(col, size) if aug[row][col] != 0), None)
        if pivot is None:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        aug[col] = [value / aug[col][col] for value in aug[col]]
        for row in range(size):
            if row != col and aug[row][col] != 0:
                factor = aug[row][col]
                aug[row] = [a - factor * b for a, b in zip(aug[row], aug[col])]
    return [aug[row][size] for row in range(size)]


def _vertex_solutions(equality: tuple, options: list[tuple], pick: int) -> Iterator[list[Fraction]]:
    for active in combinations(range(len(options)), pick):
        equations = [equality] + [options[index] for index in active]
        solution = _solve_square([lhs for lhs, _ in equations], [rhs for _, rhs in equations])
        if solution is not None:
            yield solution


def _primal_vertices(matrix: list[list[Fraction]]) -> Iterator[tuple]:
    rows, cols = len(matrix), len(matrix[0])
    one, zero = Fraction(1), Fraction(0)
    equality = ([one] * rows + [zero], one)
    options = [([matrix[i][j] for i in range(rows)] + [-one], zero) for j in range(cols)]
    options += [([one if i == k else zero for i in range(rows)] + [zero], zero) for k in range(rows)]
    for solution in _vertex_solutions(equality, options, rows):
        policy, value = solution[:rows], solution[rows]
        yields = [sum(policy[i] * matrix[i][j] for i in range(rows)) for j in range(cols)]
        if all(weight >= 0 for weight in policy) and all(gain >= value for gain in yields):
            yield value, policy, yields


def _dual_vertices(matrix: list[list[Fraction]]) -> Iterator[tuple]:
    rows, cols = len(matrix), len(matrix[0])
    one, zero = Fraction(1), Fraction(0)
    equality = ([one] * cols + [zero], one)
    options = [([matrix[i][j] for j in range(cols)] + [-one], zero) for i in range(rows)]
    options += [([one if j == k else zero for j in range(cols)] + [zero], zero) for k in range(cols)]
    for solution in _vertex_solutions(equality, options, cols):
        adversary, bound = solution[:cols], solution[cols]
        gains = [sum(adversary[j] * matrix[i][j] for j in range(cols)) for i in range(rows)]
        if all(weight >= 0 for weight in adversary) and all(gain <= bound for gain in gains):
            yield bound, adversary, gains


def solve_audit_game(matrix: list[list[Fraction]]) -> dict[str, Any]:
    """Solve the zero-sum audit game exactly with a verifiable dual certificate."""
    primal, dual = list(_primal_vertices(matrix)), list(_dual_vertices(matrix))
    if not primal or not dual:
        raise AuditPolicyError("audit game has no feasible vertex", "AUDIT_GAME_INFEASIBLE")
    value = max(item[0] for item in primal)
    _, policy, yields = min((item for item in primal if item[0] == value),
                            key=lambda item: (item[1], item[2]))
    bound = min(item[0] for item in dual)
    _, adversary, row_yields = min((item for item in dual if item[0] == bound),
                                   key=lambda item: (item[1], item[2]))
    if value != bound:
        raise AuditPolicyError("primal and dual audit values differ", "AUDIT_GAME_DUALITY_GAP")
    return {"value": value, "policy": policy, "column_yields": yields,
            "adversary": adversary, "row_yields": row_yields}


def build_policy(evidence: dict[str, Any], contract: str) -> dict[str, Any]:
    """Compute the exact ex-ante policy and decision for one contract."""
    rows = evidence["calibration"]
    checks = [row["check"] for row in rows]
    modes = evidence["attack_modes"]
    matrix = [[Fraction(rate) for rate in row["rates"]] for row in rows]
    if contract == CONTRACT_WORST_CASE:
        game = solve_audit_game(matrix)
        policy = dict(zip(checks, (str(weight) for weight in game["policy"])))
        gains = game["column_yields"]
        value = game["value"]
        decision: dict[str, Any] = {
            "worst_case_yield": str(value),
            "worst_attack_modes": [mode for mode, gain in zip(modes, gains) if gain == value],
            "threshold": str(THRESHOLD),
            "exceeds_threshold": value > THRESHOLD,
            "dual": {
                "adversary_distribution": dict(zip(modes, (str(w) for w in game["adversary"]))),
                "dual_value": str(value),
                "row_yields": dict(zip(checks, (str(g) for g in game["row_yields"]))),
            },
            "proof": {
                "method": "exact_rational_zero_sum_minimax",
                "primal_value": str(value),
                "dual_value": str(value),
                "values_equal": True,
                "primal_min_column_yield": str(min(gains)),
                "dual_max_row_yield": str(max(game["row_yields"])),
            },
        }
    elif contract == CONTRACT_PRIOR_YIELD:
        best = evidence["prior_best_check"]
        gains = matrix[checks.index(best)]
        policy = {check: "1" if check == best else "0" for check in checks}
        decision = {
            "prior_weighted_yield": evidence["prior_scores"][best],
            "worst_case_yield": str(min(gains)),
            "worst_attack_modes": [mode for mode, gain in zip(modes, gains) if gain == min(gains)],
            "threshold": str(THRESHOLD),
            "exceeds_threshold": min(gains) > THRESHOLD,
        }
    else:
        raise AuditPolicyError(f"unknown audit contract: {contract}", "AUDIT_CONTRACT_UNKNOWN")
    return {"contract": contract, "policy": policy,
            "by_attack_mode": dict(zip(modes, (str(gain) for gain in gains))),
            "decision": decision}


def _load_evidence(calibration_path: str | Path) -> dict[str, Any]:
    try:
        return inspect_audit_calibration(calibration_path)
    except AuditGameEvidenceError as exc:
        raise AuditPolicyError(f"invalid audit calibration: {exc}", "AUDIT_CALIBRATION_INVALID") from exc


def _commitment(seed: str) -> str:
    return sha256_text(f"audit-policy-commit:v1:{seed}")


def _draw_uniform(seed: str, window_id: str, fixture_sha: str, contract: str) -> Fraction:
    digest = sha256_text(f"audit-policy-draw:v1:{seed}:{window_id}:{fixture_sha}:{contract}")
    return Fraction(int(digest, 16), 1 << 256)


def _receipt_fingerprint(receipt: dict[str, Any]) -> str:
    return fingerprint({key: value for key, value in receipt.items() if key != "receipt_fingerprint"})


def publish_receipt(calibration_path: str | Path, contract: str, window_id: str,
                    seed: str, out_path: str | Path) -> dict[str, Any]:
    """Publish the ex-ante policy for one audit window with a seed commitment."""
    if not isinstance(window_id, str) or not window_id:
        raise AuditPolicyError("window id must be a non-empty string", "AUDIT_WINDOW_INVALID")
    if not isinstance(seed, str) or not seed:
        raise AuditPolicyError("seed must be a non-empty string", "AUDIT_SEED_INVALID")
    out = Path(out_path)
    if out.exists():
        raise AuditPolicyError(f"receipt already exists for this window: {out}", "AUDIT_RECEIPT_EXISTS")
    evidence = _load_evidence(calibration_path)
    built = build_policy(evidence, contract)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "kind": RECEIPT_KIND,
        "window_id": window_id,
        "contract": built["contract"],
        "snapshot_id": evidence["snapshot_id"],
        "fixture_sha256": evidence["fixture_sha256"],
        "attack_modes": evidence["attack_modes"],
        "one_check_per_window": True,
        "policy": built["policy"],
        "by_attack_mode": built["by_attack_mode"],
        "decision": built["decision"],
        "commitment": _commitment(seed),
        "seed": None,
        "action": None,
        "draw_uniform": None,
        "status": "published",
    }
    receipt["receipt_fingerprint"] = _receipt_fingerprint(receipt)
    write_json(out, receipt)
    return receipt


def _load_receipt(receipt_path: str | Path) -> dict[str, Any]:
    try:
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AuditPolicyError(f"cannot read audit receipt: {exc}", "AUDIT_RECEIPT_INVALID") from exc
    if (not isinstance(receipt, dict) or receipt.get("kind") != RECEIPT_KIND
            or receipt.get("schema_version") != 1 or receipt.get("status") not in {"published", "executed"}):
        raise AuditPolicyError("not an audit policy receipt", "AUDIT_RECEIPT_INVALID")
    if receipt.get("receipt_fingerprint") != _receipt_fingerprint(receipt):
        raise AuditPolicyError("audit receipt fingerprint mismatch", "AUDIT_RECEIPT_TAMPERED")
    return receipt


def _current_evidence(receipt: dict[str, Any], calibration_path: str | Path) -> dict[str, Any]:
    evidence = _load_evidence(calibration_path)
    if evidence["fixture_sha256"] != receipt["fixture_sha256"]:
        raise AuditPolicyError("calibration changed since publish; stale receipt must not be reused",
                               "AUDIT_CALIBRATION_STALE")
    return evidence


def _derive_action(policy: dict[str, str], uniform: Fraction) -> str:
    cumulative = Fraction(0)
    for check, weight in sorted(policy.items()):
        cumulative += Fraction(weight)
        if uniform < cumulative:
            return check
    raise AuditPolicyError("audit policy weights do not cover the draw", "AUDIT_POLICY_INVALID")


def execute_draw(receipt_path: str | Path, calibration_path: str | Path, seed: str) -> dict[str, Any]:
    """Reveal the seed and draw exactly one check for a published receipt."""
    path = Path(receipt_path)
    receipt = _load_receipt(path)
    if receipt["status"] != "published":
        raise AuditPolicyError("audit receipt already executed", "AUDIT_RECEIPT_REUSED")
    _current_evidence(receipt, calibration_path)
    if not isinstance(seed, str) or not seed or _commitment(seed) != receipt["commitment"]:
        raise AuditPolicyError("seed does not match receipt commitment", "AUDIT_SEED_MISMATCH")
    uniform = _draw_uniform(seed, receipt["window_id"], receipt["fixture_sha256"], receipt["contract"])
    receipt.update({"seed": seed, "action": _derive_action(receipt["policy"], uniform),
                    "draw_uniform": str(uniform), "status": "executed"})
    receipt["receipt_fingerprint"] = _receipt_fingerprint(receipt)
    write_json(path, receipt)
    return receipt


def verify_receipt(receipt_path: str | Path, calibration_path: str | Path,
                   require_contract: str | None = None) -> dict[str, Any]:
    """Recompute the contract from current calibration and check the receipt."""
    receipt = _load_receipt(receipt_path)
    if require_contract is not None and receipt["contract"] != require_contract:
        raise AuditPolicyError(
            f"receipt contract {receipt['contract']} does not match required {require_contract}",
            "AUDIT_CONTRACT_MISMATCH")
    evidence = _current_evidence(receipt, calibration_path)
    rebuilt = build_policy(evidence, receipt["contract"])
    if (receipt["policy"] != rebuilt["policy"] or receipt["by_attack_mode"] != rebuilt["by_attack_mode"]
            or receipt["decision"] != rebuilt["decision"]):
        raise AuditPolicyError("receipt policy does not match recomputed contract", "AUDIT_POLICY_MISMATCH")
    checks = {"fingerprint": True, "calibration_current": True, "policy_recomputed": True,
              "snapshot_matches": receipt["snapshot_id"] == evidence["snapshot_id"]}
    if receipt["status"] == "executed":
        seed = receipt["seed"]
        if not seed or _commitment(seed) != receipt["commitment"]:
            raise AuditPolicyError("revealed seed does not match commitment", "AUDIT_SEED_MISMATCH")
        uniform = _draw_uniform(seed, receipt["window_id"], receipt["fixture_sha256"], receipt["contract"])
        if (receipt["draw_uniform"] != str(uniform)
                or receipt["action"] != _derive_action(receipt["policy"], uniform)):
            raise AuditPolicyError("executed action does not replay from seed", "AUDIT_ACTION_MISMATCH")
        checks["action_replayed"] = True
    return {"valid": all(checks.values()), "checks": checks, "window_id": receipt["window_id"],
            "contract": receipt["contract"], "status": receipt["status"], "action": receipt["action"]}
