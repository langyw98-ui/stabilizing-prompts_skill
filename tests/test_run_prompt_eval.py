from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from scripts.run_prompt_eval import (
    SlotResult,
    classify_exception,
    classify_response,
    execute_run,
    new_manifest,
    pending_slots,
    record_slot_result,
)
from scripts.validate_cases import EvalCase, ValidatedCase


class Decision(BaseModel):
    action: str
    reason: str


def case(case_id: str) -> SimpleNamespace:
    return SimpleNamespace(id=case_id)


def passing_result() -> SlotResult:
    return SlotResult(
        kind="pass",
        parsed={"action": "accept", "reason": "matched"},
        raw={"content": "safe"},
    )


def test_slots_are_stable_and_resume_only_incomplete() -> None:
    manifest = new_manifest(cases=[case("a"), case("b")], repeats=2, prompt_hash="abc")

    assert [slot.key for slot in manifest.slots] == [
        "a:0:abc",
        "a:1:abc",
        "b:0:abc",
        "b:1:abc",
    ]

    manifest = record_slot_result(manifest, "a:0:abc", passing_result())

    assert [slot.key for slot in pending_slots(manifest)] == [
        "a:1:abc",
        "b:0:abc",
        "b:1:abc",
    ]


class StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"request failed with status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_status_becomes_transport_error(status: int) -> None:
    assert classify_exception(StatusError(status)).kind == "transport_error"


def test_non_retryable_sdk_exception_is_setup_error() -> None:
    assert classify_exception(StatusError(401)).kind == "setup_error"


def test_invalid_include_raw_shape_is_protocol_error() -> None:
    assert classify_response(None).kind == "protocol_error"
    assert classify_response({"raw": object()}).kind == "protocol_error"


def test_missing_function_call_is_parse_error_but_schema_validation_is_schema_error() -> None:
    missing = classify_response(
        {"raw": AIMessage(content="I cannot comply"), "parsed": None, "parsing_error": None}
    )
    # Construct a real Pydantic error through the production Schema.
    try:
        Decision.model_validate({"action": "accept"})
    except Exception as error:
        schema_error = classify_response(
            {"raw": AIMessage(content=""), "parsed": None, "parsing_error": error}
        )
    else:  # pragma: no cover - defensive assertion for the test fixture
        raise AssertionError("expected a validation error")

    assert missing.kind == "parse_error"
    assert schema_error.kind == "schema_error"


def test_manifest_persistence_is_atomic_and_never_serializes_live_models(tmp_path: Path) -> None:
    path = tmp_path / ".runtime" / "run.json"
    manifest = new_manifest(
        cases=[case("a")],
        repeats=1,
        prompt_hash="abc",
        schema=Decision,
        manifest_path=path,
    )
    result = SlotResult(
        kind="pass",
        parsed=Decision(action="accept", reason="matched"),
        raw=AIMessage(content="Authorization: Bearer unit-test-secret"),
    )

    manifest = record_slot_result(manifest, "a:0:abc", result)
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)

    assert isinstance(payload["results"]["a:0:abc"]["parsed"], dict)
    assert payload["schema_import"].endswith(":Decision")
    assert "AIMessage" not in text
    assert "unit-test-secret" not in text

    header_result = SlotResult(
        kind="business_error",
        detail={"headers": {"Authorization": "unit-test-secret"}},
    )
    header_manifest = new_manifest(
        cases=[case("a")], repeats=1, prompt_hash="abc", manifest_path=tmp_path / "header.json"
    )
    second = record_slot_result(header_manifest, "a:0:abc", header_result)
    assert "unit-test-secret" not in (tmp_path / "header.json").read_text(encoding="utf-8")
    assert second.results["a:0:abc"].status == "complete"


def test_execute_run_does_not_calculate_metrics_with_incomplete_slot(tmp_path: Path) -> None:
    manifest = new_manifest(
        cases=[case("a")],
        repeats=1,
        prompt_hash="abc",
        manifest_path=tmp_path / "run.json",
    )

    class Client:
        def with_structured_output(self, *args: object, **kwargs: object) -> "Client":
            return self

        def invoke(self, _messages: object) -> object:
            raise StatusError(503)

    result = execute_run(
        manifest,
        prompt_path=tmp_path / "prompt.md",
        prepare_call=lambda _prompt, _case: {"messages": [], "schema": Decision},
        client=Client(),
        manifest_path=tmp_path / "run.json",
    )

    assert result.results["a:0:abc"].status == "incomplete"
    assert result.metrics is None
    assert [slot.key for slot in pending_slots(result)] == ["a:0:abc"]


def _validated_case(case_id: str = "a") -> ValidatedCase:
    value = EvalCase(
        id=case_id,
        semantic_family="routing",
        source=["production.py"],
        input={"variables": {}, "context": {}},
        expect={"output": {"action": "accept", "reason": "matched"}},
        dimensions=["routing"],
        rationale="the evidence determines the expected decision",
    )
    return ValidatedCase(
        case=value,
        expected=Decision(action="accept", reason="matched"),
    )


def test_execute_run_resumes_only_the_incomplete_slot(tmp_path: Path) -> None:
    validated = _validated_case()
    manifest = new_manifest(
        cases=[validated],
        repeats=2,
        prompt_hash="abc",
        manifest_path=tmp_path / "run.json",
    )
    manifest = record_slot_result(manifest, "a:0:abc", passing_result())
    calls: list[object] = []

    class Client:
        def with_structured_output(self, schema: object, **kwargs: object) -> "Client":
            assert schema is Decision
            assert kwargs == {"method": "function_calling", "include_raw": True}
            return self

        def invoke(self, _messages: object) -> object:
            calls.append(True)
            return {
                "raw": AIMessage(content="", tool_calls=[{"name": "Decision", "args": {}, "id": "call-1"}]),
                "parsed": Decision(action="accept", reason="matched"),
                "parsing_error": None,
            }

    result = execute_run(
        manifest,
        prompt_path=tmp_path / "prompt.md",
        prepare_call=lambda _prompt, _case: {"messages": [], "schema": Decision},
        client=Client(),
        manifest_path=tmp_path / "run.json",
    )

    assert len(calls) == 1
    assert result.results["a:0:abc"].attempts == 1
    assert result.results["a:1:abc"].kind == "pass", result.results["a:1:abc"]
    assert result.metrics is not None
