"""Load and validate the frozen prompt-evaluation case datasets.

Case files are evaluation assets rather than model output.  They are therefore
validated against the same production Pydantic model used by the runner before
any model call is made.  The loader also owns the cross-split invariants that
keep validation and acceptance data from leaking into development data.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
import hashlib
import importlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Literal, TypeVar
import unicodedata

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_core import to_jsonable_python


class CaseSetupError(ValueError):
    """Raised when case files cannot form a valid evaluation dataset."""

    def __init__(
        self, message: str, *, details: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.details = dict(details or {})


FIXED_CATEGORIES = frozenset(
    {
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
    }
)
FIXED_VARIANTS = frozenset(
    {
        "normal",
        "boundary",
        "conflict",
        "ambiguity",
        "irrelevant",
        "fallback",
        "regression",
        "adversarial",
        "natural_variation",
    }
)
MIN_CASES_PER_SPLIT = 30
_SPLITS = ("dev", "validation", "acceptance")

# This alias intentionally keeps the split and variant values open at the
# model boundary.  The model validators below enforce the fixed variant set so
# validation errors can identify the offending field and value.
RequiredSplits = dict[
    Literal["dev", "validation", "acceptance"], list[str]
]


class _FrozenList(list[Any]):
    """A list-compatible container that rejects every in-place mutation."""

    __slots__ = ()

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("coverage obligation values are immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


class _FrozenDict(dict[Any, Any]):
    """A dict-compatible container that rejects every in-place mutation."""

    __slots__ = ()

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("coverage obligation values are immutable")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


class CoverageCategory(BaseModel):
    """A fixed coverage category and its evidence-backed applicability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    applicability: Literal["required", "not_applicable"]
    evidence_checked: list[str] = Field(default_factory=list)
    rationale: str | None = None

    @field_validator("category")
    @classmethod
    def _require_fixed_category(cls, value: str) -> str:
        if value not in FIXED_CATEGORIES:
            raise ValueError(f"category is unknown: {value!r}")
        return value

    @field_validator("evidence_checked")
    @classmethod
    def _require_evidence_entries(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("evidence_checked entries must not be blank")
        return value

    @field_validator("rationale")
    @classmethod
    def _require_non_blank_rationale(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("rationale must not be blank")
        return value

    @model_validator(mode="after")
    def _require_not_applicable_evidence(self) -> "CoverageCategory":
        if self.applicability == "not_applicable":
            if not self.evidence_checked:
                raise ValueError(
                    "not_applicable category requires non-empty evidence_checked"
                )
            if self.rationale is None or not self.rationale.strip():
                raise ValueError(
                    "not_applicable category requires non-empty rationale"
                )
        return self

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> "CoverageCategory":
        object.__setattr__(self, "evidence_checked", _FrozenList(self.evidence_checked))
        return self


class CoverageObligation(BaseModel):
    """One evidence-backed business rule and its split/variant obligations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    source: list[str]
    category: str
    risk: Literal["normal", "critical"]
    rule: str
    required_splits: RequiredSplits
    variant_exclusions: dict[str, str] = Field(default_factory=dict)

    @field_validator("id", "rule")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("source")
    @classmethod
    def _require_sources(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("source must contain at least one repository path")
        if any(not item.strip() for item in value):
            raise ValueError("source entries must not be blank")
        return value

    @field_validator("category")
    @classmethod
    def _require_fixed_category(cls, value: str) -> str:
        if value not in FIXED_CATEGORIES:
            raise ValueError(f"category is unknown: {value!r}")
        return value

    @field_validator("required_splits")
    @classmethod
    def _validate_required_splits(cls, value: RequiredSplits) -> RequiredSplits:
        if not value:
            raise ValueError("required_splits must contain at least one split")
        unknown_splits = set(value) - set(_SPLITS)
        if unknown_splits:
            unknown = ", ".join(sorted(map(str, unknown_splits)))
            raise ValueError(f"required_splits contains unknown split(s): {unknown}")
        for split, variants in value.items():
            if not variants:
                raise ValueError(
                    f"required_splits.{split} must contain at least one variant"
                )
            if len(variants) != len(set(variants)):
                raise ValueError(
                    f"required_splits.{split} variants must not contain duplicates"
                )
            unknown_variants = set(variants) - FIXED_VARIANTS
            if unknown_variants:
                unknown = ", ".join(sorted(map(str, unknown_variants)))
                raise ValueError(
                    f"required_splits.{split} contains unknown variant(s): {unknown}"
                )
        return value

    @field_validator("variant_exclusions")
    @classmethod
    def _validate_variant_exclusions(
        cls, value: dict[str, str]
    ) -> dict[str, str]:
        unknown_variants = set(value) - FIXED_VARIANTS
        if unknown_variants:
            unknown = ", ".join(sorted(map(str, unknown_variants)))
            raise ValueError(
                f"variant_exclusions contains unknown variant(s): {unknown}"
            )
        blank_reasons = [variant for variant, reason in value.items() if not reason.strip()]
        if blank_reasons:
            raise ValueError(
                "variant_exclusions reasons must not be blank for: "
                + ", ".join(sorted(blank_reasons))
            )
        return value

    @model_validator(mode="after")
    def _validate_variant_coverage(self) -> "CoverageObligation":
        declared = {
            variant
            for variants in self.required_splits.values()
            for variant in variants
        }
        excluded = set(self.variant_exclusions)
        declared_and_excluded = declared & excluded
        if declared_and_excluded:
            values = ", ".join(sorted(declared_and_excluded))
            raise ValueError(
                "variant_exclusions may only explain undeclared variants: " + values
            )

        if self.risk == "critical":
            if "normal" not in declared:
                raise ValueError(
                    "critical obligation required_splits must include normal variant"
                )
            critical_variants = {"boundary", "conflict", "adversarial"}
            if not declared & critical_variants:
                raise ValueError(
                    "critical obligation required_splits must include one of "
                    "boundary, conflict, or adversarial variants"
                )
            missing_exclusions = critical_variants - declared - excluded
            if missing_exclusions:
                values = ", ".join(sorted(missing_exclusions))
                raise ValueError(
                    "critical obligation variant_exclusions must explain "
                    f"undeclared variants: {values}"
                )
        return self

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> "CoverageObligation":
        object.__setattr__(self, "source", _FrozenList(self.source))
        object.__setattr__(
            self,
            "required_splits",
            _FrozenDict(
                {
                    split: _FrozenList(variants)
                    for split, variants in self.required_splits.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "variant_exclusions",
            _FrozenDict(self.variant_exclusions),
        )
        return self


class CoverageObligations(BaseModel):
    """The complete frozen coverage-obligation asset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    categories: list[CoverageCategory]
    obligations: list[CoverageObligation]

    @model_validator(mode="after")
    def _validate_complete_asset(self) -> "CoverageObligations":
        category_names = [category.category for category in self.categories]
        duplicate_categories = {
            category
            for category in category_names
            if category_names.count(category) > 1
        }
        if duplicate_categories:
            values = ", ".join(sorted(duplicate_categories))
            raise ValueError(f"duplicate category declaration(s): {values}")

        missing_categories = FIXED_CATEGORIES - set(category_names)
        if missing_categories:
            values = ", ".join(sorted(missing_categories))
            raise ValueError(f"missing category declaration(s): {values}")

        category_by_name = {
            category.category: category for category in self.categories
        }
        for obligation in self.obligations:
            category = category_by_name[obligation.category]
            if category.applicability != "required":
                raise ValueError(
                    "obligation category must be required, but "
                    f"{obligation.category!r} is not_applicable"
                )

        obligation_ids = [obligation.id for obligation in self.obligations]
        duplicate_ids = {
            obligation_id
            for obligation_id in obligation_ids
            if obligation_ids.count(obligation_id) > 1
        }
        if duplicate_ids:
            values = ", ".join(sorted(duplicate_ids))
            raise ValueError(f"duplicate obligation id(s): {values}")
        return self

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> "CoverageObligations":
        object.__setattr__(self, "categories", _FrozenList(self.categories))
        object.__setattr__(self, "obligations", _FrozenList(self.obligations))
        return self


class CaseCoverage(BaseModel):
    """Coverage identity and reporting references for one evaluation case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_obligation: str
    secondary_obligations: list[str] = Field(default_factory=list)
    variant: str
    condition_id: str
    distinction: str | None = None

    @field_validator("primary_obligation", "variant", "condition_id")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("secondary_obligations")
    @classmethod
    def _require_non_blank_secondary_obligations(
        cls, value: list[str]
    ) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("secondary_obligations entries must not be blank")
        return value

    @field_validator("variant")
    @classmethod
    def _require_fixed_variant(cls, value: str) -> str:
        if value not in FIXED_VARIANTS:
            raise ValueError(f"variant is unknown: {value!r}")
        return value

    @field_validator("condition_id")
    @classmethod
    def _require_slug_condition_id(cls, value: str) -> str:
        if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) is None:
            raise ValueError(
                "condition_id must be a lowercase stable slug containing only "
                "letters, digits, and single hyphen separators"
            )
        return value

    @model_validator(mode="after")
    def _validate_obligation_references(self) -> "CaseCoverage":
        primary = _normalized_family(self.primary_obligation)
        secondary = [_normalized_family(item) for item in self.secondary_obligations]
        if not primary:
            raise ValueError("primary_obligation must contain a letter or digit")
        if any(not item for item in secondary):
            raise ValueError(
                "secondary_obligations entries must contain a letter or digit"
            )
        if len(secondary) != len(set(secondary)):
            raise ValueError("secondary_obligations must not contain duplicates")
        if primary in secondary:
            raise ValueError(
                "primary_obligation must not be repeated in secondary_obligations"
            )
        return self

    @model_validator(mode="after")
    def _freeze_nested_values(self) -> "CaseCoverage":
        object.__setattr__(
            self,
            "secondary_obligations",
            _FrozenList(self.secondary_obligations),
        )
        return self


class EvalCase(BaseModel):
    """One complete, human-authored evaluation case."""

    model_config = ConfigDict(extra="forbid")

    id: str
    semantic_family: str
    source: list[str]
    input: dict[str, dict[str, object]]
    expect: dict[str, dict[str, object]]
    priority: Literal["normal", "critical"] = "normal"
    dimensions: list[str]
    rationale: str
    coverage: CaseCoverage

    @field_validator("id", "semantic_family", "rationale")
    @classmethod
    def _require_non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("source", "dimensions")
    @classmethod
    def _require_string_lists(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("entries must not be blank")
        return value


@dataclass(frozen=True)
class ValidatedCase:
    """A parsed case and its validated production expected object."""

    case: EvalCase
    expected: BaseModel


def _freeze_audit_value(value: object) -> object:
    """Recursively freeze mappings and sequences stored in a coverage audit."""

    if isinstance(value, Mapping):
        return _FrozenDict(
            {key: _freeze_audit_value(item) for key, item in value.items()}
        )
    if isinstance(value, tuple):
        return tuple(_freeze_audit_value(item) for item in value)
    if isinstance(value, list):
        return _FrozenList([_freeze_audit_value(item) for item in value])
    if isinstance(value, set):
        return frozenset(_freeze_audit_value(item) for item in value)
    return value


@dataclass(frozen=True)
class CoverageAudit:
    """Deterministic, mechanical coverage evidence for a complete case suite.

    This audit intentionally contains only checks the validator can establish
    from the frozen obligations and case data.  Human evidence scanning and
    saturation confirmation happen later in the workflow and are not part of
    this object.
    """

    counts: Mapping[str, Mapping[str, int]]
    distributions: Mapping[str, object]
    missing_quotas: tuple[tuple[str, str, str], ...]
    hard_duplicates: tuple[Mapping[str, object], ...]
    near_duplicates: tuple[Mapping[str, object], ...]
    categories: tuple[Mapping[str, object], ...]
    coverage_matrix: Mapping[str, object]
    mechanical_gates: Mapping[str, bool]

    def __post_init__(self) -> None:
        for field_name in (
            "counts",
            "distributions",
            "missing_quotas",
            "hard_duplicates",
            "near_duplicates",
            "categories",
            "coverage_matrix",
            "mechanical_gates",
        ):
            object.__setattr__(
                self,
                field_name,
                _freeze_audit_value(getattr(self, field_name)),
            )


@dataclass(frozen=True)
class CaseSuite:
    """The three evaluation splits in their runner-facing form."""

    dev: tuple[ValidatedCase, ...]
    validation: tuple[ValidatedCase, ...]
    acceptance: tuple[ValidatedCase, ...]
    coverage_audit: CoverageAudit

    @property
    def development(self) -> tuple[ValidatedCase, ...]:
        """Alias used by callers that spell the development split in full."""

        return self.dev

    @property
    def all_cases(self) -> tuple[ValidatedCase, ...]:
        """Return all cases in deterministic split order."""

        return self.dev + self.validation + self.acceptance

    @property
    def splits(self) -> dict[str, tuple[ValidatedCase, ...]]:
        """Return a fresh mapping of split name to validated cases."""

        return {
            "dev": self.dev,
            "validation": self.validation,
            "acceptance": self.acceptance,
        }

    def __getitem__(self, split: str) -> tuple[ValidatedCase, ...]:
        """Allow small runner integrations to address a split by name."""

        try:
            return self.splits[split]
        except KeyError as exc:
            raise KeyError(f"unknown case split: {split}") from exc


_SPLIT_ALIASES = {
    "dev": "dev",
    "development": "dev",
    "validation": "validation",
    "acceptance": "acceptance",
}
_SchemaT = TypeVar("_SchemaT", bound=BaseModel)


def _split_paths(paths: object) -> dict[str, Path]:
    """Normalize the supported three-path forms into canonical split names."""

    if isinstance(paths, Mapping):
        result: dict[str, Path] = {}
        for raw_name, raw_path in paths.items():
            if not isinstance(raw_name, str):
                raise CaseSetupError("case split names must be strings")
            name = _SPLIT_ALIASES.get(raw_name.strip().casefold())
            if name is None:
                raise CaseSetupError(
                    f"unknown case split {raw_name!r}; expected dev, validation, acceptance"
                )
            if name in result:
                raise CaseSetupError(f"case split supplied more than once: {name}")
            result[name] = Path(raw_path)
    elif isinstance(paths, Set):
        raise CaseSetupError(
            "load_case_suite paths must be an ordered iterable; unordered sets "
            "cannot determine dev, validation, acceptance"
        )
    elif isinstance(paths, Iterable) and not isinstance(
        paths, (str, bytes, bytearray)
    ):
        values = tuple(paths)
        if len(values) != 3:
            raise CaseSetupError(
                "load_case_suite requires exactly three paths: dev, validation, acceptance"
            )
        result = dict(zip(_SPLITS, (Path(path) for path in values), strict=True))
    else:
        raise CaseSetupError(
            "load_case_suite paths must be a three-item sequence or split mapping"
        )

    missing = [split for split in _SPLITS if split not in result]
    if missing:
        raise CaseSetupError(f"missing case split path(s): {', '.join(missing)}")
    return {split: result[split] for split in _SPLITS}


def _read_cases(path: Path, split: str) -> list[object]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CaseSetupError(f"unable to read {split} case file {path}: {exc}") from exc

    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise CaseSetupError(f"invalid YAML in {split} case file {path}: {exc}") from exc

    if isinstance(document, list):
        return document
    if isinstance(document, dict) and set(document) == {"cases"}:
        cases = document["cases"]
        if isinstance(cases, list):
            return cases
    raise CaseSetupError(
        f"{split} case file {path} must contain a YAML list of cases"
    )


def _format_validation_error(error: Exception) -> str:
    """Keep Pydantic's useful field path in a concise setup-error message."""

    text = str(error).replace("\n", "; ")
    return re.sub(r"\s+", " ", text).strip()


def coverage_obligations_hash(path: Path) -> str:
    """Hash the exact bytes of a coverage-obligation file."""

    try:
        content = Path(path).read_bytes()
    except (OSError, TypeError, ValueError) as error:
        raise CaseSetupError(
            f"unable to hash coverage obligations: {error}"
        ) from error
    return hashlib.sha256(content).hexdigest()


def load_coverage_obligations(path: Path, repo_root: Path) -> CoverageObligations:
    """Load and validate a coverage-obligation asset and its evidence paths."""

    try:
        source_path = Path(path)
        content = source_path.read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError, UnicodeError) as error:
        raise CaseSetupError(
            f"unable to read coverage obligations {path}: {error}"
        ) from error

    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as error:
        raise CaseSetupError(
            f"invalid YAML in coverage obligations {path}: {error}"
        ) from error

    try:
        value = CoverageObligations.model_validate(raw)
    except Exception as error:
        raise CaseSetupError(_format_validation_error(error)) from error

    try:
        root = Path(repo_root).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise CaseSetupError(f"repository root is invalid: {error}") from error

    for obligation in value.obligations:
        for source in obligation.source:
            try:
                candidate = (root / source).resolve(strict=False)
                contained = candidate.is_relative_to(root)
                exists = candidate.is_file()
            except (OSError, RuntimeError, TypeError, ValueError):
                contained = False
                exists = False
            if not contained or not exists:
                raise CaseSetupError(f"unknown obligation source: {source}")
    return value


def _normalized_family(value: str) -> str:
    """Normalize harmless spelling/punctuation differences for leakage checks."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Treat separators, punctuation, and whitespace as equivalent while
    # retaining Unicode letters/digits in non-English family names.
    return "".join(character for character in normalized if character.isalnum())


def _scenario_key(case: EvalCase) -> tuple[str, str, str]:
    """Return the normalized global identity for an evaluation scenario."""

    coverage = case.coverage
    return (
        _normalized_family(coverage.primary_obligation),
        _normalized_family(coverage.variant),
        _normalized_family(coverage.condition_id),
    )


def _normalize_input_strings(value: object) -> object:
    """Recursively normalize only string content for input leak detection."""

    if isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value).strip()
        return " ".join(normalized.split()).casefold()
    if isinstance(value, Mapping):
        return {
            _normalize_input_strings(key): _normalize_input_strings(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_input_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_normalize_input_strings(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return {
            _normalize_input_strings(item)
            for item in value
        }
    return value


def _canonical_sort_key(value: object) -> str:
    """Return a stable JSON key for an already canonical value."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_type_key(value: object) -> tuple[str, str]:
    """Return deterministic type metadata for an unordered member tie-break."""

    value_type = type(value)
    return value_type.__module__, value_type.__qualname__


def _canonical_tagged(value: object) -> object:
    """Return a deterministic, recursively typed representation of ``value``.

    JSON serialization intentionally erases distinctions such as ``bytes`` vs
    ``str`` and ``Path`` vs ``str``.  The tag is used only for deterministic
    ordering/tie-breaking; the JSON-compatible value remains authoritative in
    the emitted canonical representation.
    """

    type_module, type_name = _canonical_type_key(value)
    if isinstance(value, Mapping):
        members = [
            (_canonical_tagged(key), _canonical_tagged(item))
            for key, item in value.items()
        ]
        members.sort(
            key=lambda pair: (
                _canonical_sort_key(pair[0]),
                _canonical_sort_key(pair[1]),
            )
        )
        body: object = {
            "kind": "mapping",
            "items": [[key, item] for key, item in members],
        }
    elif isinstance(value, (set, frozenset)):
        members = sorted(
            (_canonical_tagged(item) for item in value),
            key=_canonical_sort_key,
        )
        body = {"kind": "unordered", "items": members}
    elif isinstance(value, (list, tuple)):
        body = {
            "kind": "ordered",
            "items": [_canonical_tagged(item) for item in value],
        }
    else:
        body = {"kind": "scalar", "value": _canonicalize_json(value)}
    return {"type": [type_module, type_name], "value": body}


def _canonical_tagged_sort_key(value: object) -> str:
    """Return a total deterministic sort key retaining raw type/shape tags."""

    return _canonical_sort_key(_canonical_tagged(value))


def _canonicalize_unordered(
    raw: object,
    encoded: object | None = None,
    *,
    paired_items: Sequence[tuple[object, object]] | None = None,
) -> object:
    """Canonicalize an unordered container without losing raw/JSON pairing.

    A Pydantic JSON dump turns sets into lists and may independently iterate
    those sets.  When the model context can provide singleton serializations,
    ``paired_items`` carries an explicit raw-element to JSON-element mapping.
    Otherwise we match equivalent canonical shapes and use a deterministic raw
    shape template for nested unordered values.  No independently unordered
    raw/JSON lists are zipped together.
    """

    raw_items = tuple(raw)  # type: ignore[arg-type]

    if paired_items is not None and len(paired_items) == len(raw_items):
        decorated = [
            (
                _canonicalize_with_json_hint(raw_item, encoded_item),
                _canonical_tagged(raw_item),
            )
            for raw_item, encoded_item in paired_items
        ]
        decorated.sort(
            key=lambda item: (
                _canonical_sort_key(item[0]),
                _canonical_sort_key(item[1]),
            )
        )
        return [item for item, _raw_tag in decorated]

    if encoded is None:
        decorated = [
            (_canonicalize_json(item), _canonical_tagged(item))
            for item in raw_items
        ]
        decorated.sort(
            key=lambda item: (
                _canonical_sort_key(item[0]),
                _canonical_sort_key(item[1]),
            )
        )
        return [item for item, _raw_tag in decorated]

    if not isinstance(encoded, (list, tuple)):
        # A field serializer may intentionally replace the set with another
        # JSON value.  Preserve that value rather than manufacturing a list.
        return _canonicalize_json(encoded)

    encoded_items = list(encoded)

    # First recover associations from the canonical raw shape.  The candidate
    # is recursively normalized with the actual JSON value, so nested sets are
    # handled before the match is compared.
    if len(raw_items) == len(encoded_items):
        remaining = list(range(len(encoded_items)))
        matched: list[tuple[object, object]] = []
        for raw_item in sorted(raw_items, key=_canonical_tagged_sort_key):
            raw_shape = _canonicalize_json(raw_item)
            candidates = [
                (
                    index,
                    _canonicalize_with_json_hint(raw_item, encoded_items[index]),
                )
                for index in remaining
            ]
            equivalent = [
                (index, candidate)
                for index, candidate in candidates
                if _canonical_sort_key(candidate) == _canonical_sort_key(raw_shape)
            ]
            if not equivalent:
                matched = []
                break
            # Equal candidates have equal emitted values; choosing by the
            # candidate key keeps the association independent of set order.
            index, candidate = min(
                equivalent, key=lambda item: _canonical_sort_key(item[1])
            )
            remaining.remove(index)
            matched.append((candidate, _canonical_tagged(raw_item)))
        if len(matched) == len(raw_items) and not remaining:
            matched.sort(
                key=lambda item: (
                    _canonical_sort_key(item[0]),
                    _canonical_sort_key(item[1]),
                )
            )
            return [item for item, _raw_tag in matched]

    # Scalar serializers can make a raw value's standalone JSON form differ
    # from the model's configured form (for example base64 bytes).  In that
    # case the encoded values are authoritative.  Apply one deterministic raw
    # shape template to each encoded member so nested set/frozenset structure
    # is still normalized, then sort by the resulting JSON value and tag.
    templates = sorted(raw_items, key=_canonical_tagged_sort_key)
    if not templates:
        canonical_encoded = [(_canonicalize_json(item), None) for item in encoded_items]
    else:
        template = templates[0]
        canonical_encoded = [
            (
                _canonicalize_with_json_hint(template, item),
                _canonical_tagged(template),
            )
            for item in encoded_items
        ]
    canonical_encoded.sort(
        key=lambda item: (
            _canonical_sort_key(item[0]),
            _canonical_sort_key(item[1]) if item[1] is not None else "",
        )
    )
    return [item for item, _raw_tag in canonical_encoded]


_MISSING = object()


def _singleton_json_elements(
    model: BaseModel,
    field_name: str,
    raw: object,
    encoded: object,
) -> list[tuple[object, object]] | None:
    """Serialize one unordered member at a time using the production model."""

    if not isinstance(raw, (set, frozenset)):
        return None
    if not isinstance(encoded, (list, tuple)):
        return None

    pairs: list[tuple[object, object]] = []
    for raw_item in sorted(raw, key=_canonical_tagged_sort_key):
        singleton = (
            frozenset((raw_item,))
            if isinstance(raw, frozenset)
            else {raw_item}
        )
        try:
            probe = model.model_copy(update={field_name: singleton})
            probe_dump = probe.model_dump(mode="json")
        except Exception:
            return None
        if not isinstance(probe_dump, Mapping):
            return None
        probe_value = probe_dump.get(field_name, _MISSING)
        if not isinstance(probe_value, (list, tuple)) or len(probe_value) != 1:
            return None
        pairs.append((raw_item, probe_value[0]))
    return pairs


def _canonicalize_with_json_hint(
    raw: object,
    encoded: object,
    *,
    paired_items: Sequence[tuple[object, object]] | None = None,
) -> object:
    """Canonicalize a Python-mode value while retaining its JSON-mode value.

    Pydantic's JSON mode knows how to encode values such as ``Decimal``,
    ``UUID``, ``bytes``, paths, and temporal values, but it also turns sets
    into lists before callers can impose a stable order.  Pairing the two
    representations lets us keep Pydantic's scalar encodings while retaining
    the raw container kind needed for deterministic set/frozenset handling.
    """

    if isinstance(raw, BaseModel):
        return _canonicalize_model(raw, encoded_hint=encoded)

    if isinstance(raw, Mapping):
        if not isinstance(encoded, Mapping):
            return _canonicalize_json(encoded)

        encoded_by_key = {
            _canonical_sort_key(_canonicalize_json(key)): (key, item)
            for key, item in encoded.items()
        }
        items: list[tuple[object, object]] = []
        encoded_items = list(encoded.items())
        for index, (raw_key, raw_item) in enumerate(raw.items()):
            encoded_key = _canonicalize_json(raw_key)
            encoded_pair = encoded_by_key.get(_canonical_sort_key(encoded_key))
            if encoded_pair is None:
                # A serializer may transform mapping keys in a way that is not
                # recoverable from the Python-mode value.  Pydantic preserves
                # mapping iteration order, so pair by position as a safe
                # fallback and still retain nested set ordering.
                if index >= len(encoded_items):
                    return _canonicalize_json(encoded)
                encoded_pair = encoded_items[index]
            encoded_key_value, encoded_item = encoded_pair
            items.append(
                (
                    _canonicalize_json(encoded_key_value),
                    _canonicalize_with_json_hint(raw_item, encoded_item),
                )
            )
        items.sort(key=lambda item: _canonical_sort_key(item[0]))
        return {key: item for key, item in items}

    if isinstance(raw, (set, frozenset)):
        return _canonicalize_unordered(raw, encoded, paired_items=paired_items)

    if isinstance(raw, (list, tuple)):
        if not isinstance(encoded, (list, tuple)) or len(raw) != len(encoded):
            return _canonicalize_json(encoded)
        return [
            _canonicalize_with_json_hint(raw_item, encoded_item)
            for raw_item, encoded_item in zip(raw, encoded, strict=True)
        ]

    # For scalar leaves, JSON mode is the source of truth.  It has already
    # applied field serializers and all Pydantic-supported JSON encodings.
    return _canonicalize_json(encoded)


def _canonicalize_model(
    model: BaseModel, *, encoded_hint: object = _MISSING
) -> object:
    """Canonicalize a model while retaining field-level JSON serializers."""

    raw_dump = model.model_dump(mode="python")
    encoded_dump = (
        model.model_dump(mode="json")
        if encoded_hint is _MISSING
        else encoded_hint
    )
    if not isinstance(raw_dump, Mapping) or not isinstance(encoded_dump, Mapping):
        return _canonicalize_json(encoded_dump)

    items: list[tuple[object, object]] = []
    model_fields = type(model).model_fields
    for key, raw_dump_item in raw_dump.items():
        encoded_item = encoded_dump.get(key, _MISSING)
        if encoded_item is _MISSING:
            canonical_item = _canonicalize_json(raw_dump_item)
        else:
            raw_item = (
                getattr(model, key, raw_dump_item)
                if key in model_fields
                else raw_dump_item
            )
            paired_items = _singleton_json_elements(
                model, key, raw_item, encoded_item
            )
            canonical_item = _canonicalize_with_json_hint(
                raw_item,
                encoded_item,
                paired_items=paired_items,
            )
        items.append((_canonicalize_json(key), canonical_item))

    # A custom model serializer may add JSON keys which do not appear in the
    # Python-mode dump.  Preserve those JSON-authoritative values as well.
    raw_keys = set(raw_dump)
    for key, encoded_item in encoded_dump.items():
        if key not in raw_keys:
            items.append((_canonicalize_json(key), _canonicalize_json(encoded_item)))

    items.sort(key=lambda item: _canonical_sort_key(item[0]))
    return {key: item for key, item in items}


def _canonicalize_json(value: object) -> object:
    """Recursively produce deterministic, JSON-compatible data.

    Mappings are ordered by their canonical key representation, ordered
    sequences retain their order, and unordered containers are converted to
    sorted arrays.  This is intentionally shared by input fingerprints,
    expected-object normalization, and dataset hashes.
    """

    if isinstance(value, BaseModel):
        # Keep the Python-mode tree so unordered containers remain visible,
        # while pairing it with JSON mode so scalar/model serializers retain
        # Pydantic's production JSON representation.
        return _canonicalize_model(value)
    if isinstance(value, Mapping):
        items = [
            (key, _canonicalize_json(item)) for key, item in value.items()
        ]
        items.sort(key=lambda item: _canonical_sort_key(item[0]))
        return {key: item for key, item in items}
    if isinstance(value, (set, frozenset)):
        return _canonicalize_unordered(value)
    if isinstance(value, (list, tuple)):
        return [_canonicalize_json(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # Pydantic's default JSON output represents NaN and infinities as null;
        # finite floats are already JSON scalars and must not be recursively
        # passed through a converter that returns a fresh float object.
        return value if math.isfinite(value) else None
    try:
        # This is Pydantic's same core conversion used by JSON serialization,
        # including Decimal, UUID, bytes, date/time, Path, timedelta, and
        # other supported scalar encodings.  Pydantic recursively converts any
        # container result before it reaches this function.
        encoded = to_jsonable_python(value, inf_nan_mode="null")
    except (TypeError, ValueError, OverflowError):
        return value
    if encoded is None or isinstance(encoded, (str, int, bool)):
        return encoded
    if isinstance(encoded, float):
        return encoded if math.isfinite(encoded) else None
    # Pydantic's conversion is recursive for container results.  Returning a
    # remaining unsupported value lets the outer JSON dump raise a normal
    # setup error instead of using identity as a recursion escape hatch.
    return encoded


def _canonical_json(value: object, *, label: str) -> str:
    """Serialize JSON-compatible values with deterministic unordered sets."""

    try:
        canonical = _canonicalize_json(value)
        return json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise CaseSetupError(f"{label} must contain JSON-compatible values: {exc}") from exc


def _validate_expected(
    case: EvalCase, schema: type[_SchemaT], *, split: str, index: int
) -> _SchemaT:
    if "output" not in case.expect:
        raise CaseSetupError(
            f"{split} case {case.id!r} expect must contain an output object"
        )

    output = case.expect["output"]
    if not isinstance(output, Mapping):
        # The EvalCase type normally catches this first; retain this check for
        # callers constructing EvalCase with non-standard validation settings.
        raise CaseSetupError(
            f"{split} case {case.id!r} expect.output must be an object"
        )

    try:
        # Let Pydantic own alias/validation_alias parsing and reject extras in
        # the exact same validation pass.  ``extra="forbid"`` is an override
        # for schemas whose production config would otherwise ignore extras.
        expected = schema.model_validate(output, extra="forbid")
    except Exception as exc:
        detail = _format_validation_error(exc)
        raise CaseSetupError(
            f"{split} case {case.id!r} has an invalid expect.output at item {index}: {detail}"
        ) from exc

    declared = set(schema.model_fields)
    missing = sorted(declared - set(expected.model_fields_set))
    if missing:
        raise CaseSetupError(
            f"{split} case {case.id!r} expect.output is missing schema field(s): "
            + ", ".join(missing)
        )
    return expected


def _parse_split(
    path: Path, split: str, schema: type[_SchemaT]
) -> tuple[ValidatedCase, ...]:
    parsed: list[ValidatedCase] = []
    for index, raw_case in enumerate(_read_cases(path, split)):
        try:
            case = EvalCase.model_validate(raw_case)
        except Exception as exc:
            detail = _format_validation_error(exc)
            raise CaseSetupError(
                f"{split} case at item {index} is invalid: {detail}"
            ) from exc
        expected = _validate_expected(case, schema, split=split, index=index)
        parsed.append(ValidatedCase(case=case, expected=expected))
    return tuple(parsed)


def _normalize_similarity_text(text: str) -> str:
    """Normalize user-authored text for deterministic similarity checks."""

    normalized = unicodedata.normalize("NFKC", text).casefold()
    normalized = "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith("P")
    )
    return " ".join(normalized.split())


def _mapping_path(path: str, key: object) -> str:
    """Append a mapping key to a stable JSON-like field path."""

    try:
        encoded = json.dumps(key, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        encoded = json.dumps(str(key), ensure_ascii=False)
    return f"{path}[{encoded}]"


def _flatten_input(
    value: object,
    *,
    path: str,
    strings: list[tuple[str, str]],
    scalars: list[tuple[str, object]],
) -> None:
    """Flatten input leaves while retaining paths for deterministic matching."""

    if isinstance(value, Mapping):
        for key in sorted(value, key=lambda item: str(item)):
            _flatten_input(
                value[key],
                path=_mapping_path(path, key),
                strings=strings,
                scalars=scalars,
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _flatten_input(
                item,
                path=f"{path}[{index}]",
                strings=strings,
                scalars=scalars,
            )
        return
    if isinstance(value, (set, frozenset)):
        ordered = sorted(
            value,
            key=_canonical_tagged_sort_key,
        )
        for index, item in enumerate(ordered):
            _flatten_input(
                item,
                path=f"{path}[{index}]",
                strings=strings,
                scalars=scalars,
            )
        return
    if isinstance(value, str):
        strings.append((path, value))
        return
    scalars.append((path, value))


def _text_signature(value: object) -> tuple[tuple[str, object], frozenset[str]]:
    """Return the scalar and character-trigram signatures for an input."""

    strings: list[tuple[str, str]] = []
    scalars: list[tuple[str, object]] = []
    _flatten_input(value, path="$", strings=strings, scalars=scalars)
    normalized = "\n".join(
        f"{path}={_normalize_similarity_text(text)}"
        for path, text in sorted(strings)
    )
    grams = (
        {normalized[index : index + 3] for index in range(len(normalized) - 2)}
        if len(normalized) >= 3
        else {normalized}
    )
    return tuple(sorted(scalars, key=lambda item: item[0])), frozenset(grams)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """Return Jaccard similarity for two immutable trigram sets."""

    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _same_scalar_signature(
    left: tuple[tuple[str, object], ...], right: tuple[tuple[str, object], ...]
) -> bool:
    """Compare scalar leaves without conflating Python values such as ``1`` and ``True``."""

    if len(left) != len(right):
        return False
    return all(
        left_item[0] == right_item[0]
        and type(left_item[1]) is type(right_item[1])
        and left_item[1] == right_item[1]
        for left_item, right_item in zip(left, right, strict=True)
    )


def _canonical_expected(expected: BaseModel) -> str:
    """Serialize a production expected object independently of input aliases."""

    return _canonical_json(expected, label="production expected object")


def _near_duplicate_pairs(
    split_cases: Mapping[str, tuple[ValidatedCase, ...]],
) -> tuple[Mapping[str, object], ...]:
    """Find deterministic near-duplicate case pairs across the complete suite."""

    ordered: list[tuple[int, str, str, ValidatedCase]] = []
    split_order = {split: index for index, split in enumerate(_SPLITS)}
    for split in _SPLITS:
        for validated in sorted(
            split_cases.get(split, ()), key=lambda item: item.case.id
        ):
            ordered.append((split_order[split], split, validated.case.id, validated))

    signatures: dict[int, tuple[str, tuple[tuple[str, object], ...], frozenset[str]]] = {}
    for index, (_rank, _split, _case_id, validated) in enumerate(ordered):
        scalar_signature, text_grams = _text_signature(validated.case.input)
        signatures[index] = (
            _canonical_expected(validated.expected),
            scalar_signature,
            text_grams,
        )

    pairs: list[Mapping[str, object]] = []
    for left_index, (_left_rank, left_split, left_id, left) in enumerate(ordered):
        left_expected, left_scalars, left_grams = signatures[left_index]
        for right_index in range(left_index + 1, len(ordered)):
            _right_rank, right_split, right_id, right = ordered[right_index]
            right_expected, right_scalars, right_grams = signatures[right_index]
            if left.case.coverage.primary_obligation != right.case.coverage.primary_obligation:
                continue
            if left_expected != right_expected or not _same_scalar_signature(
                left_scalars, right_scalars
            ):
                continue
            similarity = _jaccard(left_grams, right_grams)
            if similarity < 0.85:
                continue
            pairs.append(
                {
                    "left": {"split": left_split, "id": left_id},
                    "right": {"split": right_split, "id": right_id},
                    "primary_obligation": left.case.coverage.primary_obligation,
                    "similarity": similarity,
                    "distinction": {
                        "left": left.case.coverage.distinction,
                        "right": right.case.coverage.distinction,
                    },
                }
            )
    return tuple(pairs)


def _near_duplicate_error(
    pairs: Sequence[Mapping[str, object]],
) -> CaseSetupError | None:
    """Return a setup error when any suspected pair lacks a distinction."""

    missing: list[str] = []
    for pair in pairs:
        distinctions = pair.get("distinction")
        if not isinstance(distinctions, Mapping):
            distinctions = {}
        left_distinction = distinctions.get("left")
        right_distinction = distinctions.get("right")
        if not isinstance(left_distinction, str) or not left_distinction.strip():
            left = pair.get("left")
            missing.append(
                f"{left!r} missing distinction"
            )
        if not isinstance(right_distinction, str) or not right_distinction.strip():
            right = pair.get("right")
            missing.append(
                f"{right!r} missing distinction"
            )
    if missing:
        return CaseSetupError(
            "near duplicate pair(s) require nonblank distinction: "
            + "; ".join(missing),
            details={
                "duplicates": {"hard": [], "near": list(pairs)},
                "conflicts": {"input": [], "scenario": []},
            },
        )
    return None


def _validate_coverage_references(
    case: EvalCase,
    *,
    split: str,
    obligations: CoverageObligations,
) -> None:
    """Validate case obligation references against an explicit asset."""

    by_id = {obligation.id: obligation for obligation in obligations.obligations}
    coverage = case.coverage
    primary = by_id.get(coverage.primary_obligation)
    if primary is None:
        raise CaseSetupError(
            f"{split} case {case.id!r} references unknown primary obligation "
            f"{coverage.primary_obligation!r}"
        )

    category_by_name = {
        category.category: category for category in obligations.categories
    }
    category = category_by_name.get(primary.category)
    if category is None or category.applicability != "required":
        applicability = category.applicability if category is not None else "unknown"
        raise CaseSetupError(
            f"{split} case {case.id!r} references primary obligation "
            f"{primary.id!r} with invalid category {primary.category!r} "
            f"({applicability})"
        )

    for secondary in coverage.secondary_obligations:
        secondary_obligation = by_id.get(secondary)
        if secondary_obligation is None:
            raise CaseSetupError(
                f"{split} case {case.id!r} references unknown secondary obligation "
                f"{secondary!r}"
            )
        secondary_category = category_by_name.get(secondary_obligation.category)
        if (
            secondary_category is None
            or secondary_category.applicability != "required"
        ):
            applicability = (
                secondary_category.applicability
                if secondary_category is not None
                else "unknown"
            )
            raise CaseSetupError(
                f"{split} case {case.id!r} references secondary obligation "
                f"{secondary_obligation.id!r} with invalid category "
                f"{secondary_obligation.category!r} ({applicability})"
            )

    declared_variants = primary.required_splits.get(split, ())
    if coverage.variant not in declared_variants:
        declared = ", ".join(declared_variants) or "none"
        raise CaseSetupError(
            f"{split} case {case.id!r} coverage variant {coverage.variant!r} "
            f"is not declared for primary obligation {primary.id!r} in {split} "
            f"(declared: {declared})"
        )


def _coverage_audit(
    split_cases: Mapping[str, tuple[ValidatedCase, ...]],
    obligations: CoverageObligations,
    *,
    near_duplicates: Sequence[Mapping[str, object]] = (),
) -> CoverageAudit:
    """Build the mechanical coverage audit after suite validation succeeds."""

    counts = {
        split: {
            "total": len(split_cases[split]),
            "valid": len(split_cases[split]),
            "non_counting": 0,
        }
        for split in _SPLITS
    }

    ordered_obligations = tuple(
        sorted(obligations.obligations, key=lambda obligation: obligation.id)
    )
    by_id = {obligation.id: obligation for obligation in ordered_obligations}
    category_distribution: dict[str, int] = {}
    obligation_distribution: dict[str, int] = {}
    risk_distribution: dict[str, int] = {}
    split_distribution: dict[str, int] = {}
    variant_distribution: dict[str, int] = {}
    observed: set[tuple[str, str, str]] = set()
    observed_by_quota: dict[tuple[str, str, str], list[str]] = {}
    observed_by_category: dict[str, list[Mapping[str, object]]] = {}

    for split in _SPLITS:
        split_distribution[split] = len(split_cases[split])
        for validated in split_cases[split]:
            coverage = validated.case.coverage
            obligation = by_id[coverage.primary_obligation]
            quota_key = (obligation.id, split, coverage.variant)
            observed.add(quota_key)
            observed_by_quota.setdefault(quota_key, []).append(validated.case.id)
            observed_by_category.setdefault(obligation.category, []).append(
                {
                    "split": split,
                    "id": validated.case.id,
                    "obligation": obligation.id,
                    "variant": coverage.variant,
                    "condition_id": coverage.condition_id,
                }
            )
            category_distribution[obligation.category] = (
                category_distribution.get(obligation.category, 0) + 1
            )
            obligation_distribution[obligation.id] = (
                obligation_distribution.get(obligation.id, 0) + 1
            )
            risk_distribution[obligation.risk] = (
                risk_distribution.get(obligation.risk, 0) + 1
            )
            variant_distribution[coverage.variant] = (
                variant_distribution.get(coverage.variant, 0) + 1
            )

    required = {
        (obligation.id, split, variant)
        for obligation in ordered_obligations
        for split, variants in obligation.required_splits.items()
        for variant in variants
    }
    missing_quotas = tuple(sorted(required - observed))

    category_by_name = {category.category: category for category in obligations.categories}
    category_declarations = (
        set(category_by_name) == FIXED_CATEGORIES
        and len(category_by_name) == len(obligations.categories)
        and all(
            category.applicability == "required"
            or (
                bool(category.evidence_checked)
                and bool(category.rationale and category.rationale.strip())
            )
            for category in obligations.categories
        )
    )

    category_rows: list[Mapping[str, object]] = []
    missing_categories: list[str] = []
    for category_name in sorted(category_by_name):
        category = category_by_name[category_name]
        obligation_ids = tuple(
            sorted(
                obligation.id
                for obligation in ordered_obligations
                if obligation.category == category_name
            )
        )
        observed_cases = tuple(
            sorted(
                observed_by_category.get(category_name, ()),
                key=lambda item: (
                    _SPLITS.index(str(item["split"])),
                    str(item["id"]),
                ),
            )
        )
        if category.applicability == "not_applicable":
            status = "not_applicable"
            complete = True
        elif not obligation_ids:
            status = "missing_obligation"
            complete = False
            missing_categories.append(category_name)
        elif not observed_cases:
            status = "missing_primary_coverage"
            complete = False
            missing_categories.append(category_name)
        else:
            status = "complete"
            complete = True
        category_rows.append(
            {
                "category": category_name,
                "applicability": category.applicability,
                "evidence_checked": tuple(category.evidence_checked),
                "rationale": category.rationale,
                "obligations": obligation_ids,
                "observed_primary_cases": observed_cases,
                "status": status,
                "complete": complete,
            }
        )

    obligation_rows: list[Mapping[str, object]] = []
    for obligation in ordered_obligations:
        requirements: list[Mapping[str, object]] = []
        for split in _SPLITS:
            for variant in sorted(obligation.required_splits.get(split, ())):
                quota_key = (obligation.id, split, variant)
                case_ids = tuple(sorted(observed_by_quota.get(quota_key, ())))
                requirements.append(
                    {
                        "split": split,
                        "variant": variant,
                        "case_ids": case_ids,
                        "complete": bool(case_ids),
                    }
                )
        obligation_rows.append(
            {
                "obligation": obligation.id,
                "category": obligation.category,
                "risk": obligation.risk,
                "requirements": tuple(requirements),
                "complete": all(
                    bool(requirement["complete"]) for requirement in requirements
                ),
            }
        )

    coverage_matrix = {
        "categories": tuple(category_rows),
        "obligations": tuple(obligation_rows),
        "missing_categories": tuple(sorted(missing_categories)),
        "missing_quotas": tuple(missing_quotas),
    }
    category_coverage = not missing_categories

    critical_coverage = True
    critical_variants = {"boundary", "conflict", "adversarial"}
    for obligation in ordered_obligations:
        if obligation.risk != "critical":
            continue
        actual_variants = {
            validated.case.coverage.variant
            for split in _SPLITS
            for validated in split_cases[split]
            if validated.case.coverage.primary_obligation == obligation.id
        }
        declared_variants = {
            variant
            for variants in obligation.required_splits.values()
            for variant in variants
        }
        missing_exclusions = (
            critical_variants
            - declared_variants
            - set(obligation.variant_exclusions)
        )
        if "normal" not in declared_variants or "normal" not in actual_variants:
            critical_coverage = False
        if not declared_variants & critical_variants or not actual_variants & critical_variants:
            critical_coverage = False
        if missing_exclusions:
            critical_coverage = False

    mechanical_gates = {
        "minimum_counts": all(
            counts[split]["valid"] >= MIN_CASES_PER_SPLIT for split in _SPLITS
        ),
        "required_quotas": not missing_quotas,
        "category_declarations": category_declarations,
        "critical_coverage": critical_coverage,
        # Hard duplicates are rejected before this audit is constructed.  The
        # empty tuple is therefore positive evidence that this gate passed.
        "hard_duplicates": True,
        "near_duplicate_explanations": all(
            isinstance(pair.get("distinction"), Mapping)
            and isinstance(pair["distinction"].get("left"), str)
            and bool(pair["distinction"]["left"].strip())
            and isinstance(pair["distinction"].get("right"), str)
            and bool(pair["distinction"]["right"].strip())
            for pair in near_duplicates
        ),
        "coverage_matrix": (
            category_declarations
            and category_coverage
            and not missing_quotas
            and critical_coverage
        ),
    }

    distributions = {
        "category": dict(sorted(category_distribution.items())),
        "obligation": dict(sorted(obligation_distribution.items())),
        "risk": dict(sorted(risk_distribution.items())),
        "split": dict(sorted(split_distribution.items())),
        "variant": dict(sorted(variant_distribution.items())),
    }
    return CoverageAudit(
        counts=counts,
        distributions=distributions,
        missing_quotas=missing_quotas,
        hard_duplicates=(),
        near_duplicates=tuple(near_duplicates),
        categories=tuple(category_rows),
        coverage_matrix=coverage_matrix,
        mechanical_gates=mechanical_gates,
    )


def _coverage_gate_error(
    audit: CoverageAudit,
    obligations: CoverageObligations,
    split_cases: Mapping[str, tuple[ValidatedCase, ...]],
) -> CaseSetupError | None:
    """Return one deterministic error covering all failed mechanical gates."""

    failures: list[str] = []
    for split in _SPLITS:
        valid = audit.counts[split]["valid"]
        if valid < MIN_CASES_PER_SPLIT:
            failures.append(
                f"{split} has {valid} valid cases (minimum {MIN_CASES_PER_SPLIT})"
            )

    if audit.missing_quotas:
        details = ", ".join(
            f"{obligation_id} in {split} for variant {variant}"
            for obligation_id, split, variant in audit.missing_quotas
        )
        failures.append(f"missing coverage quota(s): {details}")

    if not audit.mechanical_gates["category_declarations"]:
        declared = {category.category for category in obligations.categories}
        missing = sorted(FIXED_CATEGORIES - declared)
        suffix = f": missing {', '.join(missing)}" if missing else ""
        failures.append(f"category declarations are incomplete{suffix}")

    if not audit.mechanical_gates["critical_coverage"]:
        critical_variants = {"boundary", "conflict", "adversarial"}
        for obligation in sorted(
            obligations.obligations, key=lambda obligation: obligation.id
        ):
            if obligation.risk != "critical":
                continue
            actual = {
                validated.case.coverage.variant
                for split in _SPLITS
                for validated in split_cases[split]
                if validated.case.coverage.primary_obligation == obligation.id
            }
            # The model-level obligation validator normally catches malformed
            # critical declarations before this point.  Keep this fallback
            # message precise for callers that constructed such a model with
            # ``model_construct``.
            declared = {
                variant
                for variants in obligation.required_splits.values()
                for variant in variants
            }
            missing_exclusions = (
                critical_variants
                - declared
                - set(obligation.variant_exclusions)
            )
            missing = []
            if "normal" not in declared:
                missing.append("normal")
            if not declared & critical_variants:
                missing.append("boundary|conflict|adversarial")
            if missing:
                failures.append(
                    f"critical obligation {obligation.id!r} is missing "
                    + ", ".join(missing)
                )
            elif not actual:
                failures.append(
                    f"critical obligation {obligation.id!r} has no primary cases"
                )
            if missing_exclusions:
                failures.append(
                    f"critical obligation {obligation.id!r} is missing "
                    "variant_exclusions for "
                    + ", ".join(sorted(missing_exclusions))
                )

    matrix_categories = audit.coverage_matrix.get("categories", ())
    for category in matrix_categories:
        if not isinstance(category, Mapping) or category.get("complete"):
            continue
        category_name = category.get("category", "<unknown>")
        status = category.get("status")
        if status == "missing_obligation":
            failures.append(
                f"required category {category_name!r} has no obligation"
            )
        elif status == "missing_primary_coverage":
            failures.append(
                f"required category {category_name!r} has no observed primary coverage"
            )

    if not audit.mechanical_gates["coverage_matrix"]:
        failures.append("coverage matrix is incomplete")

    if failures:
        return CaseSetupError("coverage gates failed: " + "; ".join(failures))
    return None


def load_case_split(
    path: Path, schema: type[_SchemaT], split: str
) -> tuple[ValidatedCase, ...]:
    """Load and validate one split without touching the other case files.

    ``load_case_suite`` remains the strict three-split loader used when the
    complete evaluation asset is being checked.  Runner invocations for a
    single dataset, especially read-only development/validation verification,
    use this narrower entry point so an unrelated acceptance file is neither
    required nor read.
    """

    try:
        is_schema = isinstance(schema, type) and issubclass(schema, BaseModel)
    except TypeError:
        is_schema = False
    if not is_schema:
        raise CaseSetupError("schema must be a Pydantic BaseModel subclass")

    if not isinstance(split, str):
        raise CaseSetupError("case split name must be a string")
    normalized_split = _SPLIT_ALIASES.get(split.strip().casefold())
    if normalized_split is None:
        raise CaseSetupError(
            f"unknown case split {split!r}; expected dev, validation, acceptance"
        )

    try:
        split_path = Path(path)
    except (TypeError, ValueError) as exc:
        raise CaseSetupError(
            f"{normalized_split} case path is invalid: {exc}"
        ) from exc

    parsed = _parse_split(split_path, normalized_split, schema)
    seen_ids: set[str] = set()
    for validated in parsed:
        case_id = validated.case.id
        if case_id in seen_ids:
            raise CaseSetupError(
                f"duplicate case id {case_id!r} in {normalized_split}"
            )
        seen_ids.add(case_id)
    return parsed


def _conflict_reference(split: str, case_id: str) -> dict[str, str]:
    """Return a stable, redaction-safe reference to one case."""

    return {"split": split, "id": case_id}


def _conflict_fingerprint(canonical_input: str) -> str:
    """Return a non-reversible identifier for a canonical input fingerprint."""

    return hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()


def _hard_conflict_error(
    hard_duplicates: Sequence[Mapping[str, object]],
    *,
    input_conflicts: Sequence[Mapping[str, object]],
    scenario_conflicts: Sequence[Mapping[str, object]],
    semantic_conflicts: Sequence[Mapping[str, object]],
) -> CaseSetupError:
    """Build a setup error carrying deterministic duplicate/conflict details."""

    messages: list[str] = []
    for conflict in hard_duplicates:
        kind = conflict.get("kind")
        left = conflict.get("left")
        right = conflict.get("right")
        if kind == "case_id":
            messages.append(f"duplicate case id {conflict.get('key')!r}: {left!r} conflicts with {right!r}")
        elif kind == "input_fingerprint":
            messages.append(f"duplicate input fingerprint: {left!r} conflicts with {right!r}")
        elif kind == "scenario_key":
            messages.append(f"duplicate scenario key {conflict.get('key')!r}: {left!r} conflicts with {right!r}")
        elif kind == "normalized_input_fingerprint":
            messages.append(
                f"normalized input fingerprint leakage across splits: {left!r} conflicts with {right!r}"
            )
    for conflict in semantic_conflicts:
        messages.append(
            "semantic family leakage across splits: "
            f"{conflict.get('right')!r} conflicts with {conflict.get('left')!r}"
        )
    if not messages:
        messages.append("case identity conflicts were detected")
    details = {
        "duplicates": {"hard": list(hard_duplicates), "near": []},
        "conflicts": {
            "input": list(input_conflicts),
            "scenario": list(scenario_conflicts),
            "semantic_family": list(semantic_conflicts),
        },
    }
    return CaseSetupError(
        "case identity validation failed: " + "; ".join(messages),
        details=details,
    )


def load_case_suite(
    paths: object,
    schema: type[_SchemaT],
    *,
    obligations: CoverageObligations,
) -> CaseSuite:
    """Load three YAML case files and validate every expected production object.

    ``paths`` may be a ``(dev, validation, acceptance)`` sequence or a mapping
    keyed by those split names.  The returned expected values are live
    instances of ``schema`` for direct use by runner/scorer code.
    """

    if not isinstance(obligations, CoverageObligations):
        raise CaseSetupError(
            "obligations must be an explicit CoverageObligations instance"
        )

    try:
        is_schema = isinstance(schema, type) and issubclass(schema, BaseModel)
    except TypeError:
        is_schema = False
    if not is_schema:
        raise CaseSetupError("schema must be a Pydantic BaseModel subclass")

    split_paths = _split_paths(paths)
    split_cases = {
        split: _parse_split(split_paths[split], split, schema) for split in _SPLITS
    }

    seen_ids: dict[str, str] = {}
    seen_families: dict[str, tuple[str, str]] = {}
    seen_inputs: dict[str, tuple[str, str]] = {}
    seen_normalized_inputs: dict[str, tuple[str, str]] = {}
    seen_scenarios: dict[tuple[str, str, str], tuple[str, str]] = {}
    hard_duplicates: list[Mapping[str, object]] = []
    input_conflicts: list[Mapping[str, object]] = []
    scenario_conflicts: list[Mapping[str, object]] = []
    semantic_conflicts: list[Mapping[str, object]] = []
    for split in _SPLITS:
        for validated in split_cases[split]:
            case = validated.case
            _validate_coverage_references(
                case, split=split, obligations=obligations
            )
            previous_split = seen_ids.get(case.id)
            if previous_split is not None:
                hard_duplicates.append(
                    {
                        "kind": "case_id",
                        "key": case.id,
                        "left": _conflict_reference(previous_split, case.id),
                        "right": _conflict_reference(split, case.id),
                    }
                )
            else:
                seen_ids[case.id] = split

            family = _normalized_family(case.semantic_family)
            if not family:
                raise CaseSetupError(
                    f"{split} case {case.id!r} semantic family must contain a letter or digit"
                )
            previous = seen_families.get(family)
            if previous is not None and previous[0] != split:
                semantic_conflicts.append(
                    {
                        "kind": "semantic_family",
                        "fingerprint": _conflict_fingerprint(family),
                        "left": _conflict_reference(previous[0], previous[1]),
                        "right": _conflict_reference(split, case.id),
                    }
                )
            else:
                seen_families.setdefault(family, (split, case.id))

            input_fingerprint = _canonical_json(
                case.input, label=f"{split} case {case.id!r} input"
            )
            normalized_input_fingerprint = _canonical_json(
                _normalize_input_strings(case.input),
                label=f"{split} case {case.id!r} input",
            )
            previous_input = seen_inputs.get(input_fingerprint)
            if previous_input is not None:
                conflict = {
                    "kind": "input_fingerprint",
                    "fingerprint": _conflict_fingerprint(input_fingerprint),
                    "left": _conflict_reference(previous_input[0], previous_input[1]),
                    "right": _conflict_reference(split, case.id),
                }
                input_conflicts.append(conflict)
                hard_duplicates.append(conflict)
            else:
                seen_inputs[input_fingerprint] = (split, case.id)
            previous_normalized_input = seen_normalized_inputs.get(
                normalized_input_fingerprint
            )
            if (
                previous_normalized_input is not None
                and previous_normalized_input[0] != split
            ):
                conflict = {
                    "kind": "normalized_input_fingerprint",
                    "fingerprint": _conflict_fingerprint(
                        normalized_input_fingerprint
                    ),
                    "left": _conflict_reference(
                        previous_normalized_input[0], previous_normalized_input[1]
                    ),
                    "right": _conflict_reference(split, case.id),
                }
                input_conflicts.append(conflict)
                hard_duplicates.append(conflict)
            else:
                seen_normalized_inputs[normalized_input_fingerprint] = (
                    split,
                    case.id,
                )

            scenario_key = _scenario_key(case)
            previous_scenario = seen_scenarios.get(scenario_key)
            if previous_scenario is not None:
                conflict = {
                    "kind": "scenario_key",
                    "key": list(scenario_key),
                    "left": _conflict_reference(previous_scenario[0], previous_scenario[1]),
                    "right": _conflict_reference(split, case.id),
                }
                scenario_conflicts.append(conflict)
                hard_duplicates.append(conflict)
            else:
                seen_scenarios[scenario_key] = (split, case.id)

    if hard_duplicates or semantic_conflicts:
        raise _hard_conflict_error(
            hard_duplicates,
            input_conflicts=input_conflicts,
            scenario_conflicts=scenario_conflicts,
            semantic_conflicts=semantic_conflicts,
        )

    near_duplicates = _near_duplicate_pairs(split_cases)
    near_duplicate_error = _near_duplicate_error(near_duplicates)
    if near_duplicate_error is not None:
        raise near_duplicate_error

    coverage_audit = _coverage_audit(
        split_cases, obligations, near_duplicates=near_duplicates
    )
    coverage_error = _coverage_gate_error(
        coverage_audit, obligations, split_cases
    )
    if coverage_error is not None:
        raise coverage_error

    return CaseSuite(
        dev=split_cases["dev"],
        validation=split_cases["validation"],
        acceptance=split_cases["acceptance"],
        coverage_audit=coverage_audit,
    )


def _canonical_case(validated: ValidatedCase) -> dict[str, Any]:
    data = validated.case.model_dump(mode="python")
    # The production model is the authoritative serialization for the expected
    # output, preventing equivalent Pydantic inputs from producing two hashes.
    data["expect"] = dict(data["expect"])
    # Keep the live model here so _canonicalize_json can pair its Python-mode
    # containers with Pydantic's JSON-mode scalar/field serialization.
    data["expect"]["output"] = validated.expected
    canonical = json.loads(_canonical_json(data, label="case"))
    if not isinstance(canonical, dict):
        raise CaseSetupError("case must serialize to a JSON object")
    return canonical


def dataset_hash(suite: CaseSuite) -> str:
    """Return the SHA-256 of the suite's canonical, split-aware JSON form."""

    if not isinstance(suite, CaseSuite):
        raise TypeError("dataset_hash expects a CaseSuite")

    payload = {
        split: [
            _canonical_case(validated)
            for validated in sorted(suite[split], key=lambda item: item.case.id)
        ]
        for split in _SPLITS
    }
    canonical = _canonical_json(payload, label="case suite").encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_schema(reference: str, eval_root: Path) -> type[BaseModel]:
    """Import a production Schema from the documented ``MODULE:CLASS`` form."""

    if not isinstance(reference, str) or reference.count(":") != 1:
        raise CaseSetupError("--schema must use MODULE:CLASS")
    module_name, qualname = (part.strip() for part in reference.split(":", 1))
    if not module_name or not qualname or any(
        part in {"", ".", "..", "<locals>"} for part in qualname.split(".")
    ):
        raise CaseSetupError("--schema must use MODULE:CLASS")

    root_text = str(Path(eval_root).resolve(strict=False))
    added_root = root_text not in sys.path
    if added_root:
        sys.path.insert(0, root_text)
    try:
        try:
            module: object = importlib.import_module(module_name)
        except Exception as error:
            raise CaseSetupError(
                f"unable to import Schema module {module_name!r}: {type(error).__name__}"
            ) from None
    finally:
        if added_root:
            try:
                sys.path.remove(root_text)
            except ValueError:
                pass

    value: object = module
    try:
        for name in qualname.split("."):
            value = getattr(value, name)
    except AttributeError:
        raise CaseSetupError(
            f"Schema class {qualname!r} is not defined by module {module_name!r}"
        ) from None
    try:
        is_schema = isinstance(value, type) and issubclass(value, BaseModel)
    except TypeError:
        is_schema = False
    if not is_schema:
        raise CaseSetupError(f"Schema {reference!r} must be a Pydantic BaseModel subclass")
    return value


def _git_repository_root(
    start: Path, *, explicit_root: Path | None = None
) -> Path:
    """Resolve the repository root containing an evaluation asset."""

    if explicit_root is not None:
        try:
            return Path(explicit_root).resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise CaseSetupError(f"repository root is invalid: {error}") from error

    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    except OSError as error:
        raise CaseSetupError(f"unable to resolve repository root: {error}") from error
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve(strict=False)
    detail = (result.stderr or result.stdout).strip() if result is not None else ""
    suffix = f": {detail}" if detail else ""
    raise CaseSetupError(f"unable to resolve repository root for {start}{suffix}")


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


def _redact_cli_value(value: object) -> object:
    """Redact credential-shaped values before emitting case evidence."""

    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _SENSITIVE_OUTPUT_KEY:
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_cli_value(item)
        return result
    if isinstance(value, list):
        return [_redact_cli_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_cli_value(item) for item in value]
    if isinstance(value, Set):
        redacted = [_redact_cli_value(item) for item in value]
        redacted.sort(key=_canonical_tagged_sort_key)
        return redacted
    if isinstance(value, str):
        text = re.sub(
            r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+",
            r"\1[REDACTED]",
            value,
        )
        return re.sub(r"(?i)(\bbearer\s+)[^\s,;\"']+", r"\1[REDACTED]", text)
    return value


def _file_sha256(path: Path, *, label: str) -> str:
    """Return the exact-byte SHA-256 for one emitted evaluation asset."""

    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError) as error:
        raise CaseSetupError(f"unable to hash {label}: {error}") from error


def _suite_payload(
    suite: CaseSuite,
    eval_root: Path,
    schema_ref: str,
    *,
    obligations_path: Path | None = None,
    split_paths: Mapping[str, Path] | None = None,
) -> dict[str, object]:
    """Build the stable JSON representation written by the validation CLI."""

    root = Path(eval_root)
    obligation_path = obligations_path or root / "coverage-obligations.yaml"
    paths = split_paths or {
        split: root / f"{split}-cases.yaml" for split in _SPLITS
    }
    canonical_split_paths = {
        split: Path(paths[split]) for split in _SPLITS
    }
    audit = suite.coverage_audit
    case_suite_hash = dataset_hash(suite)
    return {
        "status": "valid",
        "requires_user_review": bool(audit.near_duplicates),
        "eval_root": str(root.resolve(strict=False)),
        "schema": schema_ref,
        "case_suite_hash": case_suite_hash,
        "coverage_obligations_hash": coverage_obligations_hash(obligation_path),
        "case_file_hashes": {
            split: _file_sha256(canonical_split_paths[split], label=f"{split} cases")
            for split in _SPLITS
        },
        "counts": audit.counts,
        "coverage_matrix": audit.coverage_matrix,
        "coverage": {
            "categories": audit.categories,
            "matrix": audit.coverage_matrix,
            "distributions": audit.distributions,
            "missing_quotas": [list(item) for item in audit.missing_quotas],
            "mechanical_gates": dict(audit.mechanical_gates),
        },
        "duplicates": {
            "hard": list(audit.hard_duplicates),
            "near": list(audit.near_duplicates),
        },
        "splits": {
            split: [
                _redact_cli_value(_canonical_case(item))
                for item in sorted(suite[split], key=lambda item: item.case.id)
            ]
            for split in _SPLITS
        },
    }


def _safe_error(error: BaseException) -> str:
    """Format CLI errors without exposing credential-shaped values."""

    text = str(error).replace("\x00", "")
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(\bbearer\s+)[^\s,;\"']+", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:authorization[_-]?token|api[_-]?key|access[_-]?token|token|secret)\s*[:=]\s*)[\"']?[^\s,;\"']+",
        r"\1[REDACTED]",
        text,
    )
    return text or type(error).__name__


def _write_json(path: Path, payload: object) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-root", type=Path, required=True, help="evaluation asset directory")
    parser.add_argument(
        "--schema",
        dest="schema_ref",
        required=True,
        help="production Pydantic Schema import reference MODULE:CLASS",
    )
    parser.add_argument("--output", type=Path, required=True, help="validated suite JSON path")
    return parser


def main(
    argv: Sequence[str] | None = None, *, repo_root: Path | None = None
) -> int:
    """Validate all three case splits and write a machine-readable summary."""

    args = _parser().parse_args(argv)
    try:
        eval_root = args.eval_root.resolve(strict=False)
        if not eval_root.is_dir():
            raise CaseSetupError(f"evaluation root does not exist: {args.eval_root}")
        obligations_path = eval_root / "coverage-obligations.yaml"
        obligations = load_coverage_obligations(
            obligations_path,
            _git_repository_root(eval_root, explicit_root=repo_root),
        )
        schema = _load_schema(args.schema_ref, eval_root)
        paths = tuple(eval_root / f"{split}-cases.yaml" for split in _SPLITS)
        suite = load_case_suite(paths, schema, obligations=obligations)
        payload = _suite_payload(
            suite,
            eval_root,
            args.schema_ref,
            obligations_path=obligations_path,
            split_paths={split: paths[index] for index, split in enumerate(_SPLITS)},
        )
        _write_json(args.output, payload)
    except (CaseSetupError, OSError, ValueError, TypeError) as error:
        payload = {"status": "error", "error": _safe_error(error)}
        details = getattr(error, "details", None)
        if isinstance(details, Mapping):
            payload.update(_redact_cli_value(details))
        try:
            _write_json(args.output, payload)
        except OSError as write_error:
            payload["error"] = f"{payload['error']}; unable to write output: {_safe_error(write_error)}"
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


__all__ = [
    "CaseSetupError",
    "CaseSuite",
    "CaseCoverage",
    "CoverageAudit",
    "CoverageCategory",
    "CoverageObligation",
    "CoverageObligations",
    "EvalCase",
    "FIXED_CATEGORIES",
    "FIXED_VARIANTS",
    "MIN_CASES_PER_SPLIT",
    "RequiredSplits",
    "ValidatedCase",
    "coverage_obligations_hash",
    "dataset_hash",
    "load_coverage_obligations",
    "load_case_split",
    "load_case_suite",
    "_canonical_json",
    "_canonical_expected",
    "_flatten_input",
    "_jaccard",
    "_near_duplicate_pairs",
    "_normalize_similarity_text",
    "_scenario_key",
    "_text_signature",
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by CLI probes
    raise SystemExit(main())
