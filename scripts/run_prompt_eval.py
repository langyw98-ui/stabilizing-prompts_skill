"""Run fixed prompt-evaluation slots with resumable, redacted persistence.

The runner deliberately keeps the model client and project adapter at a narrow
boundary.  Slot identity is derived only from the case id, repeat index, and
prompt hash; a resumed run can therefore fill an incomplete slot without
silently replacing it with a newly allocated call.  Runtime Pydantic and
LangChain objects never become manifest values: they are reduced to ordinary
JSON-compatible dictionaries before the atomic write.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import MappingProxyType
from typing import Any

import yaml
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

try:  # The module is also useful when executed as a script from the Skill root.
    from scripts.local_model_client import (
        build_client,
        redact_secret,
        safe_client_config,
        safe_error,
    )
    from scripts.validate_cases import (
        CaseSetupError,
        EvalCase,
        ValidatedCase,
        load_case_split,
    )
except ModuleNotFoundError:  # pragma: no cover - direct-script compatibility
    from local_model_client import build_client, redact_secret, safe_client_config, safe_error
    from validate_cases import CaseSetupError, EvalCase, ValidatedCase, load_case_split


NON_SCORING_KINDS = frozenset({"setup_error", "transport_error", "protocol_error"})
SCORING_KINDS = frozenset({"parse_error", "schema_error", "business_error", "pass"})
REQUIRED_INCLUDE_RAW_KEYS = frozenset({"raw", "parsed", "parsing_error"})
DEFAULT_PHASE_REPEATS = {"dev": 5, "validation": 5, "acceptance": 10}
ADAPTER_SCHEMA_REFERENCE_PREFIX = "adapter-file-v1"
_SENSITIVE_OUTPUT_KEY = {
    "authorization",
    "authorization_token",
    "api_key",
    "apikey",
    "access_token",
    "password",
    "secret",
    "token",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PromptIdentityError(ValueError):
    """Raised when a resume target does not match its manifest identity."""


class UsageError(ValueError):
    """Raised when a runner mode and dataset combination is forbidden."""


def _canonical_prompt_path(path: Path | str) -> Path:
    return Path(path).resolve(strict=False)


def _prompt_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        raise PromptIdentityError("unable to read prompt for hash validation") from error


def _validate_prompt_identity(manifest: "RunManifest", prompt_path: Path) -> None:
    """Require the resume path and bytes to match the planned prompt exactly."""

    actual_path = _canonical_prompt_path(prompt_path)
    if manifest.prompt_path is not None:
        expected_path = _canonical_prompt_path(manifest.prompt_path)
        if actual_path != expected_path:
            raise PromptIdentityError(
                f"prompt path does not match manifest: {actual_path} != {expected_path}"
            )

    # A prompt path is always validated when recorded.  For legacy manifests
    # without a path, validate the hash when the supplied file exists; this
    # keeps synthetic/unit manifests usable while still preventing reuse of a
    # stale slot plan in a real run.
    if manifest.prompt_path is not None or actual_path.is_file():
        actual_hash = _prompt_hash(actual_path)
        if manifest.prompt_hash and actual_hash != manifest.prompt_hash:
            raise PromptIdentityError(
                f"prompt hash does not match manifest: {actual_hash} != {manifest.prompt_hash}"
            )


def _schema_import_reference(
    schema: object | None, adapter_path: Path | str | None = None
) -> str | None:
    """Return a durable reference for a production Schema.

    Adapters are loaded from a file under a generated module name.  That name
    is useful while the adapter is executing but cannot be imported after a
    process restart.  When the class came from such an adapter, persist the
    canonical adapter path, its content hash, and the class qualname instead.
    Ordinary importable production classes retain the historical
    ``module:qualname`` spelling.
    """
    if not isinstance(schema, type):
        return None
    module = getattr(schema, "__module__", None)
    qualname = getattr(schema, "__qualname__", None)
    if not isinstance(module, str) or not isinstance(qualname, str):
        return None
    adapter_file: Path | None = None
    module_object = sys.modules.get(module)
    module_file = getattr(module_object, "__file__", None)
    if isinstance(module_file, str):
        module_file_path = Path(module_file).resolve(strict=False)
        if adapter_path is not None:
            requested = Path(adapter_path).resolve(strict=False)
            if module_file_path == requested:
                adapter_file = requested
        elif module_file_path.name == "adapter.py":
            adapter_file = module_file_path
    if adapter_file is not None:
        try:
            canonical = adapter_file.resolve(strict=True)
            digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
        except (OSError, RuntimeError) as error:
            raise ValueError("unable to hash adapter for Schema reference") from error
        if "|" in str(canonical) or "|" in qualname or "<locals>" in qualname:
            raise ValueError("adapter Schema reference contains an invalid delimiter")
        return f"{ADAPTER_SCHEMA_REFERENCE_PREFIX}|{canonical}|{digest}|{qualname}"
    return f"{module}:{qualname}"


def _canonical_model_dump(value: BaseModel) -> Mapping[str, object]:
    """Serialize only production Schema fields using canonical field names."""

    try:
        dumped = value.model_dump(mode="json", by_alias=False)
    except Exception:
        try:
            dumped = value.model_dump(mode="python", by_alias=False)
        except Exception:
            return {}
    if not isinstance(dumped, Mapping):
        return {}
    fields = getattr(type(value), "model_fields", {})
    return {
        name: dumped[name]
        for name in fields
        if isinstance(name, str) and name in dumped
    }


def _safe_serialize(value: object, _seen: set[int] | None = None) -> object:
    """Convert runtime values to JSON-compatible values before redaction.

    ``_seen`` is an active recursion stack, rather than a process-wide visited
    set.  A shared value is therefore serialized at each reference while a
    value encountered through its own children is still recognized as a cycle.
    """

    seen = _seen if _seen is not None else set()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)

    value_id = id(value)
    if value_id in seen:
        return "[RECURSIVE]"

    if isinstance(value, BaseModel):
        seen.add(value_id)
        try:
            dumped = _canonical_model_dump(value)
        except Exception:
            dumped = safe_error(value)
        try:
            return _safe_serialize(dumped, seen)
        finally:
            seen.remove(value_id)

    if isinstance(value, Mapping):
        seen.add(value_id)
        try:
            result: dict[str, object] = {}
            for key, item in value.items():
                safe_key = key if isinstance(key, str) else _safe_serialize(key, seen)
                if not isinstance(safe_key, str):
                    safe_key = str(safe_key)
                result[safe_key] = _safe_serialize(item, seen)
            return result
        finally:
            seen.remove(value_id)

    if isinstance(value, (list, tuple)):
        seen.add(value_id)
        try:
            return [_safe_serialize(item, seen) for item in value]
        finally:
            seen.remove(value_id)

    if isinstance(value, (set, frozenset)):
        seen.add(value_id)
        try:
            values = [_safe_serialize(item, seen) for item in value]
            return sorted(values, key=lambda item: repr(item))
        finally:
            seen.remove(value_id)

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return safe_error(value)
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            return str(value.isoformat())
        except Exception:
            pass

    # Unknown SDK values must not be allowed to become an arbitrary object in
    # a YAML/JSON document.  ``safe_error`` applies the same recursive secret
    # redaction policy to their string form.
    try:
        return safe_error(value)
    except Exception:
        return "[UNSERIALIZABLE]"


def _redacted(value: object) -> object:
    try:
        serialized = _safe_serialize(value)
        return redact_secret(_redact_sensitive_keys(serialized))
    except Exception:
        return "[REDACTION_FAILED]"


def _redact_sensitive_keys(value: object) -> object:
    """Cover plain header/key mappings in addition to value-pattern redaction."""

    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _SENSITIVE_OUTPUT_KEY:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_sensitive_keys(item)
        return result
    if isinstance(value, list):
        return [_redact_sensitive_keys(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CallSlot:
    """Immutable identity for one case/repeat/prompt invocation."""

    case_id: str
    repeat_index: int
    prompt_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.case_id, str) or not self.case_id:
            raise ValueError("slot case_id must be a non-empty string")
        if not isinstance(self.repeat_index, int) or self.repeat_index < 0:
            raise ValueError("slot repeat_index must be a non-negative integer")
        if not isinstance(self.prompt_hash, str) or not self.prompt_hash:
            raise ValueError("slot prompt_hash must be a non-empty string")

    @property
    def key(self) -> str:
        return f"{self.case_id}:{self.repeat_index}:{self.prompt_hash}"

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "repeat_index": self.repeat_index,
            "prompt_hash": self.prompt_hash,
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CallSlot":
        slot = cls(
            case_id=str(value["case_id"]),
            repeat_index=int(value["repeat_index"]),
            prompt_hash=str(value["prompt_hash"]),
        )
        recorded_key = value.get("key")
        if recorded_key is not None and recorded_key != slot.key:
            raise ValueError("manifest slot key does not match its stable identity")
        return slot


@dataclass(frozen=True, slots=True)
class ClassifiedResult:
    """A response/exception classification, retaining only safe evidence."""

    kind: str
    detail: object | None = None
    raw: AIMessage | None = None
    parsed: object | None = None
    parsing_error: object | None = None

    def __post_init__(self) -> None:
        # ``ClassifiedResult("parsed", model)`` is a useful compact form for
        # adapters/tests; normalize it so ``parsed`` is still the canonical
        # field while protocol/error classifications retain their detail.
        if self.kind == "parsed" and self.parsed is None and self.detail is not None:
            object.__setattr__(self, "parsed", self.detail)
            object.__setattr__(self, "detail", None)

    @property
    def value(self) -> object | None:
        """Compatibility alias for the second constructor value."""

        return self.parsed if self.parsed is not None else self.detail

    @property
    def reason(self) -> object | None:
        return self.detail


@dataclass(frozen=True, slots=True, init=False)
class SlotResult:
    """One persisted slot result.

    ``parsed`` and ``raw`` may be live objects briefly while a call is being
    handled, but ``record_slot_result`` creates a manifest copy containing
    only dictionaries/scalars.  The permissive aliases in ``__init__`` keep
    the small public API convenient for callers using ``classification`` or
    ``state`` terminology.
    """

    kind: str
    detail: object | None
    parsed: object | None
    raw: object | None
    parsing_error: object | None
    attempts: int
    status: str
    slot_key: str | None
    started_at: str | None
    completed_at: str | None

    def __init__(
        self,
        kind: str = "pending",
        detail: object | None = None,
        parsed: object | None = None,
        raw: object | None = None,
        parsing_error: object | None = None,
        attempts: int = 1,
        status: str | None = None,
        slot_key: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        **aliases: object,
    ) -> None:
        if "classification" in aliases:
            kind = str(aliases.pop("classification"))
        if "error_kind" in aliases:
            kind = str(aliases.pop("error_kind"))
        if "state" in aliases:
            status = str(aliases.pop("state"))
        if "key" in aliases and slot_key is None:
            slot_key = str(aliases.pop("key"))
        if aliases:
            unknown = ", ".join(sorted(str(key) for key in aliases))
            raise TypeError(f"unknown SlotResult argument(s): {unknown}")
        if status is None:
            status = (
                "incomplete"
                if kind == "transport_error"
                else "paused"
                if kind in {"setup_error", "protocol_error"}
                else "complete"
            )
        elif kind == "transport_error":
            # Transport exhaustion is the one retryable state that must remain
            # resumable regardless of what a caller supplied as ``status``.
            status = "incomplete"
        elif kind in {"setup_error", "protocol_error"}:
            # Non-scoring setup/protocol evidence must pause the run.  A
            # complete slot with ``metrics=None`` would look resumable only in
            # metadata while hiding the reason for the stop.
            status = "paused"
        if attempts < 1:
            raise ValueError("slot attempts must be positive")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "detail", detail)
        object.__setattr__(self, "parsed", parsed)
        object.__setattr__(self, "raw", raw)
        object.__setattr__(self, "parsing_error", parsing_error)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "slot_key", slot_key)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "completed_at", completed_at)

    @property
    def classification(self) -> str:
        return self.kind

    @property
    def state(self) -> str:
        return self.status

    @property
    def is_complete(self) -> bool:
        return self.status == "complete"

    @property
    def is_incomplete(self) -> bool:
        return not self.is_complete

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "detail": _redacted(self.detail),
            "parsed": _redacted(self.parsed),
            "raw": _redacted(self.raw),
            "parsing_error": _redacted(self.parsing_error),
            "attempts": self.attempts,
            "status": self.status,
            "slot_key": self.slot_key,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SlotResult":
        return cls(
            kind=str(value.get("kind", value.get("classification", "pending"))),
            detail=value.get("detail"),
            parsed=value.get("parsed"),
            raw=value.get("raw"),
            parsing_error=value.get("parsing_error"),
            attempts=int(value.get("attempts", 1)),
            status=str(value.get("status", value.get("state", "complete"))),
            slot_key=value.get("slot_key") if isinstance(value.get("slot_key"), str) else None,
            started_at=value.get("started_at") if isinstance(value.get("started_at"), str) else None,
            completed_at=value.get("completed_at") if isinstance(value.get("completed_at"), str) else None,
        )


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Immutable slot plan plus serializable results and run metadata."""

    slots: tuple[CallSlot, ...] = ()
    prompt_hash: str = ""
    repeats: int = 0
    results: Mapping[str, SlotResult] = field(default_factory=dict)
    prompt_path: str | None = None
    schema_import: str | None = None
    case_data: Mapping[str, object] = field(default_factory=dict)
    dataset: str | None = None
    mode: str = "tune"
    cycle_id: str | None = None
    manifest_path: Path | None = field(default=None, repr=False, compare=False)
    started_at: str | None = None
    resumed_at: str | None = None
    completed_at: str | None = None
    status: str = "planned"
    stop_reason: str | None = None
    metrics: Mapping[str, object] | None = None
    client_config: Mapping[str, object] = field(default_factory=dict)
    runtime_cases: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", tuple(self.slots))
        object.__setattr__(self, "results", MappingProxyType(dict(self.results)))
        object.__setattr__(self, "case_data", MappingProxyType(dict(self.case_data)))
        object.__setattr__(self, "client_config", MappingProxyType(dict(self.client_config)))
        object.__setattr__(self, "runtime_cases", MappingProxyType(dict(self.runtime_cases)))
        if self.mode not in {"tune", "verify"}:
            raise ValueError("manifest mode must be tune or verify")
        if self.mode == "verify" and self.dataset == "acceptance":
            raise UsageError("verify mode cannot use the acceptance dataset")
        if self.metrics is not None:
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        slot_keys = {slot.key for slot in self.slots}
        if any(slot.prompt_hash != self.prompt_hash for slot in self.slots):
            raise ValueError("manifest slot prompt hash does not match manifest prompt hash")
        if any(key not in slot_keys for key in self.results):
            raise ValueError("manifest contains a result for an unknown slot")

    @property
    def pending(self) -> tuple[CallSlot, ...]:
        return pending_slots(self)

    @property
    def complete(self) -> bool:
        return not self.pending

    @property
    def path(self) -> Path | None:
        return self.manifest_path

    @property
    def schema_ref(self) -> str | None:
        """Compatibility spelling for the persisted production Schema ref."""

        return self.schema_import

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "prompt_hash": self.prompt_hash,
            "prompt_path": self.prompt_path,
            "repeats": self.repeats,
            "dataset": self.dataset,
            "mode": self.mode,
            "cycle_id": self.cycle_id,
            "schema_import": self.schema_import,
            "slots": [slot.to_dict() for slot in self.slots],
            "case_data": _redacted(dict(self.case_data)),
            "results": {
                key: result.to_dict() for key, result in self.results.items()
            },
            "started_at": self.started_at,
            "resumed_at": self.resumed_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "metrics": _redacted(self.metrics),
            "client_config": _redacted(dict(self.client_config)),
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object], *, manifest_path: Path | None = None
    ) -> "RunManifest":
        slots_raw = value.get("slots", ())
        if not isinstance(slots_raw, Sequence) or isinstance(slots_raw, (str, bytes)):
            raise ValueError("manifest slots must be a list")
        if any(not isinstance(item, Mapping) for item in slots_raw):
            raise ValueError("manifest slots must contain objects")
        slots = tuple(CallSlot.from_dict(item) for item in slots_raw)
        results_raw = value.get("results", {})
        if not isinstance(results_raw, Mapping):
            raise ValueError("manifest results must be a mapping")
        if any(not isinstance(item, Mapping) for item in results_raw.values()):
            raise ValueError("manifest results must contain objects")
        results = {str(key): SlotResult.from_dict(item) for key, item in results_raw.items()}
        for key, result in results.items():
            if result.slot_key is not None and result.slot_key != key:
                raise ValueError("manifest result slot key does not match its result key")
        case_data = value.get("case_data", {})
        if not isinstance(case_data, Mapping):
            case_data = {}
        client_config = value.get("client_config", {})
        if not isinstance(client_config, Mapping):
            client_config = {}
        metrics = value.get("metrics")
        if not isinstance(metrics, Mapping):
            metrics = None
        return cls(
            slots=slots,
            prompt_hash=str(value.get("prompt_hash", "")),
            repeats=int(value.get("repeats", 0)),
            results=results,
            prompt_path=value.get("prompt_path") if isinstance(value.get("prompt_path"), str) else None,
            schema_import=value.get("schema_import") if isinstance(value.get("schema_import"), str) else None,
            case_data=case_data,
            dataset=value.get("dataset") if isinstance(value.get("dataset"), str) else None,
            mode=value.get("mode", "tune") if isinstance(value.get("mode", "tune"), str) else "tune",
            cycle_id=value.get("cycle_id") if isinstance(value.get("cycle_id"), str) else None,
            manifest_path=manifest_path,
            started_at=value.get("started_at") if isinstance(value.get("started_at"), str) else None,
            resumed_at=value.get("resumed_at") if isinstance(value.get("resumed_at"), str) else None,
            completed_at=value.get("completed_at") if isinstance(value.get("completed_at"), str) else None,
            status=str(value.get("status", "planned")),
            stop_reason=value.get("stop_reason") if isinstance(value.get("stop_reason"), str) else None,
            metrics=metrics,
            client_config=client_config,
        )


