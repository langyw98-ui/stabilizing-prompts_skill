from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
from dataclasses import replace
import subprocess
import sys

import pytest
from pydantic import BaseModel, Field, PrivateAttr

from scripts.run_prompt_eval import (
    SlotResult,
    load_adapter,
    load_manifest,
    new_manifest,
    persist_manifest,
    record_slot_result,
)
from scripts.score_results import CaseScore, ScoreError, field_diff, score_run


class Decision(BaseModel):
    action: str
    reason: str


class DecisionWithPrivateState(BaseModel):
    action: str
    reason: str
    _source: str = PrivateAttr(default="expected")


class AliasedDecision(BaseModel):
    action: str = Field(alias="actionType")
    reason: str


class DecisionWithSecretPrivateState(BaseModel):
    action: str
    reason: str
    _authorization_token: str = PrivateAttr(default="expected-token")


def _case(case_id: str, *, priority: str = "normal") -> dict[str, object]:
    return {
        "id": case_id,
        "semantic_family": f"family-{case_id}",
        "source": ["production.py"],
        "input": {"variables": {"case": case_id}, "context": {}},
        "expect": {"output": {"action": "accept", "reason": "matched"}},
        "priority": priority,
        "dimensions": ["routing"],
        "rationale": "the production evidence determines this decision",
    }


def _completed_run(kinds: list[str], *, repeats: int = 2):
    """Build two cases whose four slots expose all scoring dimensions."""

    assert len(kinds) == 4
    cases = [_case("a"), _case("b")]
    manifest = new_manifest(
        cases=cases,
        repeats=repeats,
        prompt_hash="prompt-hash",
        schema=Decision,
    )
    parsed = Decision(action="accept", reason="matched")
    wrong = Decision(action="reject", reason="matched")
    for slot, kind in zip(manifest.slots, kinds, strict=True):
        result = SlotResult(
            kind=kind,
            parsed=parsed if kind == "pass" else wrong if kind == "business_error" else None,
            detail="missing_payload" if kind == "parse_error" else None,
        )
        manifest = record_slot_result(manifest, slot.key, result)
    return manifest


def test_metrics_distinguish_schema_validity_accuracy_and_stability():
    run = _completed_run(["pass", "business_error", "pass", "schema_error"])

    metrics = score_run(run, Decision)

    assert metrics.schema_valid_rate == Decimal("0.75")
    assert metrics.run_accuracy == Decimal("0.50")
    assert metrics.stable_case_rate == Decimal("0.00")
    assert metrics.scored_responses == 4
    assert metrics.pass_count == 2
    assert metrics.business_error_count == 1
    assert metrics.schema_error_count == 1
    assert metrics.counts == {
        "pass": 2,
        "parse_error": 0,
        "schema_error": 1,
        "business_error": 1,
    }
    assert metrics.case_score("a").passed == 1
    assert metrics.case_scores["a"].passed == 1  # type: ignore[index]


def test_field_diff_uses_complete_objects():
    expected = Decision(action="accept", reason="matched")
    actual = Decision(action="reject", reason="matched")

    assert field_diff(expected, actual) == [
        {"path": "action", "expected": "accept", "actual": "reject"}
    ]


def test_field_diff_preserves_production_schema_field_order():
    class OrderedDecision(BaseModel):
        zulu: str
        alpha: str

    expected = OrderedDecision(zulu="expected-z", alpha="expected-a")
    actual = OrderedDecision(zulu="actual-z", alpha="actual-a")

    assert [item["path"] for item in field_diff(expected, actual)] == [
        "zulu",
        "alpha",
    ]


def test_field_diff_reports_redacted_model_state_when_private_attr_differs():
    expected = DecisionWithSecretPrivateState(action="accept", reason="matched")
    actual = DecisionWithSecretPrivateState(action="accept", reason="matched")
    expected._authorization_token = "expected-secret-token"
    actual._authorization_token = "actual-secret-token"

    differences = field_diff(expected, actual)

    assert differences
    assert differences[0]["path"] == "$model_state"
    rendered = repr(differences)
    assert "expected-secret-token" not in rendered
    assert "actual-secret-token" not in rendered
    assert "REDACTED" in rendered


def test_scoring_uses_complete_pydantic_object_equality():
    run = new_manifest(
        cases=[_case("private")],
        repeats=1,
        prompt_hash="prompt-hash",
        schema=DecisionWithPrivateState,
    )
    actual = DecisionWithPrivateState(action="accept", reason="matched")
    actual._source = "candidate"
    slot = run.slots[0]
    run = replace(
        run,
        results={
            slot.key: SlotResult(
                kind="pass",
                parsed=actual,
                status="complete",
                slot_key=slot.key,
            )
        },
        status="complete",
    )

    metrics = score_run(run, DecisionWithPrivateState)

    assert metrics.pass_count == 0
    assert metrics.business_error_count == 1


def test_scoring_reconstructs_persisted_alias_normalized_objects():
    case = _case("aliased")
    case["expect"] = {"output": {"actionType": "accept", "reason": "matched"}}
    run = new_manifest(
        cases=[case],
        repeats=1,
        prompt_hash="prompt-hash",
        schema=AliasedDecision,
    )
    slot = run.slots[0]
    run = record_slot_result(
        run,
        slot.key,
        SlotResult(
            kind="pass",
            parsed=AliasedDecision(actionType="accept", reason="matched"),
        ),
    )

    metrics = score_run(run, AliasedDecision)

    assert metrics.run_accuracy == Decimal("1")


