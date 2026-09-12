from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from typing import Literal

import pytest
import yaml
from pydantic import BaseModel, Field

from scripts.validate_cases import (
    CaseSetupError,
    CaseSuite,
    dataset_hash,
    load_case_suite,
    main,
)


class Decision(BaseModel):
    action: Literal["accept", "reject"]
    reason: str


class AliasedDecision(BaseModel):
    action: Literal["accept", "reject"] = Field(alias="actionType")
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
    dev_input: dict[str, dict[str, object]] | None = None,
    validation: list[dict[str, object]] | None = None,
    acceptance: list[dict[str, object]] | None = None,
) -> tuple[Path, Path, Path]:
    return (
        _write(
            root / "dev-cases.yaml",
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
        _write(
            root / "validation-cases.yaml",
            validation
            or [
                _case(
                    "validation-1",
                    "routing-validation",
                    input={"variables": {"split": "validation"}, "context": {}},
                )
            ],
        ),
        _write(
            root / "acceptance-cases.yaml",
            acceptance
            or [
                _case(
                    "acceptance-1",
                    "routing-acceptance",
                    input={"variables": {"split": "acceptance"}, "context": {}},
                )
            ],
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


def test_rejects_exact_duplicate_inputs_across_splits(tmp_path: Path) -> None:
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
        load_case_suite(paths, Decision)


def test_rejects_recursively_normalized_duplicate_inputs_across_splits(
    tmp_path: Path,
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
        load_case_suite(paths, Decision)


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


def test_accepts_complete_expected_object_using_production_alias(
    tmp_path: Path,
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

    suite = load_case_suite(paths, AliasedDecision)

    expected = suite.dev[0].expected
    assert isinstance(expected, AliasedDecision)
    assert expected.action == "accept"
    assert expected.reason == "matched"
    assert expected.model_fields_set == {"action", "reason"}


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
    raw[0]["input"] = {"context": {}, "variables": {"split": "dev"}}
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


def test_case_validation_cli_writes_machine_readable_suite(
    tmp_path: Path,
) -> None:
    paths = write_case_sets(tmp_path / "eval")
    output = tmp_path / "suite.json"

    assert (
        main(
            [
                "--eval-root",
                str(tmp_path / "eval"),
                "--schema",
                "tests.test_validate_cases:Decision",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "valid"
    assert payload["schema"] == "tests.test_validate_cases:Decision"
    assert payload["dataset_hash"] == dataset_hash(load_case_suite(paths, Decision))
    assert [item["id"] for item in payload["splits"]["dev"]] == ["dev-1"]
    assert payload["splits"]["dev"][0]["expect"]["output"] == {
        "action": "accept",
        "reason": "matched",
    }


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
            ]
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