def _case_id(case: object) -> str:
    if isinstance(case, ValidatedCase):
        return case.case.id
    if isinstance(case, Mapping) and isinstance(case.get("id"), str):
        return case["id"]
    candidate = getattr(case, "id", None)
    if isinstance(candidate, str):
        return candidate
    raise ValueError("each evaluation case must expose a string id")


def _case_payload(
    case: object, *, schema: type[BaseModel] | None = None
) -> tuple[dict[str, object], object | None, type[BaseModel] | None]:
    expected: object | None = None
    case_schema: type[BaseModel] | None = None
    source = case.case if isinstance(case, ValidatedCase) else case
    if isinstance(case, ValidatedCase):
        expected = case.expected
        if isinstance(case.expected, BaseModel):
            case_schema = type(case.expected)
    if isinstance(source, BaseModel):
        payload = _safe_serialize(source)
    elif isinstance(source, Mapping):
        payload = _safe_serialize(dict(source))
    else:
        try:
            payload = _safe_serialize(vars(source))
        except TypeError:
            payload = {"id": _case_id(case)}
    if not isinstance(payload, dict):
        payload = {"id": _case_id(case)}
    if expected is not None:
        payload["expected"] = _redacted(expected)
    elif isinstance(payload.get("expect"), Mapping):
        output = payload["expect"].get("output")
        if isinstance(output, Mapping):
            if schema is not None:
                try:
                    normalized = schema.model_validate(output, extra="forbid")
                except Exception:
                    normalized = output
                payload["expected"] = _redacted(normalized)
            else:
                payload["expected"] = _redacted(output)
    return payload, expected, case_schema


