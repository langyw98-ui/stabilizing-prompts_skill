from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from scripts.evaluation_summary import (
    FORMAL_KINDS,
    FormalResult,
    SummaryEvidence,
    normalize_formal_result,
    render_summary,
    write_summary,
)


@pytest.fixture
def sample_result() -> FormalResult:
    return normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )


@pytest.fixture
def sample_evidence() -> SummaryEvidence:
    return SummaryEvidence(
        cycle_id="cycle-1",
        evidence_identity={"cycle_id": "cycle-1", "prompt_id": "classify--abc123"},
        prompt_name="classify.md",
        prompt_path="prompts/classify.md",
        model_name="fixed-model",
        case_counts={"acceptance": 2, "dev": 4, "validation": 3},
        repeats={"acceptance": 10, "dev": 5, "validation": 5},
        planned_calls={"acceptance": 20, "dev": 20, "validation": 15},
        completed_calls={"acceptance": 20, "dev": 20, "validation": 15},
        metrics={
            "acceptance": {
                "pass": 20,
                "parse_error": 0,
                "schema_error": 0,
                "business_error": 0,
                "schema_valid_rate": "1",
                "run_accuracy": "1",
                "stable_case_rate": "1",
            },
            "dev": {
                "pass": 20,
                "parse_error": 0,
                "schema_error": 0,
                "business_error": 0,
                "schema_valid_rate": "1",
                "run_accuracy": "1",
                "stable_case_rate": "1",
            },
            "validation": {
                "pass": 15,
                "parse_error": 0,
                "schema_error": 0,
                "business_error": 0,
                "schema_valid_rate": "1",
                "run_accuracy": "1",
                "stable_case_rate": "1",
            },
        },
        comparisons={
            "acceptance": {
                "status": "passed",
                "reason": "all gates passed",
                "fixes": 2,
                "regressions": 0,
                "stability_regressions": 0,
                "unchanged": 1,
            },
            "dev": {
                "status": "passed",
                "fixes": 2,
                "regressions": 0,
                "stability_regressions": 0,
                "unchanged": 2,
            },
            "validation": {
                "status": "passed",
                "fixes": 2,
                "regressions": 0,
                "stability_regressions": 0,
                "unchanged": 1,
            },
        },
        coverage={
            "categories": ["routing", "safety"],
            "boundaries": ["missing input", "ambiguous input"],
            "matrix_complete": True,
            "near_duplicate_review": "none found",
            "exclusions": {"none": "all obligations covered"},
            "saturation": "coverage is sufficient",
        },
        smoke_passed=True,
        failure_summaries=(
            {
                "failure_id": "case-2",
                "category": "business_error",
                "case_id": "case-2",
                "difference": "expected action differs",
            },
            {
                "failure_id": "case-1",
                "category": "schema_error",
                "case_id": "case-1",
                "difference": "missing field",
            },
        ),
    )


@pytest.mark.parametrize(
    ("kind", "profile", "prompt_changes", "acceptance_status"),
    [
        ("no_change_needed", "assets", False, "not_run"),
        ("no_strict_improvement", "assets", False, "not_run"),
        ("validation_failed", "assets", False, "not_run"),
        ("no_improvement_limit", "assets", False, "not_run"),
        ("round_limit", "assets", False, "not_run"),
        ("acceptance_failed", "assets", False, "failed"),
        ("acceptance_passed", "success", True, "passed"),
    ],
)
def test_formal_result_matrix(kind, profile, prompt_changes, acceptance_status):
    result = normalize_formal_result(kind, finished_at_utc="2026-09-16T08:09:10Z")
    assert result.delivery_profile == profile
    assert result.prompt_should_change is prompt_changes
    assert result.acceptance_status == acceptance_status


def test_formal_kinds_are_exact():
    assert FORMAL_KINDS == frozenset(
        {
            "no_change_needed",
            "no_strict_improvement",
            "validation_failed",
            "no_improvement_limit",
            "round_limit",
            "acceptance_failed",
            "acceptance_passed",
        }
    )


