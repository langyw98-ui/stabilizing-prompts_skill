from __future__ import annotations

from decimal import Decimal
import pytest
from pydantic import BaseModel

from scripts.compare_runs import Comparison, compare_runs, evaluate_gate
from scripts.run_prompt_eval import SlotResult, new_manifest, record_slot_result
from scripts.score_results import CaseScore, RunMetrics


def _metrics(
    *,
    accuracy: str = "1",
    stable: str = "1",
    schema_valid: str = "1",
    case_scores: tuple[CaseScore, ...] = (),
) -> RunMetrics:
    return RunMetrics(
        scored_responses=10,
        pass_count=10,
        parse_error_count=0,
        schema_error_count=0,
        business_error_count=0,
        schema_valid_rate=Decimal(schema_valid),
        run_accuracy=Decimal(accuracy),
        stable_case_rate=Decimal(stable),
        case_scores=case_scores,
    )


def _comparison(**kwargs: object) -> Comparison:
    values = {
        "baseline": _metrics(),
        "candidate": _metrics(),
        "regression_count": 0,
        "stability_regression_count": 0,
        "critical_failures": 0,
    }
    values.update(kwargs)
    return Comparison(**values)


def test_validation_requires_strict_improvement():
    comparison = _comparison()

    gate = evaluate_gate(comparison, "validation")

    assert gate.passed is False
    assert gate.reasons


def test_acceptance_allows_equal_perfect_metrics():
    comparison = _comparison()

    assert evaluate_gate(comparison, "acceptance").passed is True


def test_any_case_or_stability_regression_fails_every_candidate_gate():
    comparison = _comparison(regression_count=1, stability_regression_count=1)

    assert evaluate_gate(comparison, "development").passed is False
    assert evaluate_gate(comparison, "validation").passed is False
    assert evaluate_gate(comparison, "acceptance").passed is False


def test_manifest_compatibility_failure_cannot_pass_a_gate():
    comparison = _comparison(compatibility_errors=("client_config",))

    assert evaluate_gate(comparison, "development").passed is False
    assert evaluate_gate(comparison, "validation").passed is False
    assert evaluate_gate(comparison, "acceptance").passed is False


def test_candidate_metric_deltas_are_decimal_and_exposed():
    comparison = Comparison(
        baseline=_metrics(accuracy="0.5", stable="0.25"),
        candidate=_metrics(accuracy="0.75", stable="0.5"),
    )

    assert comparison.run_accuracy_delta == Decimal("0.25")
    assert comparison.stable_case_rate_delta == Decimal("0.25")
    assert comparison.has_strict_core_improvement()


def test_run_metrics_normalizes_serialized_decimal_rates():
    metrics = RunMetrics(
        scored_responses=1,
        pass_count=1,
        parse_error_count=0,
        schema_error_count=0,
        business_error_count=0,
        schema_valid_rate="1",  # type: ignore[arg-type]
        run_accuracy="1",  # type: ignore[arg-type]
        stable_case_rate="1",  # type: ignore[arg-type]
    )

    assert evaluate_gate(Comparison(metrics, metrics), "acceptance").passed is True


def test_gate_explains_normal_case_threshold_failure():
    candidate_case = CaseScore(
        case_id="normal-1",
        priority="normal",
        repeats=5,
        pass_count=3,
        classifications=("pass", "pass", "pass", "business_error", "business_error"),
    )
    comparison = Comparison(
        baseline=_metrics(case_scores=(candidate_case,)),
        candidate=_metrics(case_scores=(candidate_case,)),
    )

    gate = evaluate_gate(comparison, "development")

    assert gate.passed is False
    assert any("normal" in reason for reason in gate.reasons)


def test_compare_rejects_mismatched_case_evidence():
    candidate_case = CaseScore(
        case_id="candidate-only",
        priority="normal",
        repeats=5,
        pass_count=5,
        classifications=("pass",) * 5,
    )

    with pytest.raises(ValueError, match="case sets"):
        compare_runs(
            _metrics(),
            _metrics(case_scores=(candidate_case,)),
            "development",
        )


class Decision(BaseModel):
    action: str
    reason: str


def _manifest(
    prompt_hash: str,
    *,
    dataset: str = "validation",
    client_config: dict[str, object] | None = None,
    kinds: tuple[str, ...] = ("pass",) * 5,
):
    case = {
        "id": "case-1",
        "semantic_family": "family-case-1",
        "source": ["production.py"],
        "input": {"variables": {"case": "1"}, "context": {}},
        "expect": {"output": {"action": "accept", "reason": "matched"}},
        "priority": "normal",
        "dimensions": ["routing"],
        "rationale": "production evidence determines the expected decision",
    }
    manifest = new_manifest(
        [case],
        repeats=len(kinds),
        prompt_hash=prompt_hash,
        schema=Decision,
        dataset=dataset,
        cycle_id="cycle-1",
        client_config=client_config,
    )
    for slot, kind in zip(manifest.slots, kinds, strict=True):
        manifest = record_slot_result(
            manifest,
            slot.key,
            SlotResult(
                kind=kind,
                parsed=Decision(action="accept", reason="matched")
                if kind == "pass"
                else Decision(action="reject", reason="matched")
                if kind == "business_error"
                else None,
            ),
        )
    return manifest


def test_compare_completed_manifests_allows_prompt_hash_change():
    baseline = _manifest("baseline-hash")
    candidate = _manifest("candidate-hash")

    comparison = compare_runs(baseline, candidate, "validation")

    assert comparison.candidate.run_accuracy == Decimal("1")
    assert comparison.regression_count == 0


def test_compare_rejects_changed_result_affecting_manifest_field():
    baseline = _manifest("baseline-hash")
    candidate = _manifest("candidate-hash", client_config={"model": "fixed"})

    with pytest.raises(ValueError, match="client_config"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_rejects_acceptance_manifest_in_validation_phase():
    baseline = _manifest("baseline-hash", dataset="acceptance")
    candidate = _manifest("candidate-hash", dataset="acceptance")

    with pytest.raises(ValueError, match="dataset"):
        compare_runs(baseline, candidate, "validation", schema=Decision)