def new_manifest(
    cases: Sequence[object],
    repeats: int,
    prompt_hash: str,
    *,
    prompt_path: Path | str | None = None,
    schema: type[BaseModel] | None = None,
    schema_import: str | None = None,
    manifest_path: Path | None = None,
    dataset: str | None = None,
    mode: str = "tune",
    cycle_id: str | None = None,
    client_config: Mapping[str, object] | None = None,
) -> RunManifest:
    if repeats < 1:
        raise ValueError("repeats must be positive")
    ids: list[str] = []
    case_data: dict[str, object] = {}
    runtime_cases: dict[str, object] = {}
    inferred_schema = schema
    for case in cases:
        case_id = _case_id(case)
        if case_id in ids:
            raise ValueError(f"duplicate case id: {case_id}")
        ids.append(case_id)
        payload, _expected, case_schema = _case_payload(case, schema=inferred_schema)
        case_data[case_id] = payload
        # Keep the caller's validated object only as an in-process convenience;
        # ``to_dict`` intentionally persists ``case_data`` instead.  A fresh
        # process therefore always reconstructs an EvalCase from that data.
        runtime_cases[case_id] = case.case if isinstance(case, ValidatedCase) else case
        inferred_schema = inferred_schema or case_schema
    slots = tuple(
        CallSlot(case_id=case_id, repeat_index=repeat, prompt_hash=prompt_hash)
        for case_id in ids
        for repeat in range(repeats)
    )
    manifest = RunManifest(
        slots=slots,
        prompt_hash=prompt_hash,
        repeats=repeats,
        prompt_path=(
            str(_canonical_prompt_path(prompt_path))
            if prompt_path is not None
            else None
        ),
        schema_import=schema_import or _schema_import_reference(inferred_schema),
        case_data=case_data,
        dataset=dataset,
        mode=mode,
        cycle_id=cycle_id,
        manifest_path=manifest_path,
        status="planned",
        client_config=client_config or {},
        runtime_cases=runtime_cases,
    )
    if manifest_path is not None:
        persist_manifest(manifest, manifest_path)
    return manifest


