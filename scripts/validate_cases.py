"""Load and validate the frozen prompt-evaluation case datasets.

Case files are evaluation assets rather than model output.  They are therefore
validated against the same production Pydantic model used by the runner before
any model call is made.  The loader also owns the cross-split invariants that
keep validation and acceptance data from leaking into development data.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Literal, TypeVar
import unicodedata

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CaseSetupError(ValueError):
    """Raised when case files cannot form a valid evaluation dataset."""


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


@dataclass(frozen=True)
class CaseSuite:
    """The three evaluation splits in their runner-facing form."""

    dev: tuple[ValidatedCase, ...]
    validation: tuple[ValidatedCase, ...]
    acceptance: tuple[ValidatedCase, ...]

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
    elif isinstance(paths, Sequence) and not isinstance(paths, (str, bytes, bytearray)):
        if len(paths) != 3:
            raise CaseSetupError(
                "load_case_suite requires exactly three paths: dev, validation, acceptance"
            )
        result = dict(zip(_SPLITS, (Path(path) for path in paths), strict=True))
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
    return value


def _canonical_json(value: object, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
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


def load_case_suite(
    paths: object, schema: type[_SchemaT]
) -> CaseSuite:
    """Load three YAML case files and validate every expected production object.

    ``paths`` may be a ``(dev, validation, acceptance)`` sequence or a mapping
    keyed by those split names.  The returned expected values are live
    instances of ``schema`` for direct use by runner/scorer code.
    """

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
    for split in _SPLITS:
        for validated in split_cases[split]:
            case = validated.case
            previous_split = seen_ids.get(case.id)
            if previous_split is not None:
                raise CaseSetupError(
                    f"duplicate case id {case.id!r} in {previous_split} and {split}"
                )
            seen_ids[case.id] = split

            family = _normalized_family(case.semantic_family)
            if not family:
                raise CaseSetupError(
                    f"{split} case {case.id!r} semantic family must contain a letter or digit"
                )
            previous = seen_families.get(family)
            if previous is not None and previous[0] != split:
                raise CaseSetupError(
                    "semantic family leakage across splits: "
                    f"{case.semantic_family!r} in {split} conflicts with "
                    f"{previous[1]!r} in {previous[0]}"
                )
            seen_families.setdefault(family, (split, case.semantic_family))

            input_fingerprint = _canonical_json(
                case.input, label=f"{split} case {case.id!r} input"
            )
            normalized_input_fingerprint = _canonical_json(
                _normalize_input_strings(case.input),
                label=f"{split} case {case.id!r} input",
            )
            previous_input = seen_inputs.get(input_fingerprint)
            if previous_input is not None and previous_input[0] != split:
                raise CaseSetupError(
                    "input fingerprint leakage across splits: "
                    f"{case.id!r} in {split} conflicts with "
                    f"{previous_input[1]!r} in {previous_input[0]}"
                )
            previous_normalized_input = seen_normalized_inputs.get(
                normalized_input_fingerprint
            )
            if (
                previous_normalized_input is not None
                and previous_normalized_input[0] != split
            ):
                raise CaseSetupError(
                    "normalized input fingerprint leakage across splits: "
                    f"{case.id!r} in {split} conflicts with "
                    f"{previous_normalized_input[1]!r} in {previous_normalized_input[0]}"
                )
            seen_inputs.setdefault(input_fingerprint, (split, case.id))
            seen_normalized_inputs.setdefault(
                normalized_input_fingerprint, (split, case.id)
            )

    return CaseSuite(
        dev=split_cases["dev"],
        validation=split_cases["validation"],
        acceptance=split_cases["acceptance"],
    )


def _canonical_case(validated: ValidatedCase) -> dict[str, Any]:
    data = validated.case.model_dump(mode="json")
    # The production model is the authoritative serialization for the expected
    # output, preventing equivalent Pydantic inputs from producing two hashes.
    data["expect"] = dict(data["expect"])
    data["expect"]["output"] = validated.expected.model_dump(mode="json")
    return data


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
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
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
    if isinstance(value, str):
        text = re.sub(
            r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;\"']+",
            r"\1[REDACTED]",
            value,
        )
        return re.sub(r"(?i)(\bbearer\s+)[^\s,;\"']+", r"\1[REDACTED]", text)
    return value


def _suite_payload(suite: CaseSuite, eval_root: Path, schema_ref: str) -> dict[str, object]:
    """Build the stable JSON representation written by the validation CLI."""

    return {
        "status": "valid",
        "eval_root": str(eval_root.resolve(strict=False)),
        "schema": schema_ref,
        "dataset_hash": dataset_hash(suite),
        "splits": {
            split: [_redact_cli_value(_canonical_case(item)) for item in suite[split]]
            for split in _SPLITS
        },
        "counts": {split: len(suite[split]) for split in _SPLITS},
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


def main(argv: Sequence[str] | None = None) -> int:
    """Validate all three case splits and write a machine-readable summary."""

    args = _parser().parse_args(argv)
    try:
        eval_root = args.eval_root.resolve(strict=False)
        if not eval_root.is_dir():
            raise CaseSetupError(f"evaluation root does not exist: {args.eval_root}")
        schema = _load_schema(args.schema_ref, eval_root)
        paths = tuple(eval_root / f"{split}-cases.yaml" for split in _SPLITS)
        suite = load_case_suite(paths, schema)
        payload = _suite_payload(suite, eval_root, args.schema_ref)
        _write_json(args.output, payload)
    except (CaseSetupError, OSError, ValueError, TypeError) as error:
        payload = {"status": "error", "error": _safe_error(error)}
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
    "main",
]


if __name__ == "__main__":  # pragma: no cover - exercised by CLI probes
    raise SystemExit(main())