def test_score_rejects_incomplete_run_without_metrics():
    run = new_manifest(
        cases=[_case("a")], repeats=1, prompt_hash="prompt-hash", schema=Decision
    )

    with pytest.raises(ScoreError, match="incomplete"):
        score_run(run, Decision)


def test_score_rejects_non_scoring_result_without_metrics():
    run = new_manifest(
        cases=[_case("a")], repeats=1, prompt_hash="prompt-hash", schema=Decision
    )
    run = record_slot_result(
        run,
        run.slots[0].key,
        SlotResult(kind="transport_error", detail="temporary outage"),
    )

    with pytest.raises(ScoreError, match="non-scoring"):
        score_run(run, Decision)


def test_score_requires_recorded_production_schema_reference():
    run = new_manifest(
        cases=[_case("a")], repeats=1, prompt_hash="prompt-hash", schema=Decision
    )
    run = record_slot_result(
        run,
        run.slots[0].key,
        SlotResult(kind="pass", parsed=Decision(action="accept", reason="matched")),
    )
    # A completed manifest without the recorded reference cannot be safely
    # compared to production objects across a process boundary.
    from dataclasses import replace

    run = replace(run, schema_import=None)

    with pytest.raises(ScoreError, match="schema import"):
        score_run(run, Decision)


def test_score_can_resolve_the_recorded_schema_reference():
    run = _completed_run(["pass", "pass", "pass", "pass"])

    metrics = score_run(run)

    assert metrics.run_accuracy == Decimal("1")


def test_score_validates_expected_object_even_when_every_response_is_parse_error():
    run = _completed_run(["parse_error", "parse_error", "parse_error", "parse_error"])
    missing_expected = dict(run.case_data)
    first = dict(missing_expected["a"])
    first.pop("expected", None)
    first.pop("expect", None)
    missing_expected["a"] = first
    run = replace(run, case_data=missing_expected)

    with pytest.raises(ScoreError, match="expected output"):
        score_run(run, Decision)


def test_case_score_can_report_stability_from_counts():
    score = CaseScore(case_id="case-1", repeats=5, pass_count=5)

    assert score.stable is True


def test_score_cli_writes_metrics_report_without_invoking_model(tmp_path: Path):
    from scripts.score_results import main

    manifest_path = tmp_path / "run.json"
    run = _completed_run(["pass", "pass", "pass", "pass"])
    persist_manifest(run, manifest_path)
    report_path = tmp_path / "reports" / "score.json"

    assert main(["--manifest", str(manifest_path), "--report", str(report_path)]) == 0
    assert '"run_accuracy": "1"' in report_path.read_text(encoding="utf-8")


def test_score_cli_reconstructs_adapter_schema_in_a_fresh_process(tmp_path: Path):
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
    schema = prepare_call(prompt, _case("adapter"))["schema"]
    manifest_path = eval_root / "run.json"
    manifest = new_manifest(
        [_case("adapter")],
        repeats=1,
        prompt_hash=hashlib.sha256(prompt.read_bytes()).hexdigest(),
        prompt_path=prompt,
        schema=schema,
        manifest_path=manifest_path,
        dataset="external",
        cycle_id="cycle-1",
    )
    manifest = record_slot_result(
        manifest,
        manifest.slots[0].key,
        SlotResult(kind="pass", parsed=schema(action="accept", reason="matched")),
    )
    persist_manifest(manifest, manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_import"].startswith("adapter-file-v1|")

    report_path = eval_root / "score.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.score_results",
            "--manifest",
            str(manifest_path),
            "--report",
            str(report_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert '"run_accuracy": "1"' in report_path.read_text(encoding="utf-8")


def test_score_rejects_adapter_schema_reference_outside_manifest_boundary(tmp_path: Path):
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    outside = tmp_path / "outside" / "adapter.py"
    outside.parent.mkdir()
    outside.write_text(
        "from pydantic import BaseModel\n"
        "class ProductionDecision(BaseModel):\n"
        "    action: str\n"
        "    reason: str\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    reference = f"adapter-file-v1|{outside.resolve()}|{digest}|ProductionDecision"
    prompt = eval_root / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    manifest = new_manifest(
        [_case("boundary")],
        repeats=1,
        prompt_hash="prompt-hash",
        prompt_path=prompt,
        schema=Decision,
        manifest_path=eval_root / "run.json",
        dataset="external",
        cycle_id="cycle-1",
    )
    manifest = replace(manifest, schema_import=reference)
    manifest = record_slot_result(
        manifest,
        manifest.slots[0].key,
        SlotResult(kind="pass", parsed=Decision(action="accept", reason="matched")),
    )

    with pytest.raises(ScoreError, match="outside the manifest boundary"):
        score_run(manifest)


def test_score_rejects_changed_adapter_schema_content(tmp_path: Path):
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
    schema = prepare_call(prompt, _case("hash"))["schema"]
    manifest = new_manifest(
        [_case("hash")],
        repeats=1,
        prompt_hash="prompt-hash",
        prompt_path=prompt,
        schema=schema,
        manifest_path=eval_root / "run.json",
        dataset="external",
        cycle_id="cycle-1",
    )
    manifest = record_slot_result(
        manifest,
        manifest.slots[0].key,
        SlotResult(kind="pass", parsed=schema(action="accept", reason="matched")),
    )
    persist_manifest(manifest, eval_root / "run.json")
    adapter.write_text(adapter.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")

    with pytest.raises(ScoreError, match="hash does not match"):
        score_run(load_manifest(eval_root / "run.json"))