def pending_slots(manifest: RunManifest) -> tuple[CallSlot, ...]:
    return tuple(
        slot
        for slot in manifest.slots
        if slot.key not in manifest.results or manifest.results[slot.key].is_incomplete
    )


def _metrics(manifest: RunManifest) -> dict[str, object] | None:
    if pending_slots(manifest):
        return None
    results = [manifest.results[slot.key] for slot in manifest.slots]
    if any(result.kind not in SCORING_KINDS for result in results):
        return None
    counts = {kind: sum(result.kind == kind for result in results) for kind in SCORING_KINDS}
    scored = sum(counts[kind] for kind in SCORING_KINDS)
    case_ids = [slot.case_id for slot in manifest.slots]
    stable = 0
    for case_id in dict.fromkeys(case_ids):
        repeats = [
            manifest.results[slot.key]
            for slot in manifest.slots
            if slot.case_id == case_id
        ]
        if repeats and all(result.kind == "pass" for result in repeats):
            stable += 1
    case_count = len(dict.fromkeys(case_ids))
    return {
        "scored_responses": scored,
        "pass": counts["pass"],
        "parse_error": counts["parse_error"],
        "schema_error": counts["schema_error"],
        "business_error": counts["business_error"],
        "schema_valid_rate": (counts["business_error"] + counts["pass"]) / scored
        if scored
        else None,
        "run_accuracy": counts["pass"] / scored if scored else None,
        "stable_case_rate": stable / case_count if case_count else None,
    }


