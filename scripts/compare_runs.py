"""Compare exact run metrics and evaluate development-phase gates.

Comparison is intentionally a manifest-level operation only.  It never loads a
case file (and therefore cannot accidentally expose the frozen acceptance
dataset to a development/validation caller).  When manifests are supplied,
their recorded production Schema reference is resolved and each completed run
is scored before case-level regressions are calculated.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
from typing import Literal

try:
    from scripts.run_prompt_eval import DEFAULT_PHASE_REPEATS, RunManifest, load_manifest
    from scripts.score_results import (
        CaseScore,
        RunMetrics,
        ScoreError,
        _schema_from_reference,
        score_run,
    )
except ModuleNotFoundError:  # pragma: no cover - direct-script compatibility
    from run_prompt_eval import DEFAULT_PHASE_REPEATS, RunManifest, load_manifest
    from score_results import (
        CaseScore,
        RunMetrics,
        ScoreError,
        _schema_from_reference,
        score_run,
    )


Phase = Literal["development", "validation", "acceptance"]
_PHASE_DATASET = {
    "development": "dev",
    "validation": "validation",
    "acceptance": "acceptance",
}
_PHASE_REPEATS = {
    "development": DEFAULT_PHASE_REPEATS["dev"],
    "validation": DEFAULT_PHASE_REPEATS["validation"],
    "acceptance": DEFAULT_PHASE_REPEATS["acceptance"],
}


class ComparisonError(ValueError):
    """Raised when two runs cannot be safely compared."""


CompareError = ComparisonError


def _phase(value: object) -> Phase:
    if value == "dev":
        return "development"
    if value in _PHASE_DATASET:
        return value  # type: ignore[return-value]
    raise ValueError(
        "phase must be one of development, validation, acceptance"
    )


def _decimal(value: object, *, label: str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception as error:
        raise ComparisonError(f"{label} is not a decimal metric") from error


def _case_map(metrics: RunMetrics) -> dict[str, CaseScore]:
    result: dict[str, CaseScore] = {}
    for case in metrics.case_scores:
        if case.case_id in result:
            raise ComparisonError(f"duplicate case score {case.case_id!r}")
        result[case.case_id] = case
    return result


def _normalise_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _normalise_json(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalise_json(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            _normalise_json(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception as error:
        raise ComparisonError(f"manifest compatibility value is not serializable: {error}") from None


def _slot_plan(manifest: RunManifest) -> tuple[tuple[str, int], ...]:
    return tuple((slot.case_id, slot.repeat_index) for slot in manifest.slots)


def _manifest_phase_errors(
    manifest: RunManifest, label: str, phase: Phase
) -> tuple[str, ...]:
    """Reject incomplete identity or non-default evaluation intensity."""

    errors: list[str] = []
    expected_dataset = _PHASE_DATASET[phase]
    dataset = "dev" if manifest.dataset == "development" else manifest.dataset
    if not dataset:
        errors.append(f"{label}.dataset is missing")
    elif dataset != expected_dataset:
        errors.append(f"{label}.dataset is incompatible with {phase}")
    expected_repeats = _PHASE_REPEATS[phase]
    if manifest.repeats != expected_repeats:
        errors.append(
            f"{label}.repeats must be exactly {expected_repeats} for {phase}"
        )
    for name, value in (
        ("cycle_id", manifest.cycle_id),
        ("prompt_path", manifest.prompt_path),
        ("schema_import", manifest.schema_import),
        ("prompt_hash", manifest.prompt_hash),
    ):
        if not isinstance(value, str) or not value:
            errors.append(f"{label}.{name} is missing")
    if not manifest.slots:
        errors.append(f"{label}.slots are missing")
    if not manifest.case_data:
        errors.append(f"{label}.case_data is missing")
    if not isinstance(manifest.client_config, Mapping) or not manifest.client_config:
        errors.append(f"{label}.client_config is missing")
    return tuple(errors)


def _canonical_path(value: object) -> str | None:
    if value is None:
        return None
    return str(Path(str(value)).resolve(strict=False))


def _manifest_compatibility(
    baseline: RunManifest, candidate: RunManifest
) -> tuple[str, ...]:
    """Return all incompatible result-affecting manifest fields."""

    errors: list[str] = []
    if _canonical_path(baseline.prompt_path) != _canonical_path(candidate.prompt_path):
        errors.append("prompt_path")
    if baseline.repeats != candidate.repeats:
        errors.append("repeats")
    baseline_dataset = baseline.dataset
    candidate_dataset = candidate.dataset
    if baseline_dataset == "development":
        baseline_dataset = "dev"
    if candidate_dataset == "development":
        candidate_dataset = "dev"
    if baseline_dataset != candidate_dataset:
        errors.append("dataset")
    if baseline.cycle_id != candidate.cycle_id:
        errors.append("cycle_id")
    if baseline.schema_import != candidate.schema_import:
        errors.append("schema_import")
    if _slot_plan(baseline) != _slot_plan(candidate):
        errors.append("slots")
    if _canonical(baseline.case_data) != _canonical(candidate.case_data):
        errors.append("case_data")
    if _canonical(baseline.client_config) != _canonical(candidate.client_config):
        errors.append("client_config")
    # Prompt hashes and all timestamps are deliberately excluded: those are
    # precisely the fields expected to differ between baseline and candidate.
    return tuple(errors)


def _coerce_run(value: object, schema: type | None) -> tuple[RunMetrics, RunManifest | None]:
    if isinstance(value, RunMetrics):
        return value, None
    if isinstance(value, (str, Path)):
        try:
            value = load_manifest(Path(value))
        except Exception as error:
            raise ComparisonError(f"unable to load run manifest: {error}") from None
    if not isinstance(value, RunManifest):
        raise TypeError("compare_runs expects RunManifest or RunMetrics values")
    if schema is None:
        if not value.schema_import:
            raise ComparisonError("manifest schema import reference is missing")
        try:
            schema = _schema_from_reference(
                value.schema_import,
                manifest_path=value.manifest_path,
                prompt_path=value.prompt_path,
            )
        except ScoreError as error:
            raise ComparisonError(str(error)) from None
    try:
        metrics = score_run(value, schema)
    except ScoreError as error:
        raise ComparisonError(str(error)) from None
    return metrics, value


@dataclass(frozen=True, slots=True)
class Comparison:
    """Baseline/candidate metrics plus phase-specific case regressions."""

    baseline: RunMetrics
    candidate: RunMetrics
    phase: Phase = "development"
    regression_count: int | None = None
    stability_regression_count: int | None = None
    critical_failures: int | None = None
    regression_cases: tuple[str, ...] = ()
    stability_regression_cases: tuple[str, ...] = ()
    critical_failure_cases: tuple[str, ...] = ()
    run_accuracy_delta: Decimal | None = None
    stable_case_rate_delta: Decimal | None = None
    schema_valid_rate_delta: Decimal | None = None
    compatibility_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        phase = _phase(self.phase)
        object.__setattr__(self, "phase", phase)
        for name in (
            "run_accuracy_delta",
            "stable_case_rate_delta",
            "schema_valid_rate_delta",
        ):
            value = getattr(self, name)
            if value is None:
                baseline_value = getattr(self.baseline, name.removesuffix("_delta"))
                candidate_value = getattr(self.candidate, name.removesuffix("_delta"))
                value = _decimal(candidate_value, label=name) - _decimal(
                    baseline_value, label=name
                )
            else:
                value = _decimal(value, label=name)
            object.__setattr__(self, name, value)

        if (
            self.regression_count is None
            or self.stability_regression_count is None
            or self.critical_failures is None
        ):
            computed = self._compute_case_regressions(phase)
            if self.regression_count is None:
                object.__setattr__(self, "regression_count", computed[0])
            if self.stability_regression_count is None:
                object.__setattr__(self, "stability_regression_count", computed[1])
            if self.critical_failures is None:
                object.__setattr__(self, "critical_failures", computed[2])
            if not self.regression_cases:
                object.__setattr__(self, "regression_cases", computed[3])
            if not self.stability_regression_cases:
                object.__setattr__(self, "stability_regression_cases", computed[4])
            if not self.critical_failure_cases:
                object.__setattr__(self, "critical_failure_cases", computed[5])

        # Dataclass type annotations cannot enforce this for values assembled
        # by callers, so normalize the public count fields explicitly.
        for name in (
            "regression_count",
            "stability_regression_count",
            "critical_failures",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")

    def _compute_case_regressions(
        self, phase: Phase
    ) -> tuple[int, int, int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        baseline = _case_map(self.baseline)
        candidate = _case_map(self.candidate)
        if set(baseline) != set(candidate):
            # Keep construction useful for hand-authored metrics while making
            # actual manifest comparisons reject this mismatch below.
            return 0, 0, 0, (), (), ()
        required = 9 if phase == "acceptance" else 4
        regressions: list[str] = []
        stability: list[str] = []
        critical: list[str] = []
        for case_id in baseline:
            before = baseline[case_id]
            after = candidate[case_id]
            if before.meets(required) and not after.meets(required):
                regressions.append(case_id)
            if before.stable and not after.stable:
                stability.append(case_id)
            if after.priority == "critical" and not after.meets(required):
                critical.append(case_id)
        return (
            len(regressions),
            len(stability),
            len(critical),
            tuple(regressions),
            tuple(stability),
            tuple(critical),
        )

    def normal_cases_meeting(self, repeats_required: int) -> bool:
        if repeats_required < 1:
            raise ValueError("repeats_required must be positive")
        normal = [case for case in self.candidate.case_scores if case.priority == "normal"]
        return all(case.meets(repeats_required) for case in normal)

    def phase_intensity_matches(self, phase: Phase | str | None = None) -> bool:
        """Require the default number of completed repeats for scored evidence.

        Hand-authored ``RunMetrics`` without case evidence remain useful for
        callers that only need arithmetic deltas.  Manifests are scored into
        case evidence, so every real gate comparison takes this strict branch.
        """

        selected_phase = _phase(phase if phase is not None else self.phase)
        expected = _PHASE_REPEATS[selected_phase]
        if not self.candidate.case_scores:
            return True
        return all(
            case.repeats == expected and case.total_responses == expected
            for case in self.candidate.case_scores
        )

    def has_strict_core_improvement(self) -> bool:
        return bool(
            self.run_accuracy_delta is not None
            and self.stable_case_rate_delta is not None
            and (
                self.run_accuracy_delta > Decimal("0")
                or self.stable_case_rate_delta > Decimal("0")
            )
            and self.run_accuracy_delta >= Decimal("0")
            and self.stable_case_rate_delta >= Decimal("0")
        )

    def failure_reasons(self, phase: Phase | str | None = None) -> tuple[str, ...]:
        selected_phase = _phase(phase if phase is not None else self.phase)
        repeats_required = 9 if selected_phase == "acceptance" else 4
        reasons: list[str] = list(self.compatibility_errors)
        if self.candidate.schema_valid_rate != Decimal("1"):
            reasons.append("candidate schema_valid_rate is below 1")
        if self.critical_failures:
            reasons.append("critical case failure")
        if not self.normal_cases_meeting(repeats_required):
            reasons.append(
                f"normal cases do not meet the {repeats_required}-response threshold"
            )
        if not self.phase_intensity_matches(selected_phase):
            reasons.append(
                f"phase requires exactly {_PHASE_REPEATS[selected_phase]} repeats per case"
            )
        if self.regression_count:
            reasons.append("case regression")
        if self.stability_regression_count:
            reasons.append("stability regression")
        if self.run_accuracy_delta is not None and self.run_accuracy_delta < 0:
            reasons.append("run_accuracy regressed")
        if self.stable_case_rate_delta is not None and self.stable_case_rate_delta < 0:
            reasons.append("stable_case_rate regressed")
        if selected_phase == "validation" and not self.has_strict_core_improvement():
            reasons.append("validation requires strict core improvement")
        return tuple(dict.fromkeys(reasons))

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "run_accuracy_delta": str(self.run_accuracy_delta),
            "stable_case_rate_delta": str(self.stable_case_rate_delta),
            "schema_valid_rate_delta": str(self.schema_valid_rate_delta),
            "regression_count": self.regression_count,
            "regression_cases": list(self.regression_cases),
            "stability_regression_count": self.stability_regression_count,
            "stability_regression_cases": list(self.stability_regression_cases),
            "critical_failures": self.critical_failures,
            "critical_failure_cases": list(self.critical_failure_cases),
            "compatibility_errors": list(self.compatibility_errors),
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """A phase-gate decision with actionable, non-secret failure reasons."""

    passed: bool
    reasons: tuple[str, ...] = ()

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        return self.reasons


def compare_runs(
    baseline: RunManifest | RunMetrics | Path | str,
    candidate: RunManifest | RunMetrics | Path | str,
    phase: Phase | str,
    schema: type | None = None,
) -> Comparison:
    """Score and compare compatible completed runs for one explicit phase."""

    selected_phase = _phase(phase)
    baseline_metrics, baseline_manifest = _coerce_run(baseline, schema)
    candidate_metrics, candidate_manifest = _coerce_run(candidate, schema)

    errors: tuple[str, ...] = ()
    if baseline_manifest is not None and candidate_manifest is not None:
        errors = _manifest_compatibility(baseline_manifest, candidate_manifest)
        for name, manifest in (("baseline", baseline_manifest), ("candidate", candidate_manifest)):
            errors += _manifest_phase_errors(manifest, name, selected_phase)
        if baseline_manifest.schema_import != candidate_manifest.schema_import:
            errors += ("schema_import",)
        if errors:
            raise ComparisonError(
                "incompatible manifests: " + ", ".join(dict.fromkeys(errors))
            )

    baseline_cases = _case_map(baseline_metrics)
    candidate_cases = _case_map(candidate_metrics)
    if set(baseline_cases) != set(candidate_cases):
        raise ComparisonError("baseline and candidate case sets are incompatible")

    comparison = Comparison(
        baseline=baseline_metrics,
        candidate=candidate_metrics,
        phase=selected_phase,
    )
    return comparison


def evaluate_gate(
    comparison: Comparison,
    phase: Phase | str,
) -> GateResult:
    """Evaluate development, validation, or acceptance requirements exactly."""

    selected_phase = _phase(phase)
    if not isinstance(comparison, Comparison):
        raise TypeError("evaluate_gate expects a Comparison")
    repeats_required = 9 if selected_phase == "acceptance" else 4
    common = (
        not comparison.compatibility_errors
        and comparison.candidate.schema_valid_rate == Decimal("1")
        and comparison.critical_failures == 0
        and comparison.normal_cases_meeting(repeats_required)
        and comparison.phase_intensity_matches(selected_phase)
        and comparison.regression_count == 0
        and comparison.stability_regression_count == 0
    )
    if not common:
        return GateResult(False, comparison.failure_reasons(selected_phase))
    nondecreasing = (
        comparison.run_accuracy_delta is not None
        and comparison.stable_case_rate_delta is not None
        and comparison.run_accuracy_delta >= Decimal("0")
        and comparison.stable_case_rate_delta >= Decimal("0")
    )
    if selected_phase == "validation":
        passed = nondecreasing and comparison.has_strict_core_improvement()
    elif selected_phase == "acceptance":
        passed = nondecreasing
    else:
        passed = True
    return GateResult(passed, () if passed else comparison.failure_reasons(selected_phase))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=("development", "validation", "acceptance"), required=True
    )
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        comparison = compare_runs(
            load_manifest(args.baseline), load_manifest(args.candidate), args.phase
        )
        gate = evaluate_gate(comparison, args.phase)
        payload = {
            "status": "passed" if gate.passed else "failed",
            "gate": {"passed": gate.passed, "reasons": list(gate.reasons)},
            "comparison": comparison.to_dict(),
        }
        exit_code = 0 if gate.passed else 1
    except Exception as error:
        payload = {"status": "error", "error": str(error)}
        exit_code = 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return exit_code


__all__ = [
    "Comparison",
    "ComparisonError",
    "CompareError",
    "GateResult",
    "compare_runs",
    "evaluate_gate",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
