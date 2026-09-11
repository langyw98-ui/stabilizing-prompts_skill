"""Deterministically score completed prompt-evaluation manifests.

The runner stores only JSON-safe snapshots of cases and structured responses.
This module is the production-schema boundary for those snapshots: expected
and actual values are reconstructed with the recorded Pydantic type before
object equality or field-level evidence is produced.  A manifest that is not
fully complete, or that contains infrastructure/protocol evidence, is not a
scorable run and never receives partial or guessed metrics.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import importlib
import importlib.util
from pathlib import Path
import re
import sys
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

try:
    from scripts.run_prompt_eval import (
        ADAPTER_SCHEMA_REFERENCE_PREFIX,
        DEFAULT_PHASE_REPEATS,
        NON_SCORING_KINDS,
        RunManifest,
        SlotResult,
        _schema_import_reference as _runtime_schema_import_reference,
        load_manifest,
        redact_secret,
    )
except ModuleNotFoundError:  # pragma: no cover - direct-script compatibility
    from run_prompt_eval import (
        ADAPTER_SCHEMA_REFERENCE_PREFIX,
        DEFAULT_PHASE_REPEATS,
        NON_SCORING_KINDS,
        RunManifest,
        SlotResult,
        _schema_import_reference as _runtime_schema_import_reference,
        load_manifest,
        redact_secret,
    )


SCORING_KINDS = frozenset({"parse_error", "schema_error", "business_error", "pass"})
_MISSING = "[MISSING]"
_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


class ScoreError(ValueError):
    """Raised when a manifest cannot provide trustworthy final metrics."""


# A descriptive compatibility spelling for callers that prefer the noun used
# in the task brief.  Both names intentionally identify the same failure type.
ScoringError = ScoreError


def _schema_import_reference(schema: type[BaseModel]) -> str:
    reference = _runtime_schema_import_reference(schema)
    if not isinstance(reference, str):
        raise ScoreError("production schema has no import reference")
    return reference


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _adapter_reference_parts(reference: str) -> tuple[Path, str, str] | None:
    if not reference.startswith(ADAPTER_SCHEMA_REFERENCE_PREFIX + "|"):
        return None
    parts = reference.split("|")
    if len(parts) != 4 or parts[0] != ADAPTER_SCHEMA_REFERENCE_PREFIX:
        raise ScoreError("manifest adapter Schema reference is malformed")
    raw_path, digest, qualname = parts[1:]
    if not raw_path or not Path(raw_path).is_absolute():
        raise ScoreError("manifest adapter Schema reference path must be absolute")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ScoreError("manifest adapter Schema reference hash is malformed")
    components = qualname.split(".")
    if not components or any(not component.isidentifier() for component in components):
        raise ScoreError("manifest adapter Schema reference qualname is malformed")
    return Path(raw_path), digest, qualname


def _schema_from_reference(
    reference: str,
    *,
    manifest_path: Path | None = None,
    prompt_path: str | None = None,
) -> type[BaseModel]:
    """Resolve the production Schema recorded in a completed manifest."""

    adapter_parts = _adapter_reference_parts(reference)
    if adapter_parts is not None:
        adapter_path, expected_hash, qualname = adapter_parts
        try:
            adapter_path = adapter_path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise ScoreError("recorded adapter Schema source is unavailable") from None
        if adapter_path.name != "adapter.py" or not adapter_path.is_file():
            raise ScoreError("recorded adapter Schema source is outside the adapter boundary")

        # A persisted manifest may only import the adapter belonging to its
        # evaluation root.  This keeps the scorer from becoming a general
        # arbitrary-file import primitive when given a crafted manifest.
        roots: list[Path] = []
        if manifest_path is not None:
            manifest_parent = Path(manifest_path).resolve(strict=False).parent
            roots.append(manifest_parent)
            if manifest_parent.name in {".runtime", "reports"}:
                roots.append(manifest_parent.parent)
        if isinstance(prompt_path, str):
            roots.append(Path(prompt_path).resolve(strict=False).parent)
        if not roots:
            raise ScoreError("manifest adapter Schema reference lacks a file boundary")
        if not any(_is_within(adapter_path, root) for root in roots):
            raise ScoreError("recorded adapter Schema source is outside the manifest boundary")
        try:
            actual_hash = hashlib.sha256(adapter_path.read_bytes()).hexdigest()
        except OSError:
            raise ScoreError("recorded adapter Schema source is unreadable") from None
        if actual_hash != expected_hash:
            raise ScoreError("recorded adapter Schema source hash does not match manifest")

        module_name = f"_stabilizing_prompts_adapter_{expected_hash[:16]}"
        spec = importlib.util.spec_from_file_location(module_name, adapter_path)
        if spec is None or spec.loader is None:
            raise ScoreError("unable to load recorded adapter Schema source")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            value: object = module
            for part in qualname.split("."):
                value = getattr(value, part)
        except Exception:
            sys.modules.pop(module_name, None)
            raise ScoreError("unable to import production schema recorded by manifest") from None
        return _require_schema(value)

    module_name, separator, qualname = reference.partition(":")
    if not separator or not module_name or not qualname:
        raise ScoreError("manifest schema import reference is malformed")
    try:
        value: object = importlib.import_module(module_name)
        for part in qualname.split("."):
            value = getattr(value, part)
    except Exception as error:
        raise ScoreError(
            "unable to import production schema recorded by manifest"
        ) from error
    return _require_schema(value)


def _require_schema(schema: object) -> type[BaseModel]:
    try:
        valid = isinstance(schema, type) and issubclass(schema, BaseModel)
    except TypeError:
        valid = False
    if not valid:
        raise ScoreError("schema must be a Pydantic BaseModel subclass")
    return schema


def _safe_value_raw(value: object) -> object:
    """Return a JSON-compatible value without evaluating user code."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, BaseModel):
        try:
            return _safe_value_raw(value.model_dump(mode="json"))
        except Exception:
            return str(value)
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value_raw(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value_raw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_safe_value_raw(item) for item in value), key=repr)
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _safe_value(value: object) -> object:
    """Return a JSON-compatible, credential-redacted report value."""

    try:
        return redact_secret(_safe_value_raw(value))
    except Exception:
        return "[REDACTION_FAILED]"


