from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest
import yaml
from langchain_core.messages import AIMessage
from pydantic import BaseModel, Field

from scripts.run_prompt_eval import (
    SlotResult,
    _redacted,
    _safe_serialize,
    classify_exception,
    classify_response,
    execute_run,
    load_manifest,
    main,
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


def test_manifest_round_trip_restores_eval_case_for_adapter_after_process_boundary(
    tmp_path: Path,
) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("stable prompt\n", encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifest_path = tmp_path / ".runtime" / "run.json"
    manifest = new_manifest(
        cases=[_validated_case()],
        repeats=1,
        prompt_hash=prompt_hash,
        prompt_path=prompt,
        schema=Decision,
        manifest_path=manifest_path,
    )
    loaded = load_manifest(manifest_path)
    seen_cases: list[object] = []

    class Client:
        def with_structured_output(self, _schema: object, **_kwargs: object) -> "Client":
            return self

        def invoke(self, _messages: object) -> object:
            return {
                "raw": AIMessage(content="", tool_calls=[{"name": "Decision", "args": {}, "id": "call-1"}]),
                "parsed": Decision(action="accept", reason="matched"),
                "parsing_error": None,
            }

    def prepare_call(_prompt: Path, case_value: object) -> dict[str, object]:
        seen_cases.append(case_value)
        assert isinstance(case_value, EvalCase)
        return {"messages": [], "schema": Decision}

    result = execute_run(
        loaded,
        prompt_path=prompt,
        prepare_call=prepare_call,
        client=Client(),
        manifest_path=manifest_path,
    )

    assert result.results["a:0:" + prompt_hash].kind == "pass"
    assert len(seen_cases) == 1
    assert isinstance(seen_cases[0], EvalCase)


def test_runner_does_not_add_retry_layer_over_fixed_client() -> None:
    assert "max_retries" not in inspect.signature(execute_run).parameters


def test_runner_makes_one_client_invocation_for_transport_failure(tmp_path: Path) -> None:
    manifest = new_manifest(
        cases=[case("a")],
        repeats=1,
        prompt_hash="abc",
        manifest_path=tmp_path / "run.json",
    )
    invocations = 0

    class Client:
        def with_structured_output(self, *args: object, **kwargs: object) -> "Client":
            return self

        def invoke(self, _messages: object) -> object:
            nonlocal invocations
            invocations += 1
            raise StatusError(503)

    result = execute_run(
        manifest,
        prompt_path=tmp_path / "prompt.md",
        prepare_call=lambda _prompt, _case: {"messages": [], "schema": Decision},
        client=Client(),
    )

    assert invocations == 1
    assert result.results["a:0:abc"].attempts == 1
    assert result.results["a:0:abc"].status == "incomplete"


def test_adapter_import_error_is_persisted_as_paused_setup_error(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifest_path = tmp_path / ".runtime" / "run.json"
    manifest = new_manifest(
        cases=[_validated_case()],
        repeats=1,
        prompt_hash=prompt_hash,
        prompt_path=prompt,
        schema=Decision,
        manifest_path=manifest_path,
    )
    adapter = tmp_path / "adapter.py"
    adapter.write_text("raise RuntimeError('adapter import failed')\n", encoding="utf-8")

    result = execute_run(
        manifest,
        prompt_path=prompt,
        adapter_path=adapter,
        client=object(),
    )

    loaded = load_manifest(manifest_path)
    slot = loaded.results["a:0:" + prompt_hash]
    assert result.status == "paused"
    assert slot.kind == "setup_error"
    assert slot.status == "paused"
    assert "adapter import failed" in str(slot.detail)
    assert loaded.status == "paused"


def test_resume_rejects_prompt_path_mismatch_without_using_old_slots(tmp_path: Path) -> None:
    old_prompt = tmp_path / "old.md"
    new_prompt = tmp_path / "new.md"
    old_prompt.write_text("old\n", encoding="utf-8")
    new_prompt.write_text("new\n", encoding="utf-8")
    old_hash = hashlib.sha256(old_prompt.read_bytes()).hexdigest()
    manifest_path = tmp_path / "run.json"
    manifest = new_manifest(
        cases=[_validated_case()],
        repeats=1,
        prompt_hash=old_hash,
        prompt_path=old_prompt,
        schema=Decision,
        manifest_path=manifest_path,
    )
    invoked = False

    def prepare_call(_prompt: Path, _case: object) -> dict[str, object]:
        nonlocal invoked
        invoked = True
        return {"messages": [], "schema": Decision}

    with pytest.raises(ValueError, match="prompt path"):
        execute_run(
            load_manifest(manifest_path),
            prompt_path=new_prompt,
            prepare_call=prepare_call,
            client=object(),
            manifest_path=manifest_path,
        )

    assert invoked is False
    assert load_manifest(manifest_path).results == {}


def test_resume_rejects_prompt_content_hash_mismatch(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("original\n", encoding="utf-8")
    prompt_hash = hashlib.sha256(prompt.read_bytes()).hexdigest()
    manifest_path = tmp_path / "run.json"
    manifest = new_manifest(
        cases=[_validated_case()],
        repeats=1,
        prompt_hash=prompt_hash,
        prompt_path=prompt,
        schema=Decision,
        manifest_path=manifest_path,
    )
    prompt.write_text("changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="prompt hash"):
        execute_run(
            load_manifest(manifest_path),
            prompt_path=prompt,
            prepare_call=lambda _prompt, _case: {"messages": [], "schema": Decision},
            client=object(),
            manifest_path=manifest_path,
        )


def test_cli_uses_task3_validation_and_canonical_expected_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    adapter_source = (
        "from typing import Literal\n"
        "from pydantic import BaseModel, Field\n"
        "class ProductionDecision(BaseModel):\n"
        "    action: Literal['accept', 'reject'] = Field(alias='actionType')\n"
        "    reason: str\n"
        "def prepare_call(prompt_path, case):\n"
        "    return {'messages': [], 'schema': ProductionDecision}\n"
    )
    (eval_root / "adapter.py").write_text(adapter_source, encoding="utf-8")

    def write_cases(path: Path, case_id: str, split: str) -> None:
        import yaml

        path.write_text(
            yaml.safe_dump(
                [
                    {
                        "id": case_id,
                        "semantic_family": f"family-{split}",
                        "source": ["production.py"],
                        "input": {"variables": {"split": split}, "context": {}},
                        "expect": {"output": {"actionType": "accept", "reason": "matched"}},
                        "priority": "normal",
                        "dimensions": ["routing"],
                        "rationale": "production evidence determines the expected decision",
                    }
                ],
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    write_cases(eval_root / "dev-cases.yaml", "dev-1", "dev")
    write_cases(eval_root / "validation-cases.yaml", "validation-1", "validation")
    write_cases(eval_root / "acceptance-cases.yaml", "acceptance-1", "acceptance")

    class Client:
        schema: type[BaseModel] | None = None

        def with_structured_output(self, schema: type[BaseModel], **_kwargs: object) -> "Client":
            self.schema = schema
            return self

        def invoke(self, _messages: object) -> object:
            assert self.schema is not None
            parsed = self.schema(actionType="accept", reason="matched")
            return {
                "raw": AIMessage(content="", tool_calls=[{"name": "ProductionDecision", "args": {}, "id": "call-1"}]),
                "parsed": parsed,
                "parsing_error": None,
            }

    client = Client()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("scripts.run_prompt_eval.build_client", lambda _path=None: client)
    try:
        exit_code = main(
            [
                "--eval-root",
                str(eval_root),
                "--prompt",
                str(prompt),
                "--dataset",
                "dev",
                "--repeats",
                "1",
                "--manifest",
                str(eval_root / "run.json"),
            ]
        )
    finally:
        monkeypatch.undo()

    assert exit_code == 0
    assert json.loads((eval_root / "run.json").read_text(encoding="utf-8"))["results"]["dev-1:0:" + hashlib.sha256(prompt.read_bytes()).hexdigest()]["kind"] == "pass"
    assert "status" in capsys.readouterr().out


@pytest.mark.parametrize("dataset", ["dev", "validation"])
def test_cli_loads_selected_non_acceptance_split_without_acceptance_file(
    dataset: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    (eval_root / "adapter.py").write_text(
        "from pydantic import BaseModel\n"
        "class ProductionDecision(BaseModel):\n"
        "    action: str\n"
        "    reason: str\n"
        "def prepare_call(prompt_path, case):\n"
        "    return {'messages': [], 'schema': ProductionDecision}\n",
        encoding="utf-8",
    )
    case_id = f"{dataset}-only"
    (eval_root / f"{dataset}-cases.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": case_id,
                    "semantic_family": f"family-{dataset}",
                    "source": ["production.py"],
                    "input": {"variables": {"split": dataset}, "context": {}},
                    "expect": {"output": {"action": "accept", "reason": "matched"}},
                    "priority": "normal",
                    "dimensions": ["routing"],
                    "rationale": "production evidence determines the expected decision",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    class Client:
        schema: type[BaseModel] | None = None

        def with_structured_output(self, schema: type[BaseModel], **_kwargs: object) -> "Client":
            self.schema = schema
            return self

        def invoke(self, _messages: object) -> object:
            assert self.schema is not None
            return {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": self.schema(action="accept", reason="matched"),
                "parsing_error": None,
            }

    client = Client()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("scripts.run_prompt_eval.build_client", lambda _path=None: client)
    manifest_path = eval_root / "run.json"
    try:
        exit_code = main(
            [
                "--eval-root",
                str(eval_root),
                "--prompt",
                str(prompt),
                "--dataset",
                dataset,
                "--repeats",
                "1",
                "--manifest",
                str(manifest_path),
            ]
        )
    finally:
        monkeypatch.undo()

    assert exit_code == 0
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["dataset"] == dataset
    assert list(payload["case_data"]) == [case_id]
    assert payload["results"][f"{case_id}:0:{hashlib.sha256(prompt.read_bytes()).hexdigest()}"]["kind"] == "pass"
    assert "status" in capsys.readouterr().out


def test_cli_acceptance_loads_acceptance_split_without_unrelated_splits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    prompt = tmp_path / "prompt.md"
    prompt.write_text("prompt\n", encoding="utf-8")
    (eval_root / "adapter.py").write_text(
        "from pydantic import BaseModel\n"
        "class ProductionDecision(BaseModel):\n"
        "    action: str\n"
        "    reason: str\n"
        "def prepare_call(prompt_path, case):\n"
        "    return {'messages': [], 'schema': ProductionDecision}\n",
        encoding="utf-8",
    )
    case_id = "acceptance-only"
    (eval_root / "acceptance-cases.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "id": case_id,
                    "semantic_family": "family-acceptance",
                    "source": ["production.py"],
                    "input": {"variables": {"split": "acceptance"}, "context": {}},
                    "expect": {"output": {"action": "accept", "reason": "matched"}},
                    "priority": "normal",
                    "dimensions": ["routing"],
                    "rationale": "production evidence determines the expected decision",
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    class Client:
        schema: type[BaseModel] | None = None

        def with_structured_output(self, schema: type[BaseModel], **_kwargs: object) -> "Client":
            self.schema = schema
            return self

        def invoke(self, _messages: object) -> object:
            assert self.schema is not None
            return {
                "raw": AIMessage(content="", tool_calls=[]),
                "parsed": self.schema(action="accept", reason="matched"),
                "parsing_error": None,
            }

    client = Client()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("scripts.run_prompt_eval.build_client", lambda _path=None: client)
    manifest_path = eval_root / "run.json"
    try:
        exit_code = main(
            [
                "--eval-root",
                str(eval_root),
                "--prompt",
                str(prompt),
                "--dataset",
                "acceptance",
                "--repeats",
                "1",
                "--manifest",
                str(manifest_path),
            ]
        )
    finally:
        monkeypatch.undo()

    assert exit_code == 0
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["dataset"] == "acceptance"
    assert list(payload["case_data"]) == [case_id]
    assert "status" in capsys.readouterr().out


def test_api_response_validation_error_is_protocol_error() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("GET", "https://example.invalid/models"),
    )
    error = openai.APIResponseValidationError(response=response, body={"invalid": True})

    assert classify_exception(error).kind == "protocol_error"


@pytest.mark.parametrize("kind", ["setup_error", "protocol_error"])
def test_record_non_scoring_result_pauses_manifest(kind: str, tmp_path: Path) -> None:
    manifest = new_manifest(
        cases=[case("a")], repeats=1, prompt_hash="abc", manifest_path=tmp_path / f"{kind}.json"
    )

    result = record_slot_result(
        manifest,
        "a:0:abc",
        SlotResult(kind=kind, detail="failure"),
    )

    assert result.status == "paused"
    assert result.results["a:0:abc"].status == "paused"
    assert result.metrics is None
    assert load_manifest(tmp_path / f"{kind}.json").status == "paused"


def test_safe_serialize_uses_recursion_stack_and_redacts_shared_references() -> None:
    shared = {"authorization_token": "unit-test-secret", "value": "ok"}
    value = {"first": shared, "second": shared}

    serialized = _safe_serialize(value)
    redacted = _redacted(value)

    assert serialized["first"] == serialized["second"]
    assert serialized["first"] != "[RECURSIVE]"
    assert redacted["first"]["authorization_token"] == "[REDACTED]"
    assert redacted["second"]["authorization_token"] == "[REDACTED]"

    cycle: list[object] = []
    cycle.append(cycle)
    assert _safe_serialize(cycle) == ["[RECURSIVE]"]
