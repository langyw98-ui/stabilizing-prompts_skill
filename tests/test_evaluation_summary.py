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
            "dev": {"pass": 20, "parse_error": 0, "schema_error": 0, "business_error": 0},
            "validation": {
                "pass": 15,
                "parse_error": 0,
                "schema_error": 0,
                "business_error": 0,
            },
        },
        comparisons={
            "acceptance": {"status": "passed", "reason": "all gates passed"},
            "dev": {"status": "passed"},
            "validation": {"status": "passed"},
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
    values["coverage"] = {"categories": ["a * b", "c | d"], "saturation": "line1\r\nline2"}
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
        "dev": {"status": "passed"},
        "validation": {"status": "passed"},
        "acceptance": {"status": "not_run", "reason": "not run by no-change rule"},
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