def _model_mapping(value: BaseModel) -> Mapping[str, object]:
    try:
        dumped = value.model_dump(mode="python", by_alias=False)
    except Exception as error:  # pragma: no cover - defensive for custom models
        raise ScoreError(f"unable to serialize production schema object: {error}") from None
    if not isinstance(dumped, Mapping):  # pragma: no cover - pydantic contract
        raise ScoreError("production schema object did not serialize to an object")
    return dumped


def _model_state(value: BaseModel) -> dict[str, object]:
    """Capture Pydantic equality state omitted by ``model_dump``.

    Pydantic compares private attributes, extra fields, and user-added
    ``__dict__`` state in addition to declared fields.  Keep those components
    under stable names so a business mismatch never loses its evidence merely
    because the differing state is not a public model field.
    """

    field_names = set(getattr(type(value), "model_fields", {}))
    raw_dict = getattr(value, "__dict__", {})
    internal: Mapping[object, object]
    if isinstance(raw_dict, Mapping):
        internal = {
            key: item
            for key, item in raw_dict.items()
            if key not in field_names
        }
    else:
        internal = {}
    private = getattr(value, "__pydantic_private__", None)
    extra = getattr(value, "__pydantic_extra__", None)
    return {
        "private": private if isinstance(private, Mapping) else {},
        "extra": extra if isinstance(extra, Mapping) else {},
        "internal": internal,
    }


def _model_state_path(path: str) -> str:
    return f"{path}.$model_state" if path else "$model_state"


def _path_for_key(path: str, key: object) -> str:
    text = str(key)
    return f"{path}.{text}" if path else text