def record_slot_result(
    manifest: RunManifest, slot_key: str, result: SlotResult | ClassifiedResult
) -> RunManifest:
    """Return a manifest with one fixed slot atomically updated.

    Completed slots are immutable.  An incomplete slot may be replaced by its
    resumed result, but a caller cannot accidentally append a new slot or
    overwrite a completed response.
    """

    slot = next((candidate for candidate in manifest.slots if candidate.key == slot_key), None)
    if slot is None:
        raise KeyError(f"unknown slot key: {slot_key}")
    existing = manifest.results.get(slot_key)
    if existing is not None and existing.is_complete:
        raise ValueError(f"slot already complete: {slot_key}")
    if isinstance(result, ClassifiedResult):
        result = SlotResult(
            kind=result.kind,
            detail=result.detail,
            raw=result.raw,
            parsed=result.parsed,
            parsing_error=result.parsing_error,
            slot_key=slot_key,
        )
    # Recreate with redacted, JSON-safe values.  The original result remains a
    # caller-owned runtime value and is never inserted into the manifest file.
    safe_result = SlotResult(
        kind=result.kind,
        detail=_redacted(result.detail),
        parsed=_redacted(result.parsed),
        raw=_redacted(result.raw),
        parsing_error=_redacted(result.parsing_error),
        attempts=result.attempts,
        status=result.status,
        slot_key=slot_key,
        started_at=result.started_at,
        completed_at=result.completed_at or _now(),
    )
    results = dict(manifest.results)
    results[slot_key] = safe_result
    result_status = (
        "incomplete"
        if safe_result.kind == "transport_error"
        else "paused"
        if safe_result.kind in {"setup_error", "protocol_error"}
        else "complete"
        if not pending_slots(replace(manifest, results=results))
        else "running"
    )
    updated = replace(
        manifest,
        results=results,
        resumed_at=_now() if existing is not None and existing.is_incomplete else manifest.resumed_at,
        completed_at=_now() if not pending_slots(replace(manifest, results=results)) else None,
        status=result_status,
        metrics=None,
        stop_reason=(
            safe_result.kind
            if safe_result.kind in NON_SCORING_KINDS
            else None
        ),
    )
    computed = _metrics(updated)
    if computed is not None:
        updated = replace(updated, metrics=computed, status="complete", completed_at=updated.completed_at or _now())
    if updated.manifest_path is not None:
        persist_manifest(updated, updated.manifest_path)
    return updated


def _status_code(error: BaseException) -> int | None:
    candidates: list[object] = [
        getattr(error, "status_code", None),
        getattr(error, "http_status", None),
        getattr(getattr(error, "response", None), "status_code", None),
        getattr(getattr(error, "response", None), "status", None),
    ]
    for candidate in candidates:
        if isinstance(candidate, int):
            return candidate
        if isinstance(candidate, str) and candidate.isdigit():
            return int(candidate)
    return None


def _transport_exception(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    names = {cls.__name__.casefold() for cls in type(error).__mro__}
    return any(
        any(token in name for token in ("timeout", "connection", "connecterror", "transport"))
        for name in names
    )


def _protocol_exception(error: BaseException) -> bool:
    """Recognize response-envelope/protocol failures from SDK type names."""

    names = {cls.__name__.casefold() for cls in type(error).__mro__}
    return any(
        "protocol" in name
        or "envelope" in name
        or ("response" in name and "validation" in name)
        for name in names
    )


def classify_exception(error: BaseException) -> ClassifiedResult:
    """Classify SDK failures without using exception-message heuristics."""

    if _protocol_exception(error):
        return ClassifiedResult("protocol_error", safe_error(error))
    status = _status_code(error)
    if status in (408, 429) or (status is not None and 500 <= status <= 599):
        return ClassifiedResult("transport_error", f"http_status_{status}")
    if status is None and _transport_exception(error):
        return ClassifiedResult("transport_error", type(error).__name__)
    return ClassifiedResult("setup_error", safe_error(error))


def _has_tool_payload(raw: AIMessage) -> bool:
    calls = getattr(raw, "tool_calls", None)
    if isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)) and bool(calls):
        return True
    additional = getattr(raw, "additional_kwargs", None)
    if isinstance(additional, Mapping):
        calls = additional.get("tool_calls")
        return isinstance(calls, Sequence) and not isinstance(calls, (str, bytes)) and bool(calls)
    return False


def _validation_error_is_json_invalid(error: BaseException) -> bool:
    if not isinstance(error, ValidationError):
        return False
    try:
        errors = error.errors()
    except Exception:
        return False
    return any(str(item.get("type", "")).startswith("json_invalid") for item in errors)


def classify_parsing_failure(
    raw: AIMessage, parsing_error: object | None
) -> ClassifiedResult:
    if parsing_error is not None and _validation_error_is_json_invalid(parsing_error):
        kind, reason = "parse_error", "invalid_payload"
    elif isinstance(parsing_error, (json.JSONDecodeError, UnicodeDecodeError)):
        kind, reason = "parse_error", "invalid_payload"
    elif parsing_error is not None and any(
        token in type(parsing_error).__name__.casefold()
        for token in ("parse", "decode", "outputparser")
    ):
        kind, reason = "parse_error", "invalid_payload"
    elif isinstance(parsing_error, ValidationError):
        kind, reason = "schema_error", "schema_validation"
    elif parsing_error is not None:
        kind, reason = "schema_error", "schema_validation"
    elif not _has_tool_payload(raw):
        kind, reason = "parse_error", "missing_payload"
    else:
        kind, reason = "parse_error", "invalid_payload"
    return ClassifiedResult(
        kind,
        reason,
        raw=raw,
        parsing_error=parsing_error,
    )


def classify_response(value: object) -> ClassifiedResult:
    """Classify an ``include_raw=True`` response contract."""

    if not isinstance(value, Mapping) or not REQUIRED_INCLUDE_RAW_KEYS.issubset(value):
        return ClassifiedResult("protocol_error", "invalid_include_raw_contract")
    raw = value.get("raw")
    if not isinstance(raw, AIMessage):
        return ClassifiedResult("protocol_error", "missing_raw_message")
    parsed = value.get("parsed")
    parsing_error = value.get("parsing_error")
    if parsed is not None:
        return ClassifiedResult(
            "parsed",
            raw=raw,
            parsed=parsed,
            parsing_error=parsing_error,
        )
    return classify_parsing_failure(raw, parsing_error)


def _model_data(value: object) -> object:
    if isinstance(value, BaseModel):
        return _safe_serialize(value)
    return _safe_serialize(value)


def _expected_data(
    manifest: RunManifest,
    case_id: str,
    schema: type[BaseModel] | None = None,
) -> object | None:
    payload = manifest.case_data.get(case_id)
    expected: object | None = None
    if isinstance(payload, Mapping):
        if "expected" in payload:
            expected = payload["expected"]
        else:
            expect = payload.get("expect")
            if isinstance(expect, Mapping) and "output" in expect:
                expected = expect["output"]
    if schema is not None and isinstance(expected, Mapping):
        try:
            expected = schema.model_validate(
                expected,
                extra="forbid",
                by_alias=True,
                by_name=True,
            )
        except ValidationError:
            return expected
    return _model_data(expected) if isinstance(expected, BaseModel) else expected