def test_result_is_immutable():
    result = normalize_formal_result(
        "no_change_needed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    with pytest.raises(FrozenInstanceError):
        result.kind = "acceptance_passed"  # type: ignore[misc]


def test_summary_is_deterministic_and_redacted(sample_result, sample_evidence):
    first = render_summary(sample_result, sample_evidence)
    second = render_summary(sample_result, sample_evidence)
    assert first == second
    assert "Authorization" not in first
    assert "Bearer secret" not in first
    assert "raw response" not in first.casefold()
    assert "C:\\private\\worktree" not in first
    assert "prepared_commit" not in first
    assert "delivered" not in first.casefold()


@pytest.mark.parametrize(
    ("location", "prose", "secret"),
    [
        ("stop_reason", "stopped after ToKeN: stopsecret1", "stopsecret1"),
        ("failure_reason", "access ToKeN failuresecret2", "failuresecret2"),
        ("coverage", "aUtHoRiZaTiOn coveragesecret3", "coveragesecret3"),
        ("model_name", "fixed-model; bEaReR: modelsecret4", "modelsecret4"),
        ("evidence_identity", "AUTHORIZATION identitysecret5", "identitysecret5"),
        ("failure_bearer", "BEARER=failsecret6", "failsecret6"),
    ],
)
def test_summary_redacts_credentials_in_prose_values(
    sample_result, sample_evidence, location, prose, secret
):
    values = {
        field: getattr(sample_evidence, field)
        for field in sample_evidence.__dataclass_fields__
    }
    result = sample_result
    if location == "stop_reason":
        result = normalize_formal_result(
            "acceptance_passed",
            finished_at_utc="2026-09-16T08:09:10Z",
            stop_reason=prose,
        )
    elif location == "failure_reason":
        failures = [dict(failure) for failure in sample_evidence.failure_summaries]
        failures[0]["reason"] = prose
        values["failure_summaries"] = failures
    elif location == "failure_bearer":
        failures = [dict(failure) for failure in sample_evidence.failure_summaries]
        failures[0]["reason"] = prose
        values["failure_summaries"] = failures
    elif location == "coverage":
        values["coverage"] = {**sample_evidence.coverage, "saturation": prose}
    elif location == "model_name":
        values["model_name"] = prose
    elif location == "evidence_identity":
        values["evidence_identity"] = {
            **sample_evidence.evidence_identity,
            "mode": prose,
        }
    else:  # pragma: no cover - protects the parameterized test setup
        raise AssertionError(f"unknown test location: {location}")

    summary = render_summary(result, SummaryEvidence(**values))
    assert secret not in summary
    assert summary == render_summary(result, SummaryEvidence(**values))


def test_summary_preserves_benign_token_and_authorization_prose(
    sample_result, sample_evidence
):
    values = {
        field: getattr(sample_evidence, field)
        for field in sample_evidence.__dataclass_fields__
    }
    values["coverage"] = {
        **sample_evidence.coverage,
        "saturation": "token count and authorization flow remain stable",
    }

    summary = render_summary(sample_result, SummaryEvidence(**values))

    assert "token count" in summary
    assert "authorization flow" in summary


def test_summary_has_fixed_sections_and_stable_failure_order(sample_result, sample_evidence):
    summary = render_summary(sample_result, sample_evidence)
    headings = [line for line in summary.splitlines() if line.startswith("## ")]
    assert headings == [
        "## 评测对象",
        "## 结论",
        "## 测试力度",
        "## 覆盖面",
        "## 结果指标",
        "## 失败摘要",
        "## Prompt 结果",
    ]
    assert summary.index("case\\-1") < summary.index("case\\-2")
    assert "acceptance: 20/20" in summary
    assert summary.endswith("\n")
    assert "\r" not in summary


def test_summary_escapes_user_markdown(sample_evidence):
    # Rebuild because slots intentionally do not expose a mutable __dict__.
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["prompt_name"] = "evil | [link](https://example.invalid)\nnext"
    values["prompt_path"] = "prompts/<classify>|x.md"
    values["coverage"] = {
        **sample_evidence.coverage,
        "categories": ["a * b", "c | d"],
        "saturation": "line1\r\nline2",
    }
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    escaped = render_summary(result, SummaryEvidence(**values))
    assert "evil \\| \\[link\\]\\(https://example\\.invalid\\)" in escaped
    assert "prompts/\\<classify\\>\\|x\\.md" in escaped
    assert "\r" not in escaped

def test_missing_evidence_is_rejected():
    with pytest.raises((TypeError, ValueError)):
        SummaryEvidence(  # type: ignore[call-arg]
            cycle_id="cycle-1",
            prompt_name="classify.md",
        )


def test_contradictory_acceptance_evidence_is_rejected(sample_evidence):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["comparisons"] = {
        **sample_evidence.comparisons,
        "acceptance": {"status": "passed", "reason": "all gates passed"},
    }
    with pytest.raises(ValueError, match="acceptance"):
        render_summary(
            normalize_formal_result(
                "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
            ),
            SummaryEvidence(**values),
        )


@pytest.mark.parametrize(
    "acceptance_payload",
    [
        {"status": "passed", "ran": False, "reason": "contradiction"},
        {"status": "failed", "ran": False, "reason": "contradiction"},
        {"status": "not_run", "ran": True, "reason": "contradiction"},
        {"status": "not_run", "passed": False, "reason": "contradiction"},
        {"status": "not_run", "passed": True, "reason": "contradiction"},
        {"status": "passed", "passed": False, "reason": "contradiction"},
        {"status": "failed", "passed": True, "reason": "contradiction"},
        {"status": "passed", "passed": None, "reason": "contradiction"},
    ],
)
def test_acceptance_status_ran_and_passed_must_agree(sample_evidence, acceptance_payload):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["comparisons"] = {
        "dev": sample_evidence.comparisons["dev"],
        "validation": sample_evidence.comparisons["validation"],
        "acceptance": acceptance_payload,
    }
    with pytest.raises(ValueError, match="acceptance"):
        SummaryEvidence(**values)


@pytest.mark.parametrize(
    "sensitive_key",
    [
        "accessToken",
        "AuthorizationToken",
        "rawResponse",
        "deliveryStatus",
        "AccessToken",
        "RawResponse",
        "DeliveryStatus",
        "access-token",
        "raw-response",
        "delivery-status",
    ],
)
def test_sensitive_camel_pascal_and_kebab_keys_are_omitted(sample_evidence, sensitive_key):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {
        **sample_evidence.coverage,
        sensitive_key: "top-secret",
    }
    summary = render_summary(
        normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
        SummaryEvidence(**values),
    )
    assert sensitive_key.casefold() not in summary.casefold()
    assert "top-secret" not in summary


@pytest.mark.parametrize("near_duplicate_review", ["", "   ", [], {}])
def test_near_duplicate_review_requires_meaningful_conclusion(
    sample_evidence, near_duplicate_review
):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {
        **sample_evidence.coverage,
        "near_duplicate_review": near_duplicate_review,
    }
    with pytest.raises(ValueError, match="near_duplicate"):
        render_summary(
            normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
            SummaryEvidence(**values),
        )


@pytest.mark.parametrize("exclusions", [["x"], {"x": ""}, [{"id": "x"}]])
def test_exclusions_require_an_explicit_reason(sample_evidence, exclusions):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {**sample_evidence.coverage, "exclusions": exclusions}
    with pytest.raises(ValueError, match="exclusions"):
        render_summary(
            normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
            SummaryEvidence(**values),
        )


@pytest.mark.parametrize(
    "business_key",
    ["responseType", "rawMaterial", "hashAlgorithm", "response", "raw", "hash"],
)
def test_unrelated_business_keys_are_preserved(sample_evidence, business_key):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {**sample_evidence.coverage, business_key: "business-value"}
    summary = render_summary(
        normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
        SummaryEvidence(**values),
    )
    assert business_key in summary
    assert "business\\-value" in summary


@pytest.mark.parametrize(
    "machine_key",
    [
        "delivery",
        "deliveryStatus",
        "deliveryCommit",
        "deliveryConfirmed",
        "deliveryApplied",
        "deliveryVerified",
        "delivery-status",
        "delivery-commit",
        "delivery-confirmed",
        "delivery-applied",
        "delivery-verified",
    ],
)
def test_delivery_machine_state_keys_are_omitted(sample_evidence, machine_key):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {**sample_evidence.coverage, machine_key: "confirmed"}
    summary = render_summary(
        normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
        SummaryEvidence(**values),
    )
    assert machine_key.casefold() not in summary.casefold()
    assert "confirmed" not in summary


@pytest.mark.parametrize(
    "prompt_path",
    [
        "/tmp/private/prompt.md",
        "C:\\private\\prompt.md",
        "\\\\server\\share\\prompt.md",
        "\\rooted\\prompt.md",
    ],
)
def test_rejects_all_absolute_and_rooted_prompt_paths(sample_evidence, prompt_path):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["prompt_path"] = prompt_path
    with pytest.raises(ValueError, match="relative"):
        SummaryEvidence(**values)


@pytest.mark.parametrize(
    "missing_metric",
    [
        "pass",
        "parse_error",
        "schema_error",
        "business_error",
        "schema_valid_rate",
        "run_accuracy",
        "stable_case_rate",
    ],
)
def test_metrics_require_spec_73_fields(sample_evidence, missing_metric):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    metrics = {phase: dict(payload) for phase, payload in sample_evidence.metrics.items()}
    metrics["dev"].pop(missing_metric)
    values["metrics"] = metrics
    with pytest.raises(ValueError, match="metrics"):
        render_summary(
            normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
            SummaryEvidence(**values),
        )


@pytest.mark.parametrize(
    "missing_coverage",
    [
        "categories",
        "boundaries",
        "matrix_complete",
        "near_duplicate_review",
        "exclusions",
        "saturation",
    ],
)
def test_coverage_requires_spec_73_fields(sample_evidence, missing_coverage):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    coverage = dict(sample_evidence.coverage)
    coverage.pop(missing_coverage)
    values["coverage"] = coverage
    with pytest.raises(ValueError, match="coverage"):
        render_summary(
            normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
            SummaryEvidence(**values),
        )


def test_relative_path_and_file_evidence_is_preserved_and_escaped(sample_evidence):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["coverage"] = {
        **sample_evidence.coverage,
        "paths": {"case|1": "cases/input.md"},
        "files": {"schema[1].py": "relative/file.md"},
    }
    summary = render_summary(
        normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"),
        SummaryEvidence(**values),
    )
    assert "case\\|1" in summary
    assert "schema\\[1\\]\\.py" in summary
    assert "relative/file\\.md" in summary


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-09-16T08:09:10",
        "2026-09-16T08:09:10+08:00",
        "2026-09-16T08:09:10.000Z",
        "not-a-timestamp",
    ],
)
def test_invalid_finished_timestamps_are_rejected(timestamp):
    with pytest.raises(ValueError):
        normalize_formal_result("no_change_needed", finished_at_utc=timestamp)