def _diff_values(expected: object, actual: object, path: str, output: list[dict[str, object]]) -> None:
    """Recursively append complete-object differences in deterministic order."""

    if isinstance(expected, BaseModel) and isinstance(actual, BaseModel):
        if type(expected) is not type(actual):
            output.append(
                {
                    "path": path,
                    "expected": _safe_value(expected),
                    "actual": _safe_value(actual),
                }
            )
            return
        expected_map = _model_mapping(expected)
        actual_map = _model_mapping(actual)
        keys = list(expected_map)
        keys.extend(
            sorted(set(actual_map) - set(expected_map), key=str)
        )
        for key in keys:
            child_path = _path_for_key(path, key)
            if key not in expected_map:
                output.append(
                    {
                        "path": child_path,
                        "expected": _MISSING,
                        "actual": _safe_value(actual_map[key]),
                    }
                )
            elif key not in actual_map:
                output.append(
                    {
                        "path": child_path,
                        "expected": _safe_value(expected_map[key]),
                        "actual": _MISSING,
                    }
                )
            else:
                _diff_values(expected_map[key], actual_map[key], child_path, output)
        expected_state = _model_state(expected)
        actual_state = _model_state(actual)
        try:
            state_differs = expected_state != actual_state
        except Exception:
            state_differs = True
        if state_differs:
            output.append(
                {
                    "path": _model_state_path(path),
                    "expected": _safe_value(expected_state),
                    "actual": _safe_value(actual_state),
                }
            )
        return

    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        keys = sorted(set(expected) | set(actual), key=str)
        for key in keys:
            child_path = _path_for_key(path, key)
            if key not in expected:
                output.append(
                    {
                        "path": child_path,
                        "expected": _MISSING,
                        "actual": _safe_value(actual[key]),
                    }
                )
            elif key not in actual:
                output.append(
                    {
                        "path": child_path,
                        "expected": _safe_value(expected[key]),
                        "actual": _MISSING,
                    }
                )
            else:
                _diff_values(expected[key], actual[key], child_path, output)
        return

    if isinstance(expected, Sequence) and not isinstance(expected, (str, bytes, bytearray)) and isinstance(actual, Sequence) and not isinstance(actual, (str, bytes, bytearray)):
        length = max(len(expected), len(actual))
        for index in range(length):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            if index >= len(expected):
                output.append(
                    {
                        "path": child_path,
                        "expected": _MISSING,
                        "actual": _safe_value(actual[index]),
                    }
                )
            elif index >= len(actual):
                output.append(
                    {
                        "path": child_path,
                        "expected": _safe_value(expected[index]),
                        "actual": _MISSING,
                    }
                )
            else:
                _diff_values(expected[index], actual[index], child_path, output)
        return

    if isinstance(expected, (set, frozenset)) and isinstance(actual, (set, frozenset)):
        if expected != actual:
            output.append(
                {
                    "path": path,
                    "expected": _safe_value(expected),
                    "actual": _safe_value(actual),
                }
            )
        return

    if expected != actual:
        output.append(
            {
                "path": path,
                "expected": _safe_value(expected),
                "actual": _safe_value(actual),
            }
        )


def field_diff(expected: BaseModel, actual: BaseModel) -> list[dict[str, object]]:
    """Return deterministic field paths for a complete production-object diff."""

    if not isinstance(expected, BaseModel) or not isinstance(actual, BaseModel):
        raise TypeError("field_diff expects two Pydantic BaseModel objects")
    output: list[dict[str, object]] = []
    _diff_values(expected, actual, "", output)
    if not output:
        try:
            objects_differ = expected != actual
        except Exception:
            objects_differ = True
        if objects_differ:
            output.append(
                {
                    "path": "$model_state",
                    "expected": _safe_value(
                        {"fields": _model_mapping(expected), **_model_state(expected)}
                    ),
                    "actual": _safe_value(
                        {"fields": _model_mapping(actual), **_model_state(actual)}
                    ),
                }
            )
    return output


def _validate_complete_object(
    value: object,
    schema: type[_SchemaT],
    *,
    label: str,
) -> _SchemaT:
    if isinstance(value, schema):
        model = value
    elif isinstance(value, Mapping):
        try:
            # Persisted runner snapshots use Pydantic's canonical field names,
            # while case files and production structured output may use aliases.
            # Accept both spellings at this reconstruction boundary without
            # changing the production model's field/extra validation rules.
            model = schema.model_validate(
                value,
                extra="forbid",
                by_alias=True,
                by_name=True,
            )
        except ValidationError as error:
            raise ScoreError(f"{label} is not a valid production Schema object: {error}") from None
    else:
        raise ScoreError(f"{label} is not a production Schema object")

    missing = sorted(set(schema.model_fields) - set(model.model_fields_set))
    if missing:
        raise ScoreError(
            f"{label} is missing schema field(s): " + ", ".join(missing)
        )
    return model


