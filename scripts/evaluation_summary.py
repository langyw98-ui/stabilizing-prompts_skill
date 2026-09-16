"""Normalize scored terminal outcomes and render deterministic summaries.

This module is deliberately a small, pure boundary between persisted scoring
evidence and user-facing Markdown.  It does not inspect a model response,
read a worktree, or perform any Git operation.  All values crossing the
boundary are copied into immutable JSON-compatible containers before they are
rendered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
import math
from pathlib import Path, PureWindowsPath
import re
from types import MappingProxyType


FORMAL_KINDS = frozenset(
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


_RESULT_MATRIX: dict[str, tuple[str, bool, str]] = {
    "no_change_needed": ("assets", False, "not_run"),
    "no_strict_improvement": ("assets", False, "not_run"),
    "validation_failed": ("assets", False, "not_run"),
    "no_improvement_limit": ("assets", False, "not_run"),
    "round_limit": ("assets", False, "not_run"),
    "acceptance_failed": ("assets", False, "failed"),
    "acceptance_passed": ("success", True, "passed"),
}
_PHASES = ("dev", "validation", "acceptance")
_PHASE_ALIASES = {"development": "dev", "dev": "dev", "validation": "validation", "acceptance": "acceptance"}
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SummaryError(ValueError):
    """Raised when persisted summary evidence is not trustworthy."""


_SENSITIVE_KEY_PARTS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "authorization",
        "bearer",
        "credential",
        "password",
        "secret",
        "token",
    }
)
_PRIVATE_KEY_PARTS = frozenset(
    {
        "absolute_path",
        "cleanup",
        "commit",
        "confirmation",
        "delivery",
        "delivery_commit",
        "delivery_status",
        "file",
        "hash",
        "manifest",
        "patch",
        "path",
        "prepared_commit",
        "private_worktree",
        "raw",
        "response",
        "sha",
        "sha256",
        "worktree",
    }
)
_MARKDOWN_META = frozenset("\\`*_{}[]()#+-.!|<>")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_AUTH_RE = re.compile(
    r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;\"']+"
)
_PRIVATE_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\[^\r\n\"']+")
_PRIVATE_POSIX_PATH_RE = re.compile(
    r"(?i)(?<![\w:])/(?!/)(?:[^\s,;\"']+/)*(?:private|worktrees?)(?:/[^\s,;\"']*)?"
)
_FORBIDDEN_TEXT_RE = re.compile(
    r"(?i)\b(?:raw\s+response|prepared_commit|delivery_commit|delivered)\b"
)


def _normalise_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _safe_text(value: str) -> str:
    """Copy text and remove content that is not suitable for a summary."""

    if "\x00" in value:
        raise SummaryError("evidence contains a NUL character")
    text = _normalise_newlines(value)
    # A value must not be able to create a second Markdown line or heading.
    text = text.replace("\n", " ")
    text = _AUTH_RE.sub("[REDACTED]", text)
    text = _BEARER_RE.sub("[REDACTED]", text)
    text = _PRIVATE_PATH_RE.sub("[PATH REDACTED]", text)
    text = _PRIVATE_POSIX_PATH_RE.sub("[PATH REDACTED]", text)
    text = _FORBIDDEN_TEXT_RE.sub("[OMITTED]", text)
    return text


def _normalise_key(key: str) -> str:
    return re.sub(r"[-\s]+", "_", key.casefold())


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalise_key(key)
    pieces = set(normalized.split("_"))
    return normalized in _SENSITIVE_KEY_PARTS or bool(pieces & _SENSITIVE_KEY_PARTS)


def _is_private_key(key: str) -> bool:
    normalized = _normalise_key(key)
    pieces = set(normalized.split("_"))
    return normalized in _PRIVATE_KEY_PARTS or bool(pieces & _PRIVATE_KEY_PARTS)


def _freeze_json(value: object, *, label: str, stack: set[int] | None = None) -> object:
    """Deep-copy a finite JSON value into immutable containers.

    Sensitive and internal machine-state fields are omitted before their
    values are traversed.  This both prevents leakage and keeps the summary
    independent of fields that are intentionally not part of the content
    contract.
    """

    active = set() if stack is None else stack
    if value is None or type(value) is bool or type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SummaryError(f"{label} must contain only finite numbers")
        return value
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active:
            raise SummaryError(f"{label} contains a cyclic value")
        active.add(identity)
        try:
            normalized: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise SummaryError(f"{label} has a non-string key")
                # These fields are intentionally not part of a user summary.
                if _is_sensitive_key(key) or _is_private_key(key):
                    continue
                normalized[_safe_text(key)] = _freeze_json(
                    item, label=f"{label}.{key}", stack=active
                )
            return MappingProxyType(dict(sorted(normalized.items(), key=lambda pair: pair[0])))
        finally:
            active.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise SummaryError(f"{label} contains a cyclic value")
        active.add(identity)
        try:
            return tuple(
                _freeze_json(item, label=f"{label}[{index}]", stack=active)
                for index, item in enumerate(value)
            )
        finally:
            active.remove(identity)
    # Sets, Path objects, Decimal, datetimes, and arbitrary objects are not
    # JSON-compatible deterministic evidence.
    raise SummaryError(f"{label} contains a non-deterministic value")


def _require_nonempty_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SummaryError(f"{label} must be a non-empty string")
    return _safe_text(value)


def _validate_timestamp(value: object, *, label: str = "finished_at_utc") -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise SummaryError(f"{label} must be an exact UTC timestamp ending in Z")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise SummaryError(f"{label} is not a valid UTC timestamp") from None
    return value


def _validate_relative_prompt_path(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SummaryError("prompt_path must be a non-empty string")
    # Check the caller's raw spelling before secret/path redaction can turn a
    # drive-qualified path into a relative-looking placeholder.
    raw_path = _normalise_newlines(value).replace("\n", "")
    # Path.is_absolute() follows the host platform.  PureWindowsPath catches
    # drive-qualified and UNC paths even when this module runs on POSIX.
    if Path(raw_path).is_absolute() or PureWindowsPath(raw_path).is_absolute() or PureWindowsPath(raw_path).drive:
        raise SummaryError("prompt_path must be repository-relative")
    path = _safe_text(value)
    normalized = path.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise SummaryError("prompt_path must stay within the repository")
    return "/".join(parts)


def _phase_mapping(value: object, *, label: str) -> MappingProxyType:
    if not isinstance(value, Mapping):
        raise SummaryError(f"{label} must be a mapping")
    normalized: dict[str, int] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            raise SummaryError(f"{label} has a non-string phase")
        phase = _PHASE_ALIASES.get(raw_key.casefold())
        if phase is None:
            raise SummaryError(f"{label} contains unknown phase {raw_key!r}")
        if phase in normalized:
            raise SummaryError(f"{label} contains duplicate phase {phase!r}")
        if type(raw_value) is not int or raw_value < 0:
            raise SummaryError(f"{label}.{phase} must be a non-negative integer")
        normalized[phase] = raw_value
    if "dev" not in normalized or "validation" not in normalized:
        raise SummaryError(f"{label} is missing dev or validation evidence")
    return MappingProxyType(dict(sorted(normalized.items())))


def _mapping(value: object, *, label: str, require_nonempty: bool = True) -> MappingProxyType:
    normalized = _freeze_json(value, label=label)
    if not isinstance(normalized, Mapping):
        raise SummaryError(f"{label} must be a mapping")
    if require_nonempty and not normalized:
        raise SummaryError(f"{label} must not be empty")
    return normalized


def _acceptance_records(comparisons: Mapping[str, object], metrics: Mapping[str, object]) -> tuple[tuple[str, str, str | None], ...]:
    records: list[tuple[str, str, str | None]] = []
    for source_name, source in (("comparisons", comparisons), ("metrics", metrics)):
        direct = source.get("acceptance_status")
        if direct is not None:
            records.append((source_name, "acceptance_status", _coerce_acceptance_status(direct, label=f"{source_name}.acceptance_status")))
        payload = source.get("acceptance")
        if isinstance(payload, Mapping):
            status_value = payload.get("status")
            if status_value is None and "ran" in payload:
                ran = payload.get("ran")
                if type(ran) is not bool:
                    raise SummaryError(f"{source_name}.acceptance.ran must be boolean")
                if not ran:
                    status_value = "not_run"
            if status_value is None and "passed" in payload:
                passed = payload.get("passed")
                if passed is None:
                    status_value = "not_run"
                elif type(passed) is bool:
                    status_value = "passed" if passed else "failed"
                else:
                    raise SummaryError(f"{source_name}.acceptance.passed must be boolean or null")
            if status_value is not None:
                records.append((source_name, "acceptance.status", _coerce_acceptance_status(status_value, label=f"{source_name}.acceptance.status")))
            if "passed" in payload and payload.get("passed") is not None:
                passed = payload.get("passed")
                if type(passed) is not bool:
                    raise SummaryError(f"{source_name}.acceptance.passed must be boolean or null")
                if status_value is not None and (status_value == "passed") != passed:
                    raise SummaryError("acceptance evidence contains contradictory status")
    statuses = {status for _, _, status in records}
    if len(statuses) > 1:
        raise SummaryError("acceptance evidence contains contradictory status")
    return tuple(records)


def _coerce_acceptance_status(value: object, *, label: str) -> str:
    if not isinstance(value, str) or value not in {"not_run", "failed", "passed"}:
        raise SummaryError(f"{label} has an invalid acceptance status")
    return value


def _acceptance_status(evidence: "SummaryEvidence") -> str | None:
    records = _acceptance_records(evidence.comparisons, evidence.metrics)
    return records[0][2] if records else None


def _acceptance_reason(evidence: "SummaryEvidence") -> str | None:
    for source in (evidence.comparisons, evidence.metrics):
        payload = source.get("acceptance")
        if isinstance(payload, Mapping):
            for key in ("reason", "rule_reason", "explanation", "stop_reason"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        for key in (
            "acceptance_reason",
            "acceptance_rule_reason",
            "acceptance_not_run_reason",
            "not_run_reason",
        ):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


@dataclass(frozen=True, slots=True)
class FormalResult:
    """Immutable normalized scored terminal outcome."""

    kind: str
    stop_reason: str
    finished_at_utc: str
    delivery_profile: str
    prompt_should_change: bool
    acceptance_status: str

    def __post_init__(self) -> None:
        if self.kind not in FORMAL_KINDS:
            raise SummaryError(f"unknown formal result kind: {self.kind!r}")
        profile, prompt_change, acceptance = _RESULT_MATRIX[self.kind]
        if self.delivery_profile != profile or type(self.prompt_should_change) is not bool or self.prompt_should_change != prompt_change:
            raise SummaryError("formal result delivery matrix is contradictory")
        if self.acceptance_status != acceptance:
            raise SummaryError("formal result acceptance matrix is contradictory")
        object.__setattr__(self, "stop_reason", _require_nonempty_text(self.stop_reason, label="stop_reason"))
        _validate_timestamp(self.finished_at_utc)
        if not isinstance(self.delivery_profile, str) or not isinstance(self.acceptance_status, str):
            raise SummaryError("formal result fields have invalid types")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "FormalResult":
        if not isinstance(value, Mapping):
            raise SummaryError("formal result must be a mapping")
        names = {field.name for field in fields(cls)}
        unknown = set(value) - names
        missing = names - set(value)
        if unknown:
            raise SummaryError("formal result contains unknown field(s)")
        if missing:
            raise SummaryError("formal result is missing required field(s)")
        return cls(**{name: value[name] for name in names})  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "stop_reason": self.stop_reason,
            "finished_at_utc": self.finished_at_utc,
            "delivery_profile": self.delivery_profile,
            "prompt_should_change": self.prompt_should_change,
            "acceptance_status": self.acceptance_status,
        }


@dataclass(frozen=True, slots=True)
class SummaryEvidence:
    """Deeply immutable, normalized evidence accepted by the renderer."""

    cycle_id: str
    evidence_identity: Mapping[str, str]
    prompt_name: str
    prompt_path: str
    model_name: str
    case_counts: Mapping[str, int]
    repeats: Mapping[str, int]
    planned_calls: Mapping[str, int]
    completed_calls: Mapping[str, int]
    metrics: Mapping[str, object]
    comparisons: Mapping[str, object]
    coverage: Mapping[str, object]
    smoke_passed: bool
    failure_summaries: tuple[Mapping[str, object], ...]

    def __post_init__(self) -> None:
        cycle_id = _require_nonempty_text(self.cycle_id, label="cycle_id")
        identity = _mapping(self.evidence_identity, label="evidence_identity")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in identity.items()):
            raise SummaryError("evidence_identity must map strings to strings")
        if identity.get("cycle_id") != cycle_id:
            raise SummaryError("evidence_identity.cycle_id does not match cycle_id")
        object.__setattr__(self, "cycle_id", cycle_id)
        object.__setattr__(self, "evidence_identity", identity)
        object.__setattr__(self, "prompt_name", _require_nonempty_text(self.prompt_name, label="prompt_name"))
        object.__setattr__(self, "prompt_path", _validate_relative_prompt_path(self.prompt_path))
        object.__setattr__(self, "model_name", _require_nonempty_text(self.model_name, label="model_name"))
        object.__setattr__(self, "case_counts", _phase_mapping(self.case_counts, label="case_counts"))
        object.__setattr__(self, "repeats", _phase_mapping(self.repeats, label="repeats"))
        object.__setattr__(self, "planned_calls", _phase_mapping(self.planned_calls, label="planned_calls"))
        object.__setattr__(self, "completed_calls", _phase_mapping(self.completed_calls, label="completed_calls"))
        metrics = _mapping(self.metrics, label="metrics")
        comparisons = _mapping(self.comparisons, label="comparisons")
        coverage = _mapping(self.coverage, label="coverage")
        object.__setattr__(self, "metrics", metrics)
        object.__setattr__(self, "comparisons", comparisons)
        object.__setattr__(self, "coverage", coverage)
        if type(self.smoke_passed) is not bool:
            raise SummaryError("smoke_passed must be boolean")
        if not isinstance(self.failure_summaries, Sequence) or isinstance(self.failure_summaries, (str, bytes, bytearray)):
            raise SummaryError("failure_summaries must be a sequence of mappings")
        frozen_failures: list[Mapping[str, object]] = []
        for index, failure in enumerate(self.failure_summaries):
            frozen = _mapping(failure, label=f"failure_summaries[{index}]", require_nonempty=False)
            frozen_failures.append(frozen)
        object.__setattr__(self, "failure_summaries", tuple(frozen_failures))
        # Detect contradictory records at the evidence boundary, before any
        # renderer can accidentally choose one of them.
        _acceptance_records(comparisons, metrics)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> "SummaryEvidence":
        if not isinstance(value, Mapping):
            raise SummaryError("summary evidence must be a mapping")
        names = {field.name for field in fields(cls)}
        unknown = set(value) - names
        missing = names - set(value)
        if unknown:
            raise SummaryError("summary evidence contains unknown field(s)")
        if missing:
            raise SummaryError("summary evidence is missing required field(s)")
        return cls(**{name: value[name] for name in names})  # type: ignore[arg-type]


def normalize_formal_result(
    kind: str,
    *,
    finished_at_utc: str,
    stop_reason: str | None = None,
) -> FormalResult:
    """Normalize a scored terminal kind using the exact seven-result matrix."""

    if kind not in FORMAL_KINDS:
        raise SummaryError(f"unknown formal result kind: {kind!r}")
    profile, prompt_change, acceptance = _RESULT_MATRIX[kind]
    reason = kind if stop_reason is None else stop_reason
    return FormalResult(
        kind=kind,
        stop_reason=reason,
        finished_at_utc=finished_at_utc,
        delivery_profile=profile,
        prompt_should_change=prompt_change,
        acceptance_status=acceptance,
    )


def escape_markdown(value: object) -> str:
    """Escape user-controlled text for one-line Markdown rendering."""

    text = _safe_text(str(value))
    return "".join(("\\" + char) if char in _MARKDOWN_META else char for char in text)


def _display_value(value: object) -> str:
    if value is None:
        return "—"
    if type(value) is bool:
        return "是" if value else "否"
    if isinstance(value, Mapping):
        parts = [
            f"{escape_markdown(key)}={_display_value(value[key])}"
            for key in sorted(value, key=lambda item: str(item))
        ]
        return "; ".join(parts) if parts else "—"
    if isinstance(value, (tuple, list)):
        return ", ".join(_display_value(item) for item in value) if value else "—"
    return escape_markdown(value)


def _phase_value(values: Mapping[str, object], phase: str) -> object | None:
    if phase in values:
        return values[phase]
    for alias, canonical in _PHASE_ALIASES.items():
        if canonical == phase and alias in values:
            return values[alias]
    return None


def _phase_label(phase: str) -> str:
    return {"dev": "dev", "validation": "validation", "acceptance": "acceptance"}[phase]


def _require_phase_evidence(evidence: SummaryEvidence, result: FormalResult) -> None:
    for phase in ("dev", "validation"):
        for label, values in (
            ("case_counts", evidence.case_counts),
            ("repeats", evidence.repeats),
            ("planned_calls", evidence.planned_calls),
            ("completed_calls", evidence.completed_calls),
        ):
            if phase not in values:
                raise SummaryError(f"missing {label}.{phase} evidence")
        if _phase_value(evidence.metrics, phase) is None:
            raise SummaryError(f"missing metrics.{phase} evidence")
        if _phase_value(evidence.comparisons, phase) is None:
            raise SummaryError(f"missing comparisons.{phase} evidence")
    if result.acceptance_status != "not_run":
        for label, values in (
            ("case_counts", evidence.case_counts),
            ("repeats", evidence.repeats),
            ("planned_calls", evidence.planned_calls),
            ("completed_calls", evidence.completed_calls),
        ):
            if "acceptance" not in values:
                raise SummaryError(f"missing {label}.acceptance evidence")
        if _phase_value(evidence.metrics, "acceptance") is None:
            raise SummaryError("missing metrics.acceptance evidence")
        if _phase_value(evidence.comparisons, "acceptance") is None:
            raise SummaryError("missing comparisons.acceptance evidence")


def _render_target(evidence: SummaryEvidence) -> list[str]:
    mode = evidence.evidence_identity.get("mode", "tune")
    return [
        f"- Prompt 名称：{escape_markdown(evidence.prompt_name)}",
        f"- Prompt 路径：{escape_markdown(evidence.prompt_path)}",
        f"- 运行模式：{escape_markdown(mode)}",
        f"- 固定模型：{escape_markdown(evidence.model_name)}",
        f"- 周期：{escape_markdown(evidence.cycle_id)}",
    ]


def _render_conclusion(result: FormalResult) -> list[str]:
    return [
        # ``kind`` is a closed, normalized machine value rather than
        # caller-controlled prose; retain its exact spelling for operators and
        # stable filename/result correlation.
        f"- 最终结果：{result.kind}",
        f"- 停止原因：{escape_markdown(result.stop_reason)}",
        f"- 生产 Prompt 是否需要修改：{'是' if result.prompt_should_change else '否'}",
        f"- 候选是否达到交付门禁：{'是' if result.acceptance_status == 'passed' else '否'}",
    ]


def _render_effort(result: FormalResult, evidence: SummaryEvidence) -> list[str]:
    lines: list[str] = []
    for phase in _PHASES:
        if phase == "acceptance" and result.acceptance_status == "not_run":
            continue
        count = evidence.case_counts.get(phase)
        repeat = evidence.repeats.get(phase)
        planned = evidence.planned_calls.get(phase)
        completed = evidence.completed_calls.get(phase)
        if None in {count, repeat, planned, completed}:
            raise SummaryError(f"incomplete {phase} test-effort evidence")
        lines.append(
            f"- {_phase_label(phase)}：案例 {count}；重复 {repeat}；计划调用 {planned}；完成调用 {completed}"
        )
    lines.append(f"- adapter smoke：{'通过' if evidence.smoke_passed else '失败'}")
    if result.acceptance_status == "not_run":
        reason = _acceptance_reason(evidence)
        if not reason:
            raise SummaryError("acceptance not_run evidence requires a rule explanation")
        lines.append(f"- acceptance：not_run；未按规则运行；原因：{escape_markdown(reason)}")
    else:
        label = "通过" if result.acceptance_status == "passed" else "失败"
        lines.append(f"- acceptance：{result.acceptance_status}；已按规则运行；结果：{label}")
    return lines


def _render_mapping_lines(mapping: Mapping[str, object], *, prefix: str = "") -> list[str]:
    lines: list[str] = []
    for key in sorted(mapping, key=lambda item: str(item)):
        value = mapping[key]
        lines.append(f"- {prefix}{escape_markdown(key)}：{_display_value(value)}")
    return lines


def _render_coverage(evidence: SummaryEvidence) -> list[str]:
    lines = _render_mapping_lines(evidence.coverage)
    if not lines:
        raise SummaryError("coverage evidence is empty")
    return lines


def _render_metrics(result: FormalResult, evidence: SummaryEvidence) -> list[str]:
    lines: list[str] = []
    for phase in _PHASES:
        if phase == "acceptance" and result.acceptance_status == "not_run":
            continue
        metrics = _phase_value(evidence.metrics, phase)
        if metrics is None:
            continue
        if not isinstance(metrics, Mapping):
            raise SummaryError(f"metrics.{phase} must be a mapping")
        passed = metrics.get("pass", metrics.get("pass_count"))
        completed = evidence.completed_calls.get(phase)
        if passed is not None and completed is not None:
            prefix = f"{escape_markdown(phase)}: {escape_markdown(passed)}/{escape_markdown(completed)}"
        else:
            prefix = escape_markdown(phase)
        details = [
            f"{escape_markdown(key)}={_display_value(metrics[key])}"
            for key in sorted(metrics, key=lambda item: str(item))
        ]
        lines.append(f"- {prefix}" + (f"；{'；'.join(details)}" if details else ""))
    for phase in _PHASES:
        if phase == "acceptance" and result.acceptance_status == "not_run":
            continue
        comparison = _phase_value(evidence.comparisons, phase)
        if comparison is None:
            continue
        lines.append(f"- 比较 {_phase_label(phase)}：{_display_value(comparison)}")
    if not lines:
        raise SummaryError("metrics and comparisons evidence is empty")
    return lines


def _failure_key(value: Mapping[str, object]) -> tuple[str, str]:
    for key in ("failure_id", "case_id", "id", "case"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return (_safe_text(candidate), _display_value(value))
    # The value was already normalized and immutable, so this repr is stable
    # for a fixed mapping after keys have been sorted.
    return ("", _display_value(value))


def _render_failures(evidence: SummaryEvidence) -> list[str]:
    if not evidence.failure_summaries:
        return ["- 无可行动失败摘要。"]
    lines: list[str] = []
    for failure in sorted(evidence.failure_summaries, key=_failure_key):
        lines.append(f"- {_display_value(failure)}")
    return lines


def _render_prompt_result(result: FormalResult) -> list[str]:
    if result.kind == "no_change_needed":
        return ["- Prompt 保持原样；仅计划交付可交付评测目录。"]
    if result.kind == "acceptance_passed":
        return ["- 候选通过全部适用门禁；冻结 Prompt 等待交付决定。"]
    return ["- 候选被拒绝；Prompt 保持原样，仅计划交付可交付评测目录。"]


def render_summary(result: FormalResult, evidence: SummaryEvidence) -> str:
    """Render one deterministic seven-section Markdown summary."""

    if not isinstance(result, FormalResult):
        raise SummaryError("result must be a FormalResult")
    if not isinstance(evidence, SummaryEvidence):
        raise SummaryError("evidence must be SummaryEvidence")
    _require_phase_evidence(evidence, result)
    recorded_acceptance = _acceptance_status(evidence)
    if recorded_acceptance != result.acceptance_status:
        raise SummaryError("acceptance evidence is inconsistent with result acceptance_status")

    sections = (
        ("评测对象", _render_target(evidence)),
        ("结论", _render_conclusion(result)),
        ("测试力度", _render_effort(result, evidence)),
        ("覆盖面", _render_coverage(evidence)),
        ("结果指标", _render_metrics(result, evidence)),
        ("失败摘要", _render_failures(evidence)),
        ("Prompt 结果", _render_prompt_result(result)),
    )
    lines: list[str] = []
    for index, (heading, content) in enumerate(sections):
        if index:
            lines.append("")
        lines.append(f"## {heading}")
        lines.extend(content)
    return "\n".join(lines).replace("\r\n", "\n").replace("\r", "\n") + "\n"


def write_summary(eval_root: Path, result: FormalResult, evidence: SummaryEvidence) -> Path:
    """Write a summary at its identity-derived path without overwriting."""

    if not isinstance(eval_root, Path):
        raise TypeError("eval_root must be a Path")
    # render before creating anything so invalid evidence cannot leave a
    # seemingly valid evaluation-summaries directory behind.
    content = render_summary(result, evidence)
    timestamp = result.finished_at_utc[:10] + "-" + result.finished_at_utc[11:19].replace(":", "")
    destination = eval_root / "evaluation-summaries" / f"{timestamp}-{result.kind}.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError:
        raise FileExistsError(f"summary destination already exists: {destination}") from None
    return destination


__all__ = [
    "FORMAL_KINDS",
    "FormalResult",
    "SummaryEvidence",
    "SummaryError",
    "escape_markdown",
    "normalize_formal_result",
    "render_summary",
    "write_summary",
]