def _restore_eval_case(case_id: str, payload: object) -> EvalCase:
    if not isinstance(payload, Mapping):
        raise CaseSetupError(f"manifest case {case_id!r} is not an object")
    candidate = dict(payload)
    # ``expected`` is the canonical production-object snapshot used by the
    # scorer; it is not part of Task 3's EvalCase input model.
    candidate.pop("expected", None)
    try:
        return EvalCase.model_validate(candidate)
    except Exception as error:
        raise CaseSetupError(
            f"manifest case {case_id!r} cannot be restored as EvalCase: {error}"
        ) from error


def _case_for(manifest: RunManifest, case_id: str, cases: Mapping[str, object] | None) -> object:
    if cases is not None and case_id in cases:
        return cases[case_id]
    if case_id in manifest.runtime_cases:
        runtime_payload = manifest.runtime_cases[case_id]
        if isinstance(runtime_payload, EvalCase):
            return runtime_payload
        if isinstance(runtime_payload, ValidatedCase):
            return runtime_payload.case
        if isinstance(runtime_payload, Mapping):
            return _restore_eval_case(case_id, runtime_payload)
        # Direct callers may use a lightweight case double in tests; this
        # value is never persisted and is not a fallback for a loaded manifest.
        return runtime_payload
    payload = manifest.case_data.get(case_id)
    if isinstance(payload, Mapping):
        return _restore_eval_case(case_id, payload)
    raise CaseSetupError(f"manifest does not contain case {case_id!r}")


def _slot_result_from_response(
    manifest: RunManifest,
    slot: CallSlot,
    classified: ClassifiedResult,
    schema: type[BaseModel] | None,
    attempts: int,
) -> SlotResult:
    if classified.kind != "parsed":
        return SlotResult(
            kind=classified.kind,
            detail=classified.detail,
            raw=classified.raw,
            parsed=classified.parsed,
            parsing_error=classified.parsing_error,
            attempts=attempts,
            status="complete" if classified.kind != "transport_error" else "incomplete",
            slot_key=slot.key,
        )
    parsed = classified.parsed
    if schema is not None:
        if not isinstance(parsed, schema):
            if isinstance(parsed, Mapping):
                try:
                    parsed = schema.model_validate(parsed)
                except ValidationError as error:
                    kind = "parse_error" if _validation_error_is_json_invalid(error) else "schema_error"
                    return SlotResult(
                        kind=kind,
                        detail="invalid_payload" if kind == "parse_error" else "schema_validation",
                        raw=classified.raw,
                        parsed=parsed,
                        parsing_error=error,
                        attempts=attempts,
                        slot_key=slot.key,
                    )
            else:
                return SlotResult(
                    kind="schema_error",
                    detail="unexpected_schema_type",
                    raw=classified.raw,
                    parsed=parsed,
                    attempts=attempts,
                    slot_key=slot.key,
                )
    actual_data = _model_data(parsed)
    expected_data = _expected_data(manifest, slot.case_id, schema)
    kind = "pass" if expected_data is not None and actual_data == expected_data else "business_error"
    return SlotResult(
        kind=kind,
        detail=None if kind == "pass" else "object_mismatch",
        raw=classified.raw,
        parsed=actual_data,
        attempts=attempts,
        slot_key=slot.key,
    )