def test_non_run_acceptance_has_rule_explanation(sample_evidence):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["comparisons"] = {
        **sample_evidence.comparisons,
        "acceptance": {
            **sample_evidence.comparisons["acceptance"],
            "status": "not_run",
            "reason": "not run by no-change rule",
        },
    }
    result = normalize_formal_result(
        "no_change_needed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    summary = render_summary(result, SummaryEvidence(**values))
    assert "not run by no\\-change rule" in summary
    assert "0" not in summary.split("acceptance", 1)[-1].split("\n", 1)[0]


def test_rejects_absolute_prompt_path(sample_evidence):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["prompt_path"] = "C:\\private\\worktree\\prompt.md"
    with pytest.raises(ValueError, match="relative"):
        SummaryEvidence(**values)


def test_rejects_nonfinite_or_nondeterministic_evidence(sample_evidence):
    values = {field: getattr(sample_evidence, field) for field in sample_evidence.__dataclass_fields__}
    values["metrics"] = {"dev": {"run_accuracy": float("nan")}}
    with pytest.raises(ValueError, match="finite"):
        SummaryEvidence(**values)

    values["metrics"] = {"dev": {"run_accuracy": object()}}
    with pytest.raises(ValueError, match="determin"):
        SummaryEvidence(**values)


def test_write_summary_uses_exact_utc_path_and_refuses_collision(
    tmp_path: Path, sample_result, sample_evidence
):
    destination = write_summary(tmp_path, sample_result, sample_evidence)
    assert destination == tmp_path / "evaluation-summaries/2026-09-16-080910-acceptance_passed.md"
    assert destination.read_text(encoding="utf-8").endswith("\n")
    with pytest.raises(FileExistsError):
        write_summary(tmp_path, sample_result, sample_evidence)