def _case_payload(manifest: RunManifest, case_id: str) -> Mapping[str, object]:
    payload = manifest.case_data.get(case_id)
    if not isinstance(payload, Mapping):
        raise ScoreError(f"manifest case {case_id!r} is missing its persisted case data")
    return payload


def _expected_object(
    manifest: RunManifest, case_id: str, schema: type[_SchemaT]
) -> _SchemaT:
    payload = _case_payload(manifest, case_id)
    expected = payload.get("expected")
    if expected is None:
        expect = payload.get("expect")
        expected = expect.get("output") if isinstance(expect, Mapping) else None
    if not isinstance(expected, Mapping):
        raise ScoreError(f"manifest case {case_id!r} has no complete expected output")
    return _validate_complete_object(
        expected,
        schema,
        label=f"case {case_id!r} expected output",
    )


@dataclass(frozen=True, slots=True)
class CaseScore:
    """Exact classification summary for one case across its fixed slots."""

    case_id: str
    priority: str = "normal"
    repeats: int = 0
    pass_count: int = 0
    parse_error_count: int = 0
    schema_error_count: int = 0
    business_error_count: int = 0
    classifications: tuple[str, ...] = ()
    field_diffs: tuple[tuple[dict[str, object], ...], ...] = ()

    @property
    def total_responses(self) -> int:
        return len(self.classifications) if self.classifications else self.repeats

    @property
    def scored_responses(self) -> int:
        return self.total_responses

    @property
    def passes(self) -> int:
        return self.pass_count

    @property
    def passed(self) -> int:
        """Compatibility spelling for the number of passing repeats."""

        return self.pass_count

    @property
    def stable(self) -> bool:
        if self.classifications:
            return all(
                classification == "pass" for classification in self.classifications
            )
        return (
            self.repeats > 0
            and self.pass_count == self.repeats
            and self.parse_error_count == 0
            and self.schema_error_count == 0
            and self.business_error_count == 0
        )

    @property
    def stable_case(self) -> bool:
        return self.stable

    def meets(self, repeats_required: int) -> bool:
        if repeats_required < 1:
            raise ValueError("repeats_required must be positive")
        if self.priority == "critical":
            return self.total_responses > 0 and self.pass_count == self.total_responses
        return self.pass_count >= repeats_required

    def meets_gate(self, repeats_required: int) -> bool:
        return self.meets(repeats_required)

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "priority": self.priority,
            "repeats": self.repeats,
            "total_responses": self.total_responses,
            "scored_responses": self.scored_responses,
            "pass": self.pass_count,
            "parse_error": self.parse_error_count,
            "schema_error": self.schema_error_count,
            "business_error": self.business_error_count,
            "stable": self.stable,
            "classifications": list(self.classifications),
            "field_diffs": [list(diffs) for diffs in self.field_diffs],
        }


class CaseScoreCollection(tuple[CaseScore, ...]):
    """Tuple-ordered case scores with convenient case-id lookup."""

    def __new__(cls, values: Sequence[CaseScore] = ()) -> "CaseScoreCollection":
        return super().__new__(cls, values)

    def __getitem__(self, key: int | slice | str) -> CaseScore | tuple[CaseScore, ...]:
        if isinstance(key, str):
            for case in self:
                if case.case_id == key:
                    return case
            raise KeyError(key)
        return super().__getitem__(key)

    def get(self, case_id: str, default: CaseScore | None = None) -> CaseScore | None:
        try:
            return self[case_id]  # type: ignore[return-value]
        except KeyError:
            return default

    def values(self) -> tuple[CaseScore, ...]:
        return tuple(self)

    def by_id(self) -> Mapping[str, CaseScore]:
        return {case.case_id: case for case in self}


