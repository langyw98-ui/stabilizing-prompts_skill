from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from collections.abc import Mapping
from typing import Literal

import pytest
import yaml
from pydantic import AliasChoices, BaseModel, Field, ValidationError

from scripts.validate_cases import (
    CaseCoverage,
    CaseSetupError,
    CaseSuite,
    CoverageAudit,
    CoverageObligations,
    EvalCase,
    coverage_obligations_hash,
    dataset_hash,
    load_case_split,
    load_coverage_obligations,
    load_case_suite,
    main,
    _jaccard,
    _canonical_json,
    _git_repository_root,
    _near_duplicate_pairs,
    _normalize_similarity_text,
    _scenario_key,
    _split_paths,
    _text_signature,
)


class Decision(BaseModel):
    action: Literal["accept", "reject"]
    reason: str


class AliasedDecision(BaseModel):
    action: Literal["accept", "reject"] = Field(alias="actionType")
    reason: str


class FlexibleAliasedDecision(BaseModel):
    action: Literal["accept", "reject"] = Field(
        validation_alias=AliasChoices("actionType", "action")
    )
    reason: str


def _case(
    case_id: str,
    family: str,
    *,
    output: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    value: dict[str, object] = {
        "id": case_id,
        "semantic_family": family,
        "source": ["evidence.py"],
        "input": {"variables": {}, "context": {}},
        "expect": {"output": output or {"action": "accept", "reason": "matched"}},
        "priority": "normal",
        "dimensions": ["routing"],
        "rationale": "the evidence determines the expected decision",
        "coverage": {
            "primary_obligation": "classify-input",
            "secondary_obligations": [],
            "variant": (
                "boundary"
                if case_id.startswith("validation-")
                else "natural_variation"
                if case_id.startswith("acceptance-")
                else "normal"
            ),
            "condition_id": f"{case_id}-condition",
            "distinction": None,
        },
    }
    value.update(extra)
    return value


def _write(path: Path, cases: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")
    return path


def complete_obligations_payload() -> dict[str, object]:
    categories = [
        "normal_path",
        "output_partition",
        "near_boundary",
        "field_boundary",
        "conditional_branch",
        "conflict",
        "ambiguity",
        "irrelevant_input",
        "fallback",
        "historical_regression",
        "adversarial",
    ]
    return {
        "version": 1,
        "categories": [
            {
                "category": name,
                "applicability": "required" if name == "normal_path" else "not_applicable",
                "evidence_checked": [] if name == "normal_path" else ["repository-tests"],
                "rationale": None
                if name == "normal_path"
                else f"the fixture has no evidenced {name} behavior",
            }
            for name in categories
        ],
        "obligations": [
            {
                "id": "classify-input",
                "source": ["evidence.py"],
                "category": "normal_path",
                "risk": "normal",
                "rule": "return the evidenced routing decision",
                "required_splits": {
                    "dev": ["normal", "boundary", "natural_variation"],
                    "validation": ["normal", "boundary", "natural_variation"],
                    "acceptance": ["normal", "boundary", "natural_variation"],
                },
                "variant_exclusions": {},
            }
        ],
    }


def write_obligations(root: Path, payload: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence.py").write_text("EVIDENCE = True\n", encoding="utf-8")
    path = root / "coverage-obligations.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def coverage_obligations(tmp_path: Path):
    payload = complete_obligations_payload()
    payload["obligations"][0]["required_splits"] = {
        "dev": ["normal", "boundary", "natural_variation"],
        "validation": ["normal", "boundary", "natural_variation"],
        "acceptance": ["normal", "boundary", "natural_variation"],
    }
    return load_coverage_obligations(
        write_obligations(tmp_path, payload), tmp_path
    )


@pytest.fixture
def case_files(tmp_path: Path) -> dict[str, Path]:
    paths = write_case_sets(tmp_path)
    return dict(zip(("dev", "validation", "acceptance"), paths, strict=True))


@pytest.fixture
def output_schema():
    return Decision


def read_cases(path: Path) -> list[dict[str, object]]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, list)
    return value


def write_cases(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")


def remove_coverage(path: Path, *, case_id: str) -> None:
    cases = read_cases(path)
    next(case for case in cases if case["id"] == case_id).pop("coverage")
    write_cases(path, cases)


def duplicate_scenario_with_new_input(
    case_files: Mapping[str, Path], *, source_split: str, target_split: str
) -> None:
    source = read_cases(case_files[source_split])[0]
    target_cases = read_cases(case_files[target_split])
    target_cases[0]["coverage"] = dict(source["coverage"])
    target_cases[0]["input"] = {
        "variables": {"text": "different wording only"}, "context": {}
    }
    write_cases(case_files[target_split], target_cases)


def make_case(
    split: str,
    index: int,
    *,
    obligation: str = "classify-input",
    variant: str | None = None,
    output: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one deterministic, substantively distinct fixture case."""

    default_variant = {
        "dev": "normal",
        "validation": "boundary",
        "acceptance": "natural_variation",
    }[split]
    value = _case(
        f"{split}-{index}",
        f"routing-{split}-{index}",
        output=output,
        input={
            "variables": {"split": split, "message": f"{split} message {index}"},
            "context": {"index": index},
        },
        coverage={
            "primary_obligation": obligation,
            "secondary_obligations": [],
            "variant": variant or default_variant,
            "condition_id": f"{split}-{index}-condition",
            "distinction": None,
        },
    )
    return value


def _complete_split_cases(
    split: str, provided: list[dict[str, object]] | None
) -> list[dict[str, object]]:
    """Keep legacy fixture callers valid while meeting the hard case floor."""

    cases = list(provided or [])
    present_variants = {
        case["coverage"]["variant"]
        for case in cases
        if isinstance(case.get("coverage"), dict)
    }
    required_variants = ("normal", "boundary", "natural_variation")
    template_output = None
    if cases and isinstance(cases[0].get("expect"), dict):
        raw_output = cases[0]["expect"].get("output")
        if isinstance(raw_output, dict):
            template_output = raw_output
    next_index = 1
    for required_variant in required_variants:
        if required_variant not in present_variants:
            while f"{split}-{next_index}" in {case["id"] for case in cases}:
                next_index += 1
            cases.append(
                make_case(
                    split,
                    next_index,
                    variant=required_variant,
                    output=template_output,
                )
            )
            next_index += 1
    while len(cases) < 30:
        while f"{split}-{next_index}" in {case["id"] for case in cases}:
            next_index += 1
        cases.append(make_case(split, next_index, output=template_output))
        next_index += 1
    return cases


def remove_last_case(path: Path) -> None:
    cases = read_cases(path)
    write_cases(path, cases[:-1])


def require_variant(
    obligations: CoverageObligations, obligation_id: str, *, split: str, variant: str
) -> CoverageObligations:
    payload = obligations.model_dump(mode="json")
    matches = [value for value in payload["obligations"] if value["id"] == obligation_id]
    if matches:
        matches[0]["required_splits"].setdefault(split, []).append(variant)
    else:
        payload["obligations"].append({
            "id": obligation_id,
            "source": ["evidence.py"],
            "category": "normal_path",
            "risk": "normal",
            "rule": "exercise a quota that secondary references cannot satisfy",
            "required_splits": {split: [variant]},
            "variant_exclusions": {},
        })
    return CoverageObligations.model_validate(payload)


def add_secondary_reference_to_every_case(path: Path, obligation_id: str) -> None:
    cases = read_cases(path)
    for case in cases:
        case["coverage"]["secondary_obligations"].append(obligation_id)
    write_cases(path, cases)


def make_near_duplicate_pair(
    case_files: Mapping[str, Path], *, distinction: str | None
) -> None:
    """Make the first two development cases similar without hard duplicating them."""

    cases = read_cases(case_files["dev"])
    left = cases[0]
    right = copy.deepcopy(cases[1])
    right["expect"] = copy.deepcopy(left["expect"])
    right["coverage"]["primary_obligation"] = left["coverage"][
        "primary_obligation"
    ]

    # Keep a long shared body so this helper exercises the trigram threshold;
    # punctuation-only variation must still leave distinct exact inputs.
    left_input = copy.deepcopy(left["input"])
    left_input.setdefault("variables", {})["text"] = (
        "The evidenced routing condition selects the accepted decision"
    )
    right_input = copy.deepcopy(left_input)
    right_input["variables"]["text"] += "!"
    left["input"] = left_input
    right["input"] = right_input
    left["coverage"]["distinction"] = distinction
    right["coverage"]["distinction"] = distinction
    cases[0], cases[1] = left, right
    write_cases(case_files["dev"], cases)


def test_coverage_obligations_require_every_fixed_category(tmp_path: Path) -> None:
    path = tmp_path / "coverage-obligations.yaml"
    path.write_text("version: 1\ncategories: []\nobligations: []\n", encoding="utf-8")

    with pytest.raises(CaseSetupError, match="missing categor"):
        load_coverage_obligations(path, tmp_path)


def test_not_applicable_requires_evidence_and_rationale(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["categories"][0] = {
        "category": "normal_path",
        "applicability": "not_applicable",
        "evidence_checked": [],
        "rationale": "",
    }
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="evidence_checked|rationale"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_require_top_level_fields(tmp_path: Path) -> None:
    for field in ("categories", "obligations"):
        payload = complete_obligations_payload()
        payload.pop(field)
        path = write_obligations(tmp_path / field, payload)

        with pytest.raises(CaseSetupError, match=field):
            load_coverage_obligations(path, tmp_path / field)


def test_coverage_obligations_allow_explicit_empty_obligations(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"] = []
    path = write_obligations(tmp_path, payload)

    loaded = load_coverage_obligations(path, tmp_path)

    assert loaded.obligations == []


def test_coverage_obligations_are_deeply_immutable(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["variant_exclusions"] = {
        "conflict": "the evidence has no conflict branch",
    }
    path = write_obligations(tmp_path, payload)
    loaded = load_coverage_obligations(path, tmp_path)

    with pytest.raises(TypeError):
        loaded.categories.append(loaded.categories[0])
    with pytest.raises(TypeError):
        loaded.categories[0].evidence_checked.append("new-evidence")
    with pytest.raises(TypeError):
        loaded.obligations.append(loaded.obligations[0])
    with pytest.raises(TypeError):
        loaded.obligations[0].source.append("another.py")
    with pytest.raises(TypeError):
        loaded.obligations[0].required_splits["dev"].append("boundary")
    with pytest.raises(TypeError):
        loaded.obligations[0].required_splits["dev"] = ["boundary"]
    with pytest.raises(TypeError):
        loaded.obligations[0].variant_exclusions["adversarial"] = "reason"
    with pytest.raises(ValidationError):
        loaded.obligations[0].id = "changed"


def test_coverage_obligations_reject_duplicate_categories(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["categories"].append(dict(payload["categories"][0]))
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="categor"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_duplicate_variants(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["required_splits"]["dev"] = ["normal", "normal"]
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_unknown_split_key(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["required_splits"]["other"] = ["normal"]
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="required_splits"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_declared_variant_exclusion(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["variant_exclusions"] = {
        "normal": "normal is declared in dev",
    }
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant_exclusions"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_blank_exclusion_reason(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["variant_exclusions"] = {"boundary": "  "}
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant_exclusions"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_unknown_exclusion_variant(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["variant_exclusions"] = {
        "project-specific": "the evidence has no such variant",
    }
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant_exclusions|variant"):
        load_coverage_obligations(path, tmp_path)


def test_critical_obligation_accepts_exclusions_for_unused_risky_variants(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["risk"] = "critical"
    payload["obligations"][0]["required_splits"] = {
        "dev": ["normal", "boundary"],
    }
    payload["obligations"][0]["variant_exclusions"] = {
        "conflict": "the evidence has no conflict branch",
        "adversarial": "the evidence has no adversarial branch",
    }
    path = write_obligations(tmp_path, payload)

    loaded = load_coverage_obligations(path, tmp_path)

    assert loaded.obligations[0].risk == "critical"


def test_coverage_obligations_accept_alias_and_parent_segments(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = ["./nested/../evidence.py"]
    path = write_obligations(tmp_path, payload)

    loaded = load_coverage_obligations(path, tmp_path)

    assert loaded.obligations[0].source == ["./nested/../evidence.py"]


def test_coverage_obligations_reject_source_escape(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = ["../outside-evidence.py"]
    (tmp_path.parent / "outside-evidence.py").write_text(
        "OUTSIDE = True\n", encoding="utf-8"
    )
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="source"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_symlink_source_when_supported(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-evidence.py"
    outside.write_text("OUTSIDE = True\n", encoding="utf-8")
    link = tmp_path / "linked-evidence.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = ["linked-evidence.py"]
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="source"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_source_resolve_runtime_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = ["loop.py"]
    path = write_obligations(tmp_path, payload)
    original_resolve = Path.resolve

    def raise_for_loop(
        candidate: Path, *, strict: bool = False
    ) -> Path:
        if candidate.name == "loop.py":
            raise RuntimeError("symlink loop")
        return original_resolve(candidate, strict=strict)

    monkeypatch.setattr(Path, "resolve", raise_for_loop)

    with pytest.raises(CaseSetupError, match="source"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_duplicate_ids(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"].append(dict(payload["obligations"][0]))
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="obligation.*id"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_unknown_category(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["category"] = "project-specific"
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="category"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_unknown_variant(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["required_splits"]["dev"] = ["project-specific"]
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_unknown_risk(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["risk"] = "urgent"
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="risk"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_not_applicable_category_reference(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["categories"][0] = {
        "category": "normal_path",
        "applicability": "not_applicable",
        "evidence_checked": ["repository-tests"],
        "rationale": "the production flow has no normal path",
    }
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="category"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_empty_source_list(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = []
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="source"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_missing_repository_source(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["source"] = ["missing/evidence.py"]
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="source"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_reject_empty_split_variant_list(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["required_splits"]["dev"] = []
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="required_splits|variant"):
        load_coverage_obligations(path, tmp_path)


def test_critical_obligation_requires_normal_and_risky_variant(tmp_path: Path) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["risk"] = "critical"
    payload["obligations"][0]["required_splits"] = {"dev": ["ambiguity"]}
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="normal|boundary|conflict|adversarial"):
        load_coverage_obligations(path, tmp_path)


def test_critical_obligation_requires_exclusions_for_unused_risky_variants(
    tmp_path: Path,
) -> None:
    payload = complete_obligations_payload()
    payload["obligations"][0]["risk"] = "critical"
    payload["obligations"][0]["required_splits"] = {"dev": ["normal", "boundary"]}
    path = write_obligations(tmp_path, payload)

    with pytest.raises(CaseSetupError, match="variant_exclusions"):
        load_coverage_obligations(path, tmp_path)


def test_coverage_obligations_hash_binds_exact_file_bytes(tmp_path: Path) -> None:
    path = write_obligations(tmp_path, complete_obligations_payload())

    first = coverage_obligations_hash(path)
    assert len(first) == 64
    assert coverage_obligations_hash(path) == first

    path.write_bytes(path.read_bytes() + b"\n")

    assert coverage_obligations_hash(path) != first


def write_case_sets(
    root: Path,
    *,
    dev_expect: dict[str, object] | None = None,
    dev_input: dict[str, dict[str, object]] | None = None,
    validation: list[dict[str, object]] | None = None,
    acceptance: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path]:
    return (
        _write(
            root / "dev-cases.yaml",
            _complete_split_cases(
                "dev",
                [
                    _case(
                        "dev-1",
                        "routing-dev",
                        output=dev_expect,
                        input=dev_input
                        or {"variables": {"split": "dev"}, "context": {}},
                    )
                ],
            ),
        ),
        _write(
            root / "validation-cases.yaml",
            _complete_split_cases(
                "validation",
                validation
                or [
                    _case(
                        "validation-1",
                        "routing-validation",
                        input={"variables": {"split": "validation"}, "context": {}},
                    )
                ],
            ),
        ),
        _write(
            root / "acceptance-cases.yaml",
            _complete_split_cases(
                "acceptance",
                acceptance
                or [
                    _case(
                        "acceptance-1",
                        "routing-acceptance",
                        input={"variables": {"split": "acceptance"}, "context": {}},
                    )
                ],
            ),
        ),
    )


def test_case_coverage_metadata_is_required_and_parsed(
    case_files, output_schema, coverage_obligations
) -> None:
    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    coverage = suite.dev[0].case.coverage
    assert isinstance(coverage, CaseCoverage)
    assert coverage.primary_obligation == "classify-input"
    assert _scenario_key(suite.dev[0].case) == (
        "classifyinput",
        "normal",
        "dev1condition",
    )


def test_exactly_thirty_valid_cases_produce_audit_counts(
    case_files, output_schema, coverage_obligations
) -> None:
    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    assert isinstance(suite.coverage_audit, CoverageAudit)
    assert suite.coverage_audit.counts == {
        "dev": {"total": 30, "valid": 30, "non_counting": 0},
        "validation": {"total": 30, "valid": 30, "non_counting": 0},
        "acceptance": {"total": 30, "valid": 30, "non_counting": 0},
    }


def test_thirty_one_independent_cases_are_all_counted(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases.append(make_case("dev", 31))
    write_cases(case_files["dev"], cases)

    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    assert suite.coverage_audit.counts == {
        "dev": {"total": 31, "valid": 31, "non_counting": 0},
        "validation": {"total": 30, "valid": 30, "non_counting": 0},
        "acceptance": {"total": 30, "valid": 30, "non_counting": 0},
    }


def test_each_split_requires_thirty_valid_cases(
    case_files, output_schema, coverage_obligations
) -> None:
    remove_last_case(case_files["validation"])

    with pytest.raises(CaseSetupError, match="validation.*29.*30"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_missing_required_split_variant_quota_is_reported(
    case_files, output_schema, coverage_obligations
) -> None:
    obligations = require_variant(
        coverage_obligations,
        "classify-input",
        split="validation",
        variant="adversarial",
    )

    with pytest.raises(
        CaseSetupError, match="missing.*classify-input.*validation.*adversarial"
    ):
        load_case_suite(case_files.values(), output_schema, obligations=obligations)


def test_required_category_without_obligation_fails_matrix_completeness(
    case_files, output_schema, coverage_obligations
) -> None:
    payload = coverage_obligations.model_dump(mode="json")
    payload["categories"] = [
        *payload["categories"],
    ]
    payload["categories"] = [
        {
            **category,
            "applicability": (
                "required"
                if category["category"] == "output_partition"
                else category["applicability"]
            ),
            "evidence_checked": (
                []
                if category["category"] == "output_partition"
                else category["evidence_checked"]
            ),
            "rationale": None
            if category["category"] == "output_partition"
            else category["rationale"],
        }
        for category in payload["categories"]
    ]
    obligations = CoverageObligations.model_validate(payload)

    with pytest.raises(CaseSetupError, match="output_partition.*obligation"):
        load_case_suite(
            case_files.values(), output_schema, obligations=obligations
        )


def test_required_category_with_unobserved_obligation_fails_matrix_completeness(
    case_files, output_schema, coverage_obligations
) -> None:
    payload = coverage_obligations.model_dump(mode="json")
    payload["categories"] = [
        {
            **category,
            "applicability": (
                "required"
                if category["category"] == "output_partition"
                else category["applicability"]
            ),
            "evidence_checked": (
                []
                if category["category"] == "output_partition"
                else category["evidence_checked"]
            ),
            "rationale": None
            if category["category"] == "output_partition"
            else category["rationale"],
        }
        for category in payload["categories"]
    ]
    payload["obligations"].append(
        {
            "id": "partition-input",
            "source": ["evidence.py"],
            "category": "output_partition",
            "risk": "normal",
            "rule": "partition the evidenced routing decisions",
            "required_splits": {"dev": ["normal"]},
            "variant_exclusions": {},
        }
    )
    obligations = CoverageObligations.model_validate(payload)

    with pytest.raises(CaseSetupError, match="output_partition.*primary"):
        load_case_suite(
            case_files.values(), output_schema, obligations=obligations
        )


def test_secondary_obligations_do_not_satisfy_quota(
    case_files, output_schema, coverage_obligations
) -> None:
    obligations = require_variant(
        coverage_obligations,
        "reject-unrelated",
        split="acceptance",
        variant="adversarial",
    )
    add_secondary_reference_to_every_case(
        case_files["acceptance"], "reject-unrelated"
    )

    with pytest.raises(CaseSetupError, match="missing.*acceptance.*adversarial"):
        load_case_suite(case_files.values(), output_schema, obligations=obligations)


def test_case_coverage_is_deeply_immutable_and_serializable() -> None:
    payload = {
        "primary_obligation": "classify-input",
        "secondary_obligations": ["related-obligation"],
        "variant": "normal",
        "condition_id": "stable-condition",
        "distinction": "an evidenced distinction",
    }

    coverage = CaseCoverage.model_validate(payload)

    with pytest.raises(ValidationError):
        coverage.variant = "boundary"
    with pytest.raises(TypeError):
        coverage.secondary_obligations.append("another-obligation")
    with pytest.raises(TypeError):
        coverage.secondary_obligations[0] = "changed-obligation"

    assert coverage.model_dump(mode="json") == payload
    assert json.loads(coverage.model_dump_json()) == payload


def test_case_requires_coverage_metadata(
    case_files, output_schema, coverage_obligations
) -> None:
    remove_coverage(case_files["dev"], case_id="dev-1")

    with pytest.raises(CaseSetupError, match="coverage"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_load_case_split_validates_coverage_without_opening_obligations(
    case_files, output_schema
) -> None:
    parsed = load_case_split(case_files["dev"], output_schema, "dev")

    assert parsed[0].case.coverage.variant == "normal"


def test_global_scenario_key_rejects_wording_only_variation(
    case_files, output_schema, coverage_obligations
) -> None:
    duplicate_scenario_with_new_input(
        case_files, source_split="dev", target_split="validation"
    )

    with pytest.raises(CaseSetupError, match="scenario key"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_global_scenario_key_allows_substantive_variant_and_condition_changes(
    case_files, output_schema, coverage_obligations
) -> None:
    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    assert len({_scenario_key(item.case) for item in suite.all_cases}) == 90


def test_secondary_obligations_never_change_scenario_identity() -> None:
    base = _case("dev-1", "routing-dev")
    with_secondary = _case("dev-2", "routing-dev-2")
    with_secondary["coverage"] = {
        **base["coverage"],
        "secondary_obligations": ["other-obligation"],
    }

    second = CaseCoverage.model_validate(with_secondary["coverage"])
    assert second.secondary_obligations == ["other-obligation"]

    first_case = EvalCase.model_validate(base)
    second_case = EvalCase.model_validate(with_secondary)
    assert _scenario_key(first_case) == _scenario_key(second_case)


def test_case_coverage_rejects_non_slug_condition_id(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases[0]["coverage"]["condition_id"] = "not a stable slug"
    write_cases(case_files["dev"], cases)

    with pytest.raises(CaseSetupError, match="condition_id"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_case_coverage_rejects_duplicate_secondary_or_primary_reference(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases[0]["coverage"]["secondary_obligations"] = [
        "classify-input",
        "classify-input",
    ]
    write_cases(case_files["dev"], cases)

    with pytest.raises(CaseSetupError, match="secondary_obligations"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_case_coverage_rejects_unknown_primary_obligation(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases[0]["coverage"]["primary_obligation"] = "unknown-obligation"
    write_cases(case_files["dev"], cases)

    with pytest.raises(CaseSetupError, match="unknown primary obligation"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_case_coverage_rejects_unknown_secondary_obligation(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases[0]["coverage"]["secondary_obligations"] = ["unknown-obligation"]
    write_cases(case_files["dev"], cases)

    with pytest.raises(CaseSetupError, match="unknown secondary obligation"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_case_coverage_rejects_unknown_fixed_variant(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    cases[0]["coverage"]["variant"] = "project-specific"
    write_cases(case_files["dev"], cases)

    with pytest.raises(CaseSetupError, match="variant"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_rejects_exact_duplicate_inputs_within_one_split(
    tmp_path: Path, coverage_obligations
) -> None:
    paths = write_case_sets(tmp_path)
    cases = read_cases(paths[0])
    duplicate = dict(cases[0])
    duplicate["id"] = "dev-31"
    duplicate["semantic_family"] = "independent-dev-family"
    duplicate["coverage"] = {
        **cases[0]["coverage"],
        "condition_id": "another-condition",
    }
    cases.append(duplicate)
    write_cases(paths[0], cases)

    with pytest.raises(CaseSetupError, match="input fingerprint"):
        load_case_suite(paths, Decision, obligations=coverage_obligations)


def test_requires_complete_schema_object(tmp_path: Path, coverage_obligations) -> None:
    paths = write_case_sets(tmp_path, dev_expect={"action": "accept"})

    with pytest.raises(CaseSetupError, match="reason"):
        load_case_suite(paths, Decision, obligations=coverage_obligations)


def test_rejects_duplicate_ids_and_cross_split_family_leakage(
    tmp_path: Path, coverage_obligations
) -> None:
    duplicate_paths = write_case_sets(
        tmp_path / "duplicate",
        validation=[_case("dev-1", "different-family")],
    )

    with pytest.raises(CaseSetupError, match="duplicate.*id"):
        load_case_suite(duplicate_paths, Decision, obligations=coverage_obligations)

    leaking_paths = write_case_sets(
        tmp_path / "leaking",
        validation=[_case("validation-1", "routing-dev")],
    )

    with pytest.raises(CaseSetupError, match="semantic family"):
        load_case_suite(leaking_paths, Decision, obligations=coverage_obligations)


def test_rejects_exact_duplicate_inputs_across_splits(
    tmp_path: Path, coverage_obligations
) -> None:
    paths = write_case_sets(
        tmp_path,
        validation=[
            _case(
                "validation-1",
                "different-validation-family",
                input={"variables": {}, "context": {}},
            )
        ],
        dev_input={"variables": {}, "context": {}},
    )

    with pytest.raises(CaseSetupError, match="input fingerprint"):
        load_case_suite(paths, Decision, obligations=coverage_obligations)


def test_rejects_recursively_normalized_duplicate_inputs_across_splits(
    tmp_path: Path, coverage_obligations
) -> None:
    paths = write_case_sets(
        tmp_path,
        validation=[
            _case(
                "validation-1",
                "another-validation-family",
                input={
                    "variables": {"message": "  ACCEPT   VALUE  "},
                    "context": {"region": "US"},
                },
            )
        ],
    )
    raw = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    raw[0]["input"] = {
        "variables": {"message": "accept value"},
        "context": {"region": "  us  "},
    }
    paths[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(CaseSetupError, match="input fingerprint"):
        load_case_suite(paths, Decision, obligations=coverage_obligations)


def test_loads_all_splits_with_validated_production_models(
    tmp_path: Path, coverage_obligations
) -> None:
    paths = write_case_sets(tmp_path)

    suite = load_case_suite(paths, Decision, obligations=coverage_obligations)

    assert isinstance(suite, CaseSuite)
    assert len(suite.dev) == 30
    assert len(suite.validation) == 30
    assert len(suite.acceptance) == 30
    assert suite.dev[0].case.id == "dev-1"
    assert suite.validation[0].case.id == "validation-1"
    assert suite.acceptance[0].case.id == "acceptance-1"
    assert isinstance(suite.dev[0].expected, Decision)
    assert suite.dev[0].expected.action == "accept"
    assert suite.dev[0].expected.reason == "matched"


def test_accepts_complete_expected_object_using_production_alias(
    tmp_path: Path, coverage_obligations
) -> None:
    alias_output = {"actionType": "accept", "reason": "matched"}
    paths = write_case_sets(
        tmp_path,
        dev_expect=alias_output,
        validation=[
            _case(
                "validation-1",
                "routing-validation",
                output=alias_output,
                input={"variables": {"split": "validation"}, "context": {}},
            )
        ],
        acceptance=[
            _case(
                "acceptance-1",
                "routing-acceptance",
                output=alias_output,
                input={"variables": {"split": "acceptance"}, "context": {}},
            )
        ],
    )

    suite = load_case_suite(paths, AliasedDecision, obligations=coverage_obligations)

    expected = suite.dev[0].expected
    assert isinstance(expected, AliasedDecision)
    assert expected.action == "accept"
    assert expected.reason == "matched"
    assert expected.model_fields_set == {"action", "reason"}


def test_rejects_unknown_case_fields(tmp_path: Path, coverage_obligations) -> None:
    paths = write_case_sets(tmp_path)
    raw = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    raw[0]["unexpected"] = True
    paths[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(CaseSetupError, match="unexpected"):
        load_case_suite(paths, Decision, obligations=coverage_obligations)


def test_dataset_hash_is_stable_for_yaml_case_order(
    tmp_path: Path, coverage_obligations
) -> None:
    first = write_case_sets(tmp_path / "first")
    second = write_case_sets(tmp_path / "second")

    raw = yaml.safe_load(second[0].read_text(encoding="utf-8"))
    raw[0]["input"] = {"context": {}, "variables": {"split": "dev"}}
    second[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert dataset_hash(
        load_case_suite(first, Decision, obligations=coverage_obligations)
    ) == dataset_hash(
        load_case_suite(second, Decision, obligations=coverage_obligations)
    )


def test_dataset_hash_changes_when_expected_output_changes(
    tmp_path: Path, coverage_obligations
) -> None:
    first = write_case_sets(tmp_path / "first")
    second = write_case_sets(tmp_path / "second")
    raw = yaml.safe_load(second[0].read_text(encoding="utf-8"))
    raw[0]["expect"]["output"]["reason"] = "different"
    second[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert dataset_hash(
        load_case_suite(first, Decision, obligations=coverage_obligations)
    ) != dataset_hash(
        load_case_suite(second, Decision, obligations=coverage_obligations)
    )


def test_near_duplicate_without_distinction_fails(
    case_files, output_schema, coverage_obligations
) -> None:
    make_near_duplicate_pair(case_files, distinction=None)

    with pytest.raises(CaseSetupError, match="near duplicate.*distinction"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_near_duplicate_with_distinction_requires_review(
    case_files, output_schema, coverage_obligations
) -> None:
    distinction = "different evidenced decision boundary"
    make_near_duplicate_pair(case_files, distinction=distinction)

    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    pairs = suite.coverage_audit.near_duplicates
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["left"] == {"split": "dev", "id": "dev-1"}
    assert pair["right"] == {"split": "dev", "id": "dev-2"}
    assert pair["primary_obligation"] == "classify-input"
    assert pair["similarity"] >= 0.85
    assert pair["distinction"] == {"left": distinction, "right": distinction}
    assert suite.coverage_audit.mechanical_gates["near_duplicate_explanations"]


def test_near_duplicate_pair_order_is_split_then_case_id(
    case_files, output_schema, coverage_obligations
) -> None:
    make_near_duplicate_pair(case_files, distinction="dev distinction")
    validation_cases = read_cases(case_files["validation"])
    left = validation_cases[0]
    right = copy.deepcopy(validation_cases[1])
    left["input"]["variables"]["text"] = "The evidenced validation boundary selects a decision"
    right["input"] = copy.deepcopy(left["input"])
    right["input"]["variables"]["text"] += "?"
    right["expect"] = copy.deepcopy(left["expect"])
    right["coverage"]["primary_obligation"] = left["coverage"][
        "primary_obligation"
    ]
    left["coverage"]["distinction"] = "validation distinction"
    right["coverage"]["distinction"] = "validation distinction"
    validation_cases[0], validation_cases[1] = left, right
    write_cases(case_files["validation"], validation_cases)

    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    pairs = suite.coverage_audit.near_duplicates
    assert _near_duplicate_pairs(suite.splits) == pairs
    assert [(pair["left"], pair["right"]) for pair in pairs] == [
        (
            {"split": "dev", "id": "dev-1"},
            {"split": "dev", "id": "dev-2"},
        ),
        (
            {"split": "validation", "id": "validation-1"},
            {"split": "validation", "id": "validation-2"},
        ),
    ]


def test_near_duplicate_similarity_normalizes_nfkc_case_punctuation_and_space() -> None:
    assert _normalize_similarity_text(" ＡＢＣ，\tD！  E\n") == "abc d e"


def test_jaccard_uses_inclusive_point_eighty_five_threshold() -> None:
    left = frozenset("abcdefghijklmnopq")
    right = frozenset("abcdefghijklmnopqrst")

    assert _jaccard(left, right) == pytest.approx(0.85)
    assert _jaccard(left, right) >= 0.85
    assert _jaccard(frozenset({"x"}), frozenset({"y"})) < 0.85


def test_text_signature_preserves_multiple_string_paths_and_non_string_leaves() -> None:
    first = {
        "variables": {"title": "One", "text": "Two"},
        "context": {"region": "US", "attempt": 1},
        "items": ["Three", {"enabled": True}],
    }
    reordered = {
        "items": ["Three", {"enabled": True}],
        "context": {"attempt": 1, "region": "US"},
        "variables": {"text": "Two", "title": "One"},
    }
    changed_scalar = copy.deepcopy(reordered)
    changed_scalar["context"]["attempt"] = 2

    assert _text_signature(first) == _text_signature(reordered)
    assert _text_signature(first)[0] != _text_signature(changed_scalar)[0]


def test_text_signature_handles_short_strings() -> None:
    first = _text_signature({"text": "Hi"})
    assert _normalize_similarity_text("Hi") == "hi"
    assert first[1]
    assert first == _text_signature({"text": "hi"})
    assert _jaccard(first[1], first[1]) == 1.0


def test_near_duplicate_requires_same_expected_and_scalar_signature(
    case_files, output_schema, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    left = cases[0]
    right = copy.deepcopy(cases[1])
    right["coverage"]["primary_obligation"] = left["coverage"][
        "primary_obligation"
    ]
    right["input"] = copy.deepcopy(left["input"])
    right["input"]["variables"]["text"] = "A wholly unrelated decision boundary"
    left["input"]["variables"]["text"] = "A shared evidenced decision boundary"
    right["input"]["context"]["extra"] = 9
    right["coverage"]["distinction"] = "different scalar branch"
    left["coverage"]["distinction"] = "different scalar branch"
    cases[0], cases[1] = left, right
    write_cases(case_files["dev"], cases)

    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )

    assert suite.coverage_audit.near_duplicates == ()


def test_near_duplicate_uses_normalized_production_expected_object(
    case_files, coverage_obligations
) -> None:
    cases = read_cases(case_files["dev"])
    left = cases[0]
    right = copy.deepcopy(cases[1])
    right["coverage"]["primary_obligation"] = left["coverage"][
        "primary_obligation"
    ]
    left["input"]["variables"]["text"] = "A shared evidenced decision boundary"
    right["input"] = copy.deepcopy(left["input"])
    right["input"]["variables"]["text"] += "?"
    left["coverage"]["distinction"] = "same production decision, different alias spelling"
    right["coverage"]["distinction"] = "same production decision, different alias spelling"
    left["expect"]["output"] = {"actionType": "accept", "reason": "matched"}
    right["expect"]["output"] = {"action": "accept", "reason": "matched"}
    cases[0], cases[1] = left, right
    write_cases(case_files["dev"], cases)

    # AliasedDecision is needed to prove that aliases normalize to one
    # production expected object before grouping.
    suite = load_case_suite(
        case_files.values(), FlexibleAliasedDecision, obligations=coverage_obligations
    )

    assert suite.coverage_audit.near_duplicates


def test_case_validation_cli_writes_machine_readable_suite(
    tmp_path: Path, coverage_obligations
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())
    output = tmp_path / "suite.json"

    assert (
        main(
            [
                "--eval-root",
                    str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "valid"
    assert payload["requires_user_review"] is False
    assert payload["schema"] == "tests.test_validate_cases:Decision"
    suite = load_case_suite(paths, Decision, obligations=coverage_obligations)
    assert payload["case_suite_hash"] == dataset_hash(suite)
    assert payload["coverage_obligations_hash"] == coverage_obligations_hash(
        eval_root / "coverage-obligations.yaml"
    )
    assert payload["case_file_hashes"] == {
        split: hashlib.sha256(paths[index].read_bytes()).hexdigest()
        for index, split in enumerate(("dev", "validation", "acceptance"))
    }
    assert payload["counts"] == suite.coverage_audit.counts
    assert payload["coverage"]["distributions"] == suite.coverage_audit.distributions
    assert payload["coverage"]["missing_quotas"] == []
    assert payload["coverage"]["mechanical_gates"] == suite.coverage_audit.mechanical_gates
    assert payload["duplicates"] == {"hard": [], "near": []}
    assert len(payload["splits"]["dev"]) == 30
    assert payload["splits"]["dev"][0]["id"] == "dev-1"
    assert payload["splits"]["dev"][0]["expect"]["output"] == {
        "action": "accept",
        "reason": "matched",
    }
    category_rows = payload["coverage"]["categories"]
    assert category_rows[0]["category"] == "adversarial"
    assert all(
        {"category", "applicability", "evidence_checked", "rationale"}
        <= set(row)
        for row in category_rows
    )
    assert payload["coverage"]["matrix"] == payload["coverage_matrix"]
    assert payload["coverage"]["matrix"]["missing_categories"] == []
    assert payload["coverage"]["matrix"]["missing_quotas"] == []


def test_case_validation_cli_reports_structured_input_and_scenario_conflicts(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())

    cases = read_cases(paths[0])
    cases[1]["input"] = copy.deepcopy(cases[0]["input"])
    write_cases(paths[0], cases)
    output = tmp_path / "suite-error.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert payload["duplicates"]["hard"]
    assert payload["duplicates"]["hard"][0]["kind"] == "input_fingerprint"
    assert payload["conflicts"]["input"] == payload["duplicates"]["hard"]
    assert {
        "left",
        "right",
        "fingerprint",
        "kind",
    } <= set(payload["duplicates"]["hard"][0])

    # A distinct input with the same normalized scenario is a separate hard
    # conflict and is reported through the same deterministic error contract.
    cases = read_cases(paths[0])
    cases[1]["input"] = {
        "variables": {"split": "dev", "message": "different evidence"},
        "context": {},
    }
    cases[1]["coverage"] = copy.deepcopy(cases[0]["coverage"])
    write_cases(paths[0], cases)
    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert any(
        item["kind"] == "scenario_key" for item in payload["duplicates"]["hard"]
    )
    assert payload["conflicts"]["scenario"]


def test_case_validation_cli_redacts_structured_conflict_fingerprints(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())

    secret_values = (
        "password-value-that-must-not-leak",
        "nested-api-token-that-must-not-leak",
        "secret-value-that-must-not-leak",
        "access-token-value-that-must-not-leak",
    )
    secret_input = {
        "variables": {
            "password": secret_values[0],
            "nested": {
                "api_token": secret_values[1],
                "secret": secret_values[2],
            },
        },
        "context": {"credentials": {"access_token": secret_values[3]}},
    }
    cases = read_cases(paths[0])
    cases[0]["input"] = copy.deepcopy(secret_input)
    cases[1]["input"] = copy.deepcopy(secret_input)
    write_cases(paths[0], cases)
    output = tmp_path / "suite-error.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )

    output_text = output.read_text(encoding="utf-8")
    assert all(secret not in output_text for secret in secret_values)
    payload = json.loads(output_text)
    conflict = payload["duplicates"]["hard"][0]
    fingerprint = conflict["fingerprint"]
    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)
    assert conflict["kind"] == "input_fingerprint"
    assert conflict["left"] == {"split": "dev", "id": "dev-1"}
    assert conflict["right"] == {"split": "dev", "id": "dev-2"}


def test_production_expected_normalization_preserves_pydantic_json_scalars() -> None:
    code = (
        "import json\n"
        "from datetime import date, datetime, time, timezone\n"
        "from decimal import Decimal\n"
        "from enum import Enum\n"
        "from pathlib import Path\n"
        "from uuid import UUID\n"
        "from pydantic import BaseModel\n"
        "from scripts.validate_cases import _canonical_expected\n"
        "class Kind(str, Enum):\n"
        "    alpha = 'alpha'\n"
        "class Output(BaseModel):\n"
        "    labels: set[str]\n"
        "    frozen: frozenset[int]\n"
        "    amount: Decimal\n"
        "    identifier: UUID\n"
        "    payload: bytes\n"
        "    happened: datetime\n"
        "    day: date\n"
        "    at: time\n"
        "    kind: Kind\n"
        "    location: Path\n"
        "value = Output(\n"
        "    labels={'beta', 'alpha'}, frozen=frozenset({3, 1, 2}),\n"
        "    amount=Decimal('1.20'),\n"
        "    identifier=UUID('12345678-1234-5678-1234-567812345678'),\n"
        "    payload=b'hello',\n"
        "    happened=datetime(2024, 1, 2, 3, 4, 5, 678901, tzinfo=timezone.utc),\n"
        "    day=date(2024, 1, 2), at=time(3, 4, 5, 678901),\n"
        "    kind=Kind.alpha, location=Path('fixtures/case.yaml'),\n"
        ")\n"
        "raw = value.model_dump(mode='json')\n"
        "raw['labels'] = sorted(raw['labels'])\n"
        "raw['frozen'] = sorted(raw['frozen'])\n"
        "print(_canonical_expected(value))\n"
        "print(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')))\n"
    )
    canonical_outputs = []
    expected_outputs = []
    for seed in ("21", "22", "23"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        canonical_outputs.append(lines[0])
        expected_outputs.append(lines[1])

    assert canonical_outputs[0] == canonical_outputs[1] == canonical_outputs[2]
    canonical = json.loads(canonical_outputs[0])
    expected = json.loads(expected_outputs[0])
    assert canonical == expected


def test_canonical_serializer_handles_finite_and_nonfinite_float_values() -> None:
    code = (
        "import json\n"
        "from pydantic import BaseModel\n"
        "from scripts.validate_cases import _canonical_expected\n"
        "class Output(BaseModel):\n"
        "    finite: float\n"
        "    nan: float\n"
        "    positive_inf: float\n"
        "    negative_inf: float\n"
        "value = Output(finite=1.25, nan=float('nan'), "
        "positive_inf=float('inf'), negative_inf=-float('inf'))\n"
        "print(_canonical_expected(value))\n"
        "print(value.model_dump_json())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=True,
    )

    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == json.loads(lines[1])
    assert json.loads(lines[0]) == {
        "finite": 1.25,
        "nan": None,
        "positive_inf": None,
        "negative_inf": None,
    }
    assert _canonical_json({"finite": 1.25}, label="float test") == (
        '{"finite":1.25}'
    )


def test_case_validation_cli_accepts_finite_float_inputs_without_recursion_error(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())
    cases = read_cases(paths[0])
    cases[0]["input"]["context"].update(
        {
            "threshold": 1.25,
            "nan": float("nan"),
            "positive_inf": float("inf"),
            "negative_inf": -float("inf"),
        }
    )
    write_cases(paths[0], cases)
    output = tmp_path / "suite.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 0
    )
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "valid"


def test_production_expected_normalization_uses_paired_json_set_values() -> None:
    code = (
        "import json\n"
        "from pydantic import BaseModel, ConfigDict, field_serializer\n"
        "from pathlib import Path\n"
        "from scripts.validate_cases import _canonical_expected\n"
        "class Output(BaseModel):\n"
        "    model_config = ConfigDict(ser_json_bytes='base64')\n"
        "    bytes_values: set[bytes | str]\n"
        "    path_values: set[Path | str]\n"
        "    json_values: set[str]\n"
        "    @field_serializer('json_values', when_used='json')\n"
        "    def serialize_json_values(self, values: set[str]) -> list[str]:\n"
        "        return [f'json:{value}' for value in values]\n"
        "value = Output(\n"
        "    bytes_values={b'a', 'a'}, path_values={Path('a'), 'a'},\n"
        "    json_values={'beta', 'alpha'},\n"
        ")\n"
        "raw = value.model_dump(mode='json')\n"
        "for key in ('bytes_values', 'path_values', 'json_values'):\n"
        "    raw[key] = sorted(raw[key])\n"
        "print(_canonical_expected(value))\n"
        "print(json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(',', ':')))\n"
    )
    canonical_outputs = []
    expected_outputs = []
    for seed in ("31", "32", "33"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.splitlines()
        assert len(lines) == 2
        canonical_outputs.append(lines[0])
        expected_outputs.append(lines[1])

    assert canonical_outputs[0] == canonical_outputs[1] == canonical_outputs[2]
    canonical = json.loads(canonical_outputs[0])
    expected = json.loads(expected_outputs[0])
    assert canonical == expected
    assert canonical["bytes_values"] == ["YQ==", "a"]
    assert canonical["json_values"] == ["json:alpha", "json:beta"]
    assert canonical["path_values"] == ["a", "a"]


def test_case_validation_cli_redacts_semantic_family_conflicts(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())

    secret_family = "family-derived-from-password-token-secret"
    dev_cases = read_cases(paths[0])
    validation_cases = read_cases(paths[1])
    dev_cases[0]["semantic_family"] = secret_family
    validation_cases[0]["semantic_family"] = secret_family
    write_cases(paths[0], dev_cases)
    write_cases(paths[1], validation_cases)
    output = tmp_path / "suite-error.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )

    output_text = output.read_text(encoding="utf-8")
    assert secret_family not in output_text
    payload = json.loads(output_text)
    conflict = payload["conflicts"]["semantic_family"][0]
    fingerprint = conflict["fingerprint"]
    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)
    assert fingerprint == hashlib.sha256(
        "familyderivedfrompasswordtokensecret".encode("utf-8")
    ).hexdigest()
    assert conflict["kind"] == "semantic_family"
    assert conflict["left"] == {"split": "dev", "id": "dev-1"}
    assert conflict["right"] == {"split": "validation", "id": "validation-1"}


def test_case_validation_cli_validates_obligations_before_schema_import(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    (eval_root / "side_effect_schema.py").write_text(
        "from pathlib import Path\n"
        "Path(__file__).with_name('schema-imported.marker').write_text('bad')\n"
        "from pydantic import BaseModel\n"
        "class Decision(BaseModel):\n"
        "    action: str\n"
        "    reason: str\n",
        encoding="utf-8",
    )
    (eval_root / "coverage-obligations.yaml").write_text(
        "version: 1\ncategories: []\nobligations: []\n", encoding="utf-8"
    )
    output = tmp_path / "suite-error.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "side_effect_schema:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )
    assert not (eval_root / "schema-imported.marker").exists()


def test_canonical_serializer_sorts_unordered_values_across_hash_seeds() -> None:
    code = (
        "from scripts.validate_cases import _canonical_json; "
        "print(_canonical_json({'values': {'alpha', 'beta', 'gamma'}, "
        "'frozen': frozenset({3, 1, 2})}, label='test'))"
    )
    outputs = []
    for seed in ("1", "2", "3"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0] == (
        '{"frozen":[1,2,3],"values":["alpha","beta","gamma"]}'
    )


def test_production_expected_normalization_sorts_set_fields_across_hash_seeds() -> None:
    code = (
        "from pydantic import BaseModel\n"
        "from scripts.validate_cases import _canonical_expected\n"
        "class Output(BaseModel):\n"
        "    labels: set[str]\n"
        "print(_canonical_expected(Output(labels={'alpha', 'beta', 'gamma'})))"
    )
    outputs = []
    for seed in ("11", "12", "13"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0] == '{"labels":["alpha","beta","gamma"]}'


def test_split_paths_reject_unordered_iterables() -> None:
    with pytest.raises(CaseSetupError, match="ordered"):
        _split_paths({"dev.yaml", "validation.yaml", "acceptance.yaml"})


def test_repository_root_resolution_fails_closed_without_explicit_injection(
    tmp_path: Path,
) -> None:
    with pytest.raises(CaseSetupError, match="repository root"):
        _git_repository_root(tmp_path)

    assert _git_repository_root(tmp_path, explicit_root=tmp_path) == tmp_path.resolve()


def test_case_validation_cli_reports_explained_near_duplicates_for_review(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    paths = write_case_sets(eval_root)
    write_obligations(eval_root, complete_obligations_payload())
    case_files = dict(zip(("dev", "validation", "acceptance"), paths, strict=True))
    make_near_duplicate_pair(
        case_files, distinction="different evidenced decision boundary"
    )
    output = tmp_path / "suite.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "valid"
    assert payload["requires_user_review"] is True
    assert len(payload["duplicates"]["near"]) == 1
    assert payload["duplicates"]["near"][0]["similarity"] >= 0.85


def test_case_validation_cli_writes_explicit_error_for_invalid_assets(
    tmp_path: Path,
) -> None:
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    output = tmp_path / "suite-error.json"

    assert (
        main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ],
            repo_root=eval_root,
        )
        == 2
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "error" in payload and payload["error"]


def test_case_validation_cli_help_and_unknown_argument_are_real_argparse_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    help_result = subprocess.run(
        [sys.executable, "-m", "scripts.validate_cases", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    garbage_result = subprocess.run(
        [sys.executable, "-m", "scripts.validate_cases", "--garbage"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "--schema" in help_result.stdout
    assert garbage_result.returncode != 0
