from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import pytest
from pydantic import BaseModel

from scripts.compare_runs import Comparison, compare_runs, evaluate_gate
from scripts.run_prompt_eval import (
    SlotResult,
    load_adapter,
    new_manifest,
    persist_manifest,
    record_slot_result,
)
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


def test_development_gate_rejects_four_of_four_instead_of_required_five_repeats():
    case = CaseScore(
        case_id="normal-1",
        priority="normal",
        repeats=4,
        pass_count=4,
        classifications=("pass",) * 4,
    )
    comparison = Comparison(
        baseline=_metrics(case_scores=(case,)),
        candidate=_metrics(case_scores=(case,)),
    )

    gate = evaluate_gate(comparison, "development")

    assert gate.passed is False
    assert any("5" in reason for reason in gate.reasons)


def test_acceptance_gate_rejects_nine_of_nine_instead_of_required_ten_repeats():
    case = CaseScore(
        case_id="normal-1",
        priority="normal",
        repeats=9,
        pass_count=9,
        classifications=("pass",) * 9,
    )
    comparison = Comparison(
        baseline=_metrics(case_scores=(case,)),
        candidate=_metrics(case_scores=(case,)),
    )

    gate = evaluate_gate(comparison, "acceptance")

    assert gate.passed is False
    assert any("10" in reason for reason in gate.reasons)


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
    if dataset == "acceptance" and len(kinds) == 5:
        kinds = ("pass",) * 10
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
        prompt_path="prompt.md",
        schema=Decision,
        dataset=dataset,
        cycle_id="cycle-1",
        client_config=client_config or {"model": "fixed"},
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
    candidate = _manifest("candidate-hash", client_config={"model": "changed"})

    with pytest.raises(ValueError, match="client_config"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_rejects_acceptance_manifest_in_validation_phase():
    baseline = _manifest("baseline-hash", dataset="acceptance")
    candidate = _manifest("candidate-hash", dataset="acceptance")

    with pytest.raises(ValueError, match="dataset"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_rejects_manifests_missing_phase_dataset_identity():
    baseline = _manifest("baseline-hash")
    candidate = _manifest("candidate-hash")
    baseline = replace(baseline, dataset=None)
    candidate = replace(candidate, dataset=None)

    with pytest.raises(ValueError, match="dataset"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_rejects_manifests_missing_cycle_identity():
    baseline = replace(_manifest("baseline-hash"), cycle_id=None)
    candidate = replace(_manifest("candidate-hash"), cycle_id=None)

    with pytest.raises(ValueError, match="cycle_id"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_rejects_manifest_repeats_below_phase_default():
    baseline = _manifest("baseline-hash", kinds=("pass",) * 4)
    candidate = _manifest("candidate-hash", kinds=("pass",) * 4)

    with pytest.raises(ValueError, match="repeats"):
        compare_runs(baseline, candidate, "validation", schema=Decision)


def test_compare_cli_reconstructs_adapter_schema_in_a_fresh_process(tmp_path: Path):
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    adapter = eval_root / "adapter.py"
    adapter.write_text(
        "from pydantic import BaseModel\n"
        "class ProductionDecision(BaseModel):\n"
        "    action: str\n"
        "    reason: str\n"
        "def prepare_call(prompt_path, case):\n"
        "    return {'messages': [], 'schema': ProductionDecision}\n",
        encoding="utf-8",
    )
    prompt = eval_root / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    prepare_call = load_adapter(eval_root)
    schema = prepare_call(prompt, {"id": "case-1"})["schema"]
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
    prompt_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifests: list[Path] = []
    for name, kinds in (
        ("baseline", ("business_error",) + ("pass",) * 4),
        ("candidate", ("pass",) * 5),
    ):
        manifest_path = eval_root / f"{name}.json"
        manifest = new_manifest(
            [case],
            repeats=5,
            prompt_hash=prompt_hash if name == "baseline" else "candidate-hash",
            prompt_path=prompt,
            schema=schema,
            manifest_path=manifest_path,
            dataset="validation",
            cycle_id="cycle-1",
            client_config={"model": "fixed"},
        )
        for slot, kind in zip(manifest.slots, kinds, strict=True):
            parsed = schema(action="accept", reason="matched")
            if kind == "business_error":
                parsed = schema(action="reject", reason="matched")
            manifest = record_slot_result(
                manifest,
                slot.key,
                SlotResult(kind=kind, parsed=parsed),
            )
        persist_manifest(manifest, manifest_path)
        manifests.append(manifest_path)

    report_path = eval_root / "compare.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.compare_runs",
            "--baseline",
            str(manifests[0]),
            "--candidate",
            str(manifests[1]),
            "--phase",
            "validation",
            "--report",
            str(report_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "passed"