@dataclass(frozen=True, slots=True)
class RunMetrics:
    """Run-level metrics with exact decimal rates and case evidence."""

    scored_responses: int
    pass_count: int
    parse_error_count: int
    schema_error_count: int
    business_error_count: int
    schema_valid_rate: Decimal
    run_accuracy: Decimal
    stable_case_rate: Decimal
    case_scores: CaseScoreCollection = CaseScoreCollection()

    def __post_init__(self) -> None:
        for name in (
            "schema_valid_rate",
            "run_accuracy",
            "stable_case_rate",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                try:
                    value = Decimal(str(value))
                except Exception as error:
                    raise ValueError(f"{name} must be a decimal") from error
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
            object.__setattr__(self, name, value)
        if isinstance(self.case_scores, Mapping):
            values = tuple(self.case_scores.values())
        else:
            values = tuple(self.case_scores)
        object.__setattr__(self, "case_scores", CaseScoreCollection(values))

    @property
    def passes(self) -> int:
        return self.pass_count

    @property
    def counts(self) -> Mapping[str, int]:
        return {
            "pass": self.pass_count,
            "parse_error": self.parse_error_count,
            "schema_error": self.schema_error_count,
            "business_error": self.business_error_count,
        }

    @property
    def case_count(self) -> int:
        return len(self.case_scores)

    @property
    def cases(self) -> Mapping[str, CaseScore]:
        return self.case_scores.by_id()

    @property
    def stable_cases(self) -> int:
        return sum(case.stable for case in self.case_scores)

    def case_score(self, case_id: str) -> CaseScore:
        for case in self.case_scores:
            if case.case_id == case_id:
                return case
        raise KeyError(case_id)

    def to_dict(self) -> dict[str, object]:
        return {
            "scored_responses": self.scored_responses,
            "pass": self.pass_count,
            "parse_error": self.parse_error_count,
            "schema_error": self.schema_error_count,
            "business_error": self.business_error_count,
            "schema_valid_rate": str(self.schema_valid_rate),
            "run_accuracy": str(self.run_accuracy),
            "stable_case_rate": str(self.stable_case_rate),
            "case_scores": [case.to_dict() for case in self.case_scores],
        }


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        raise ScoreError("completed run has no scored responses")
    return Decimal(numerator) / Decimal(denominator)


def _validate_manifest_phase_repeats(manifest: RunManifest) -> None:
    dataset = "dev" if manifest.dataset == "development" else manifest.dataset
    if dataset not in DEFAULT_PHASE_REPEATS:
        return
    expected = DEFAULT_PHASE_REPEATS[dataset]
    if manifest.repeats != expected:
        raise ScoreError(
            f"manifest repeats must be exactly {expected} for {dataset}"
        )


def score_run(
    manifest: RunManifest | Path,
    schema: type[_SchemaT] | None = None,
) -> RunMetrics:
    """Score a completed manifest using its recorded production Schema type."""

    if isinstance(manifest, (str, Path)):
        try:
            manifest = load_manifest(Path(manifest))
        except Exception as error:
            raise ScoreError(f"unable to load run manifest: {error}") from None
    if not isinstance(manifest, RunManifest):
        raise TypeError("score_run expects a RunManifest or manifest path")
    _validate_manifest_phase_repeats(manifest)
    if any(
        isinstance(result, SlotResult) and result.kind in NON_SCORING_KINDS
        for result in manifest.results.values()
    ):
        raise ScoreError("run contains non-scoring result; final metrics are unavailable")
    if manifest.status != "complete" or manifest.pending:
        raise ScoreError("run is incomplete; final metrics are unavailable")
    if not manifest.slots:
        raise ScoreError("completed run has no call slots")
    if not manifest.schema_import:
        raise ScoreError("manifest schema import reference is missing")
    production_schema = (
        _require_schema(schema)
        if schema is not None
        else _schema_from_reference(
            manifest.schema_import,
            manifest_path=manifest.manifest_path,
            prompt_path=manifest.prompt_path,
        )
    )
    expected_ref = _schema_import_reference(production_schema)
    if manifest.schema_import != expected_ref:
        raise ScoreError(
            "manifest schema import reference is incompatible with the production schema"
        )

    case_expected: dict[str, BaseModel] = {}
    for slot in manifest.slots:
        if slot.case_id not in case_expected:
            case_expected[slot.case_id] = _expected_object(
                manifest, slot.case_id, production_schema
            )

    grouped: dict[str, list[tuple[str, SlotResult, list[dict[str, object]]]]] = {}
    ordered_case_ids: list[str] = []
    for slot in manifest.slots:
        if slot.case_id not in ordered_case_ids:
            ordered_case_ids.append(slot.case_id)
        result = manifest.results.get(slot.key)
        if result is not None and result.kind in NON_SCORING_KINDS:
            raise ScoreError(
                f"run contains non-scoring result {result.kind!r}; final metrics are unavailable"
            )
        if result is None or not result.is_complete:
            raise ScoreError("run is incomplete; final metrics are unavailable")
        kind = result.kind
        if kind not in SCORING_KINDS:
            raise ScoreError(f"run contains unknown result classification {kind!r}")

        differences: list[dict[str, object]] = []
        if kind in {"pass", "business_error"}:
            expected = case_expected[slot.case_id]
            actual = _validate_complete_object(
                result.parsed,
                production_schema,
                label=f"case {slot.case_id!r} slot {slot.key!r} actual output",
            )
            differences = field_diff(expected, actual)
            # The complete production-object comparison is authoritative.  A
            # stale/mislabeled persisted kind cannot manufacture a pass or hide
            # a business mismatch.
            kind = "pass" if expected == actual else "business_error"
        grouped.setdefault(slot.case_id, []).append((kind, result, differences))

    case_scores: list[CaseScore] = []
    for case_id in ordered_case_ids:
        payload = _case_payload(manifest, case_id)
        priority = payload.get("priority", "normal")
        if not isinstance(priority, str) or priority not in {"normal", "critical"}:
            raise ScoreError(f"case {case_id!r} has an invalid priority")
        entries = grouped[case_id]
        classifications = tuple(entry[0] for entry in entries)
        if manifest.repeats and len(entries) != manifest.repeats:
            raise ScoreError(
                f"case {case_id!r} has {len(entries)} results; expected {manifest.repeats}"
            )
        case_scores.append(
            CaseScore(
                case_id=case_id,
                priority=priority,
                repeats=len(entries),
                pass_count=classifications.count("pass"),
                parse_error_count=classifications.count("parse_error"),
                schema_error_count=classifications.count("schema_error"),
                business_error_count=classifications.count("business_error"),
                classifications=classifications,
                field_diffs=tuple(tuple(entry[2]) for entry in entries),
            )
        )

    counts = {
        "pass": sum(case.pass_count for case in case_scores),
        "parse_error": sum(case.parse_error_count for case in case_scores),
        "schema_error": sum(case.schema_error_count for case in case_scores),
        "business_error": sum(case.business_error_count for case in case_scores),
    }
    scored = sum(counts.values())
    valid = counts["business_error"] + counts["pass"]
    stable = sum(case.stable for case in case_scores)
    return RunMetrics(
        scored_responses=scored,
        pass_count=counts["pass"],
        parse_error_count=counts["parse_error"],
        schema_error_count=counts["schema_error"],
        business_error_count=counts["business_error"],
        schema_valid_rate=_ratio(valid, scored),
        run_accuracy=_ratio(counts["pass"], scored),
        stable_case_rate=_ratio(stable, len(case_scores)),
        case_scores=tuple(case_scores),
    )


def report_json(metrics: RunMetrics) -> str:
    """Serialize metrics for the scoring CLI without floating-point rates."""

    return json.dumps(metrics.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Score one completed manifest and write a report without model calls."""

    args = _parser().parse_args(argv)
    try:
        metrics = score_run(load_manifest(args.manifest))
        payload = {"status": "complete", "metrics": metrics.to_dict()}
        exit_code = 0
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
    "CaseScore",
    "CaseScoreCollection",
    "RunMetrics",
    "ScoreError",
    "ScoringError",
    "field_diff",
    "main",
    "report_json",
    "score_run",
]


if __name__ == "__main__":  # pragma: no cover - CLI exercised through main below
    raise SystemExit(main())