def load_adapter(eval_root: Path) -> Callable[[Path, object], Mapping[str, object]]:
    adapter_path = eval_root / "adapter.py"
    if not adapter_path.is_file():
        raise RuntimeError("adapter.py is missing")
    # Python's built-in hash is randomized per process.  A deterministic module
    # name keeps the persisted ``schema_import`` compatible across resumes.
    adapter_identity = hashlib.sha256(
        str(adapter_path.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    name = f"prompt_eval_adapter_{adapter_identity}"
    spec = importlib.util.spec_from_file_location(name, adapter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load adapter.py")
    module = importlib.util.module_from_spec(spec)
    # Keep the generated module available only as an in-process execution
    # detail.  Manifests never rely on this name: ``_schema_import_reference``
    # records a durable adapter-file reference instead.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as error:
        sys.modules.pop(name, None)
        raise RuntimeError(safe_error(error)) from None
    prepare_call = getattr(module, "prepare_call", None)
    if not callable(prepare_call):
        raise RuntimeError("adapter.py must define prepare_call")
    return prepare_call


def _invoke(runnable: object, messages: object) -> object:
    invoke = getattr(runnable, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("structured client does not expose invoke")
    return invoke(messages)


def _record_setup_failure(
    manifest: RunManifest, detail: object, output_path: Path | None
) -> RunManifest:
    """Persist a non-scoring setup failure without losing the slot plan."""

    current = replace(
        manifest,
        started_at=manifest.started_at or _now(),
        status="running",
        stop_reason=None,
        metrics=None,
    )
    if output_path is not None:
        persist_manifest(current, output_path)
    pending = pending_slots(current)
    if not pending:
        current = replace(current, status="paused", stop_reason="setup_error", metrics=None)
        if output_path is not None:
            persist_manifest(current, output_path)
        return current
    slot = pending[0]
    return record_slot_result(
        current,
        slot.key,
        SlotResult(
            kind="setup_error",
            detail=detail,
            attempts=1,
            status="paused",
            slot_key=slot.key,
            completed_at=_now(),
        ),
    )


def execute_run(
    manifest: RunManifest | Path,
    prompt_path: Path | None = None,
    prepare_call: Callable[[Path, object], Mapping[str, object]] | None = None,
    client: object | None = None,
    manifest_path: Path | None = None,
    *,
    cases: Sequence[object] | Mapping[str, object] | None = None,
    adapter_path: Path | None = None,
    eval_root: Path | None = None,
    credentials_path: Path | None = None,
) -> RunManifest:
    """Execute only pending slots and persist after each slot update.

    Retry ownership stays entirely with the fixed ``ChatOpenAI`` client.  The
    runner performs one structured invocation per slot; with the client's
    ``max_retries=2`` that gives no more than three transport attempts total.
    """

    if isinstance(manifest, Path):
        manifest = load_manifest(manifest)
    if not isinstance(manifest, RunManifest):
        raise TypeError("execute_run expects a RunManifest or manifest path")
    if manifest.mode == "verify" and manifest.dataset == "acceptance":
        raise UsageError("verify mode cannot use the acceptance dataset")
    output_path = manifest_path or manifest.manifest_path
    if output_path is not None and manifest.manifest_path != output_path:
        manifest = replace(manifest, manifest_path=output_path)
    if prompt_path is None and manifest.prompt_path is not None:
        prompt_path = Path(manifest.prompt_path)
    if prompt_path is None:
        raise ValueError("prompt_path is required")

    try:
        _validate_prompt_identity(manifest, prompt_path)
    except PromptIdentityError as error:
        paused = replace(
            manifest,
            status="paused",
            stop_reason="setup_error",
            metrics=None,
        )
        if output_path is not None:
            persist_manifest(paused, output_path)
        raise PromptIdentityError(str(error)) from None

    canonical_prompt = _canonical_prompt_path(prompt_path)
    current = replace(
        manifest,
        prompt_path=manifest.prompt_path or str(canonical_prompt),
        started_at=manifest.started_at or _now(),
        resumed_at=_now() if manifest.results else manifest.resumed_at,
        status="running",
        stop_reason=None,
        client_config=manifest.client_config or safe_client_config(),
    )
    if output_path is not None:
        persist_manifest(current, output_path)

    adapter_was_loaded = False
    if prepare_call is None:
        if adapter_path is None and eval_root is not None:
            adapter_path = eval_root / "adapter.py"
        if adapter_path is not None:
            try:
                prepare_call = load_adapter(adapter_path.parent)
                adapter_was_loaded = True
            except Exception as error:
                return _record_setup_failure(current, safe_error(error), output_path)
    if prepare_call is None:
        return _record_setup_failure(
            current, "prepare_call or adapter_path is required", output_path
        )
    if not callable(prepare_call):
        return _record_setup_failure(
            current, "adapter prepare_call is not callable", output_path
        )

    try:
        if isinstance(cases, Mapping):
            case_map: dict[str, object] | None = {
                str(key): (
                    value.case if isinstance(value, ValidatedCase) else value
                )
                for key, value in cases.items()
            }
        elif cases is not None:
            case_map = {
                _case_id(value): (
                    value.case if isinstance(value, ValidatedCase) else value
                )
                for value in cases
            }
        else:
            case_map = None
    except Exception as error:
        return _record_setup_failure(current, safe_error(error), output_path)

    if client is None:
        try:
            client = build_client(credentials_path)
        except Exception as error:
            return _record_setup_failure(current, safe_error(error), output_path)

    for slot in pending_slots(current):
        started = _now()
        previous_attempts = current.results.get(slot.key).attempts if slot.key in current.results else 0
        try:
            case_value = _case_for(current, slot.case_id, case_map)
            if adapter_was_loaded and not isinstance(case_value, EvalCase):
                raise CaseSetupError(
                    "adapter prepare_call requires a validated EvalCase"
                )
            call = prepare_call(canonical_prompt, case_value)
            if not isinstance(call, Mapping) or "messages" not in call or "schema" not in call:
                raise RuntimeError("adapter prepare_call must return messages and schema")
            schema = call["schema"]
            if not isinstance(schema, type) or not issubclass(schema, BaseModel):
                raise RuntimeError("adapter schema must be a Pydantic BaseModel class")
            schema_reference = _schema_import_reference(schema, adapter_path)
            if schema_reference is None:
                raise RuntimeError("adapter schema has no durable import reference")
            if current.schema_import is not None and current.schema_import != schema_reference:
                raise RuntimeError("adapter returned an incompatible Schema")
            if current.schema_import is None:
                current = replace(current, schema_import=schema_reference)
            structured = client.with_structured_output(
                schema,
                method="function_calling",
                include_raw=True,
            )
            response = _invoke(structured, call["messages"])
            classified = classify_response(response)
            slot_result = _slot_result_from_response(
                current, slot, classified, schema, previous_attempts + 1
            )
            slot_result = replace(slot_result, started_at=started, completed_at=_now())
        except Exception as error:
            classified_error = classify_exception(error)
            slot_result = SlotResult(
                kind=classified_error.kind,
                detail=classified_error.detail,
                attempts=previous_attempts + 1,
                slot_key=slot.key,
                started_at=started,
                completed_at=_now(),
            )
        current = record_slot_result(current, slot.key, slot_result)
        if current.results[slot.key].kind in NON_SCORING_KINDS:
            break

    if not pending_slots(current) and current.status == "running":
        current = replace(current, status="complete", completed_at=current.completed_at or _now())
        if current.manifest_path is not None:
            persist_manifest(current, current.manifest_path)
    elif pending_slots(current) and current.status == "running":
        current = replace(current, status="incomplete")
        if current.manifest_path is not None:
            persist_manifest(current, current.manifest_path)
    return current


def persist_manifest(manifest: RunManifest, path: Path) -> None:
    """Atomically persist JSON/YAML using a temporary in the target directory."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = manifest.to_dict()
    if destination.suffix.casefold() in {".yaml", ".yml"}:
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
    else:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


save_manifest = persist_manifest
write_manifest = persist_manifest


def load_manifest(path: Path) -> RunManifest:
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError("unable to read run manifest") from None
    try:
        value = yaml.safe_load(text) if source.suffix.casefold() in {".yaml", ".yml"} else json.loads(text)
    except Exception:
        raise RuntimeError("run manifest is malformed") from None
    if not isinstance(value, Mapping):
        raise RuntimeError("run manifest must contain an object")
    return RunManifest.from_dict(value, manifest_path=source)


def _load_cli_cases(path: Path) -> list[EvalCase]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        raise RuntimeError("case dataset is unreadable") from None
    if isinstance(value, Mapping) and set(value) == {"cases"}:
        value = value["cases"]
    if not isinstance(value, list):
        raise RuntimeError("case dataset must contain a list")
    try:
        return [EvalCase.model_validate(item) for item in value]
    except Exception:
        raise RuntimeError("case dataset contains an invalid case") from None


def _schema_from_adapter(
    prepare_call: Callable[[Path, object], Mapping[str, object]],
    prompt_path: Path,
    case: EvalCase,
) -> type[BaseModel]:
    """Discover the production Schema through the Task 3 adapter boundary."""

    try:
        call = prepare_call(prompt_path, case)
    except Exception as error:
        raise CaseSetupError(f"adapter schema discovery failed: {safe_error(error)}") from None
    if not isinstance(call, Mapping) or "schema" not in call:
        raise CaseSetupError("adapter prepare_call must return a production schema")
    schema = call["schema"]
    if not isinstance(schema, type) or not issubclass(schema, BaseModel):
        raise CaseSetupError("adapter schema must be a Pydantic BaseModel class")
    return schema


def _validated_external_cases(
    cases: Sequence[EvalCase], schema: type[BaseModel]
) -> list[ValidatedCase]:
    """Validate an explicitly supplied external split with the production Schema."""

    validated: list[ValidatedCase] = []
    for index, case in enumerate(cases):
        output = case.expect.get("output")
        if not isinstance(output, Mapping):
            raise CaseSetupError(
                f"external case {case.id!r} expect must contain an output object"
            )
        try:
            expected = schema.model_validate(output, extra="forbid")
        except Exception as error:
            raise CaseSetupError(
                f"external case {case.id!r} has an invalid expect.output at item {index}: {error}"
            ) from None
        missing = sorted(set(schema.model_fields) - set(expected.model_fields_set))
        if missing:
            raise CaseSetupError(
                f"external case {case.id!r} expect.output is missing schema field(s): "
                + ", ".join(missing)
            )
        validated.append(ValidatedCase(case=case, expected=expected))
    return validated


def _cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-root", type=Path, required=True)
    parser.add_argument("--prompt", type=Path, required=True)
    parser.add_argument("--dataset", choices=("dev", "validation", "acceptance", "external"), required=True)
    parser.add_argument(
        "--mode",
        choices=("tune", "verify"),
        default="tune",
        help="workflow owner; verify cannot select the acceptance dataset",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=None,
        help="repeat count; development/validation require 5 and acceptance requires 10",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--prompt-hash", default=None)
    parser.add_argument("--credentials", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _cli_parser()
    args = parser.parse_args(argv)
    # This guard intentionally runs immediately after argparse.  In
    # particular, it precedes manifest reads, case-file reads, adapter import,
    # client construction, and every model invocation.
    if args.mode == "verify" and args.dataset == "acceptance":
        payload = {
            "status": "error",
            "error": "verify mode cannot use the acceptance dataset",
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    default_repeats = DEFAULT_PHASE_REPEATS.get(args.dataset, 5)
    repeats = default_repeats if args.repeats is None else args.repeats
    if repeats < 1:
        parser.error("--repeats must be positive")
    required_repeats = DEFAULT_PHASE_REPEATS.get(args.dataset)
    if required_repeats is not None and repeats != required_repeats:
        parser.error(
            f"--dataset {args.dataset} requires exactly {required_repeats} repeats"
        )
    if args.dataset == "external":
        dataset_path = args.eval_root / "external-cases.yaml"
    else:
        dataset_path = args.eval_root / f"{args.dataset}-cases.yaml"
    prepare_call: Callable[[Path, object], Mapping[str, object]] | None = None
    if args.manifest.exists():
        manifest = load_manifest(args.manifest)
        if manifest.dataset != args.dataset:
            parser.error("manifest dataset does not match --dataset")
        if manifest.mode != args.mode:
            parser.error("manifest mode does not match --mode")
        if manifest.repeats != repeats:
            parser.error(
                f"manifest repeats {manifest.repeats} do not match required {repeats}"
            )
    else:
        prompt_hash = args.prompt_hash or hashlib.sha256(args.prompt.read_bytes()).hexdigest()
        raw_cases = _load_cli_cases(dataset_path)
        # Establish a durable slot plan before importing project code.  If the
        # adapter or case validation fails, that plan becomes the paused
        # setup-error evidence instead of disappearing with the exception.
        provisional = new_manifest(
            raw_cases,
            repeats,
            prompt_hash,
            prompt_path=args.prompt,
            manifest_path=args.manifest,
            dataset=args.dataset,
            mode=args.mode,
        )
        try:
            prepare_call = load_adapter(args.eval_root)
            if not raw_cases:
                raise CaseSetupError("selected case dataset must not be empty")
            schema = _schema_from_adapter(prepare_call, args.prompt, raw_cases[0])
            if args.dataset == "external":
                cases = _validated_external_cases(raw_cases, schema)
            else:
                cases = list(
                    load_case_split(dataset_path, schema, args.dataset)
                )
        except Exception as error:
            result = _record_setup_failure(
                provisional, safe_error(error), args.manifest
            )
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "pending": len(result.pending),
                        "metrics": _redacted(result.metrics),
                    },
                    ensure_ascii=False,
                )
            )
            return 0 if result.status == "complete" else 2
        manifest = new_manifest(
            cases,
            repeats,
            prompt_hash,
            prompt_path=args.prompt,
            manifest_path=args.manifest,
            dataset=args.dataset,
            mode=args.mode,
            schema=schema,
        )
    result = execute_run(
        manifest,
        prompt_path=args.prompt,
        prepare_call=prepare_call,
        adapter_path=args.eval_root / "adapter.py" if prepare_call is None else None,
        manifest_path=args.manifest,
        credentials_path=args.credentials,
    )
    print(json.dumps({"status": result.status, "mode": result.mode, "pending": len(result.pending), "metrics": _redacted(result.metrics)}, ensure_ascii=False))
    return 0 if result.status == "complete" else 2


__all__ = [
    "CallSlot",
    "ClassifiedResult",
    "ADAPTER_SCHEMA_REFERENCE_PREFIX",
    "DEFAULT_PHASE_REPEATS",
    "RunManifest",
    "SlotResult",
    "UsageError",
    "classify_exception",
    "classify_parsing_failure",
    "classify_response",
    "execute_run",
    "load_adapter",
    "load_manifest",
    "new_manifest",
    "pending_slots",
    "persist_manifest",
    "record_slot_result",
    "save_manifest",
    "write_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
