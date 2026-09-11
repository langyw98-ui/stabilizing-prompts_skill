"""Load and validate the frozen prompt-evaluation case datasets.

Case files are evaluation assets rather than model output.  They are therefore
validated against the same production Pydantic model used by the runner before
any model call is made.  The loader also owns the cross-split invariants that
keep validation and acceptance data from leaking into development data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Literal, TypeVar
import unicodedata

import yaml
from pydantic import BaseModel, ConfigDict, field_validator


class CaseSetupError(ValueError):
    """Raised when case files cannot form a valid evaluation dataset."""


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


_SPLITS = ("dev", "validation", "acceptance")
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


def _normalized_family(value: str) -> str:
    """Normalize harmless spelling/punctuation differences for leakage checks."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    # Treat separators, punctuation, and whitespace as equivalent while
    # retaining Unicode letters/digits in non-English family names.
    return "".join(character for character in normalized if character.isalnum())


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

    declared = set(schema.model_fields)
    supplied = set(output)
    missing = sorted(declared - supplied)
    if missing:
        raise CaseSetupError(
            f"{split} case {case.id!r} expect.output is missing schema field(s): "
            + ", ".join(missing)
        )
    unexpected = sorted(supplied - declared)
    if unexpected:
        raise CaseSetupError(
            f"{split} case {case.id!r} expect.output contains unknown schema field(s): "
            + ", ".join(unexpected)
        )

    try:
        return schema.model_validate(output)
    except Exception as exc:
        detail = _format_validation_error(exc)
        raise CaseSetupError(
            f"{split} case {case.id!r} has an invalid expect.output at item {index}: {detail}"
        ) from exc


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


__all__ = [
    "CaseSetupError",
    "CaseSuite",
    "EvalCase",
    "ValidatedCase",
    "dataset_hash",
    "load_case_suite",
]
