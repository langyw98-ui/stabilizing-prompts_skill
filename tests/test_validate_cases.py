from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel

from scripts.validate_cases import (
    CaseSetupError,
    CaseSuite,
    dataset_hash,
    load_case_suite,
)


class Decision(BaseModel):
    action: Literal["accept", "reject"]
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
    }
    value.update(extra)
    return value


def _write(path: Path, cases: list[dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")
    return path


def write_case_sets(
    root: Path,
    *,
    dev_expect: dict[str, object] | None = None,
    validation: list[dict[str, object]] | None = None,
    acceptance: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path]:
    return (
        _write(
            root / "dev-cases.yaml",
            [_case("dev-1", "routing-dev", output=dev_expect)],
        ),
        _write(
            root / "validation-cases.yaml",
            validation or [_case("validation-1", "routing-validation")],
        ),
        _write(
            root / "acceptance-cases.yaml",
            acceptance or [_case("acceptance-1", "routing-acceptance")],
        ),
    )


def test_requires_complete_schema_object(tmp_path: Path) -> None:
    paths = write_case_sets(tmp_path, dev_expect={"action": "accept"})

    with pytest.raises(CaseSetupError, match="reason"):
        load_case_suite(paths, Decision)


def test_rejects_duplicate_ids_and_cross_split_family_leakage(tmp_path: Path) -> None:
    duplicate_paths = write_case_sets(
        tmp_path / "duplicate",
        validation=[_case("dev-1", "different-family")],
    )

    with pytest.raises(CaseSetupError, match="duplicate.*id"):
        load_case_suite(duplicate_paths, Decision)

    leaking_paths = write_case_sets(
        tmp_path / "leaking",
        validation=[_case("validation-1", "routing-dev")],
    )

    with pytest.raises(CaseSetupError, match="semantic family"):
        load_case_suite(leaking_paths, Decision)


def test_loads_all_splits_with_validated_production_models(tmp_path: Path) -> None:
    paths = write_case_sets(tmp_path)

    suite = load_case_suite(paths, Decision)

    assert isinstance(suite, CaseSuite)
    assert [item.case.id for item in suite.dev] == ["dev-1"]
    assert [item.case.id for item in suite.validation] == ["validation-1"]
    assert [item.case.id for item in suite.acceptance] == ["acceptance-1"]
    assert isinstance(suite.dev[0].expected, Decision)
    assert suite.dev[0].expected.action == "accept"
    assert suite.dev[0].expected.reason == "matched"


def test_rejects_unknown_case_fields(tmp_path: Path) -> None:
    paths = write_case_sets(tmp_path)
    raw = yaml.safe_load(paths[0].read_text(encoding="utf-8"))
    raw[0]["unexpected"] = True
    paths[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(CaseSetupError, match="unexpected"):
        load_case_suite(paths, Decision)


def test_dataset_hash_is_stable_for_yaml_case_order(tmp_path: Path) -> None:
    first = write_case_sets(tmp_path / "first")
    second = write_case_sets(tmp_path / "second")

    raw = yaml.safe_load(second[0].read_text(encoding="utf-8"))
    raw[0]["input"] = {"context": {}, "variables": {}}
    second[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert dataset_hash(load_case_suite(first, Decision)) == dataset_hash(
        load_case_suite(second, Decision)
    )


def test_dataset_hash_changes_when_expected_output_changes(tmp_path: Path) -> None:
    first = write_case_sets(tmp_path / "first")
    second = write_case_sets(tmp_path / "second")
    raw = yaml.safe_load(second[0].read_text(encoding="utf-8"))
    raw[0]["expect"]["output"]["reason"] = "different"
    second[0].write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert dataset_hash(load_case_suite(first, Decision)) != dataset_hash(
        load_case_suite(second, Decision)
    )
