# Coverage-Driven Cases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require three evidence-backed case splits of at least 30 valid cases each, with frozen coverage obligations, deterministic duplicate audits, mechanically complete coverage, and explicit human saturation confirmation before any model call.

**Architecture:** Extend `scripts/validate_cases.py` as the single schema and mechanical-validation boundary while preserving `load_case_split` for runner isolation. The skill remains the orchestration boundary for evidence scanning, saturation statements, user confirmation, freezing, and invalidation; no new orchestrator, CLI mode, runtime manifest field, or confirmation asset is introduced.

**Tech Stack:** Python 3.14, Pydantic v2, PyYAML, pytest

**Spec:** `docs/superpowers/specs/2026-09-14-stabilizing-prompts-project-local-worktree-design.md`

## Global Constraints

- `dev`, `validation`, and `acceptance` each require at least 30 valid cases; the constant is not configurable.
- Thirty is a floor, not a stopping condition; evidenced uncovered boundaries require more cases.
- Target-model output is never business truth and cannot create obligations or expected objects.
- Fixed categories must be declared `required` or evidence-backed `not_applicable`.
- Fixed variants are `normal`, `boundary`, `conflict`, `ambiguity`, `irrelevant`, `fallback`, `regression`, `adversarial`, and `natural_variation`.
- Hard duplicates fail; near duplicates require a substantive `distinction` and explicit user review.
- `validate_cases.py` reports only mechanical coverage. Codex proposes saturation after scanning production evidence, and the user confirms it.
- `coverage-obligations.yaml` is frozen, hashed, committed, and deliverable, but run manifests do not bind its hash.
- Acceptance isolation, fixed repeats, scoring, candidate limits, and delivery confirmation remain unchanged.
- All commands in this repository are run through `rtk`.

---

### Task 1: Coverage obligation schema and canonical hash

**Files:**
- Modify: `scripts/validate_cases.py:23-114,416-435,602-611`
- Test: `tests/test_validate_cases.py`

**Interfaces:**
- Produces: `CoverageCategory`, `RequiredSplits`, `CoverageObligation`, `CoverageObligations`, `load_coverage_obligations(path: Path, repo_root: Path) -> CoverageObligations`, and `coverage_obligations_hash(path: Path) -> str`.
- Consumes later: Task 2 case-reference validation and Task 3 mechanical quotas.

- [ ] **Step 1: Add failing schema tests**

Add these test helpers first:

```python
def complete_obligations_payload() -> dict[str, object]:
    categories = [
        "normal_path", "output_partition", "near_boundary", "field_boundary",
        "conditional_branch", "conflict", "ambiguity", "irrelevant_input",
        "fallback", "historical_regression", "adversarial",
    ]
    return {
        "version": 1,
        "categories": [
            {"category": name, "applicability": "required", "evidence_checked": []}
            for name in categories
        ],
        "obligations": [{
            "id": "classify-input",
            "source": ["evidence.py"],
            "category": "normal_path",
            "risk": "normal",
            "rule": "return the evidenced routing decision",
            "required_splits": {
                "dev": ["normal"],
                "validation": ["boundary"],
                "acceptance": ["natural_variation"],
            },
            "variant_exclusions": {},
        }],
    }


def write_obligations(root: Path, payload: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "evidence.py").write_text("EVIDENCE = True\n", encoding="utf-8")
    path = root / "coverage-obligations.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path
```

```python
def test_coverage_obligations_require_every_fixed_category(tmp_path):
    path = tmp_path / "coverage-obligations.yaml"
    path.write_text("version: 1\ncategories: []\nobligations: []\n", encoding="utf-8")
    with pytest.raises(CaseSetupError, match="missing categor"):
        load_coverage_obligations(path, tmp_path)


def test_not_applicable_requires_evidence_and_rationale(tmp_path):
    payload = complete_obligations_payload()
    payload["categories"][0] = {
        "category": "normal_path",
        "applicability": "not_applicable",
        "evidence_checked": [],
        "rationale": "",
    }
    path = write_obligations(tmp_path, payload)
    with pytest.raises(CaseSetupError, match="evidence_checked|rationale"):
        load_coverage_obligations(path, tmp_path)
```

Alongside the concrete tests above, implement named cases for duplicate obligation IDs, unknown category, unknown variant, unknown risk, an obligation referencing a `not_applicable` category, an empty source list, a missing repository-relative source file, and an empty split variant list. Each rejection matches the invalid field name. Add `test_coverage_obligations_hash_binds_exact_file_bytes`: assert an unchanged file has the same 64-character SHA-256 and that adding YAML whitespace changes it.

- [ ] **Step 2: Run the schema tests and verify failure**

Run: `rtk pytest tests/test_validate_cases.py -k "coverage_obligations or not_applicable or obligation_hash" -q`

Expected: FAIL because coverage obligation types and loaders do not exist.

- [ ] **Step 3: Add fixed enums and Pydantic models**

```python
FIXED_CATEGORIES = frozenset({
    "normal_path", "output_partition", "near_boundary", "field_boundary",
    "conditional_branch", "conflict", "ambiguity", "irrelevant_input",
    "fallback", "historical_regression", "adversarial",
})
FIXED_VARIANTS = frozenset({
    "normal", "boundary", "conflict", "ambiguity", "irrelevant",
    "fallback", "regression", "adversarial", "natural_variation",
})
MIN_CASES_PER_SPLIT = 30


class CoverageCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    applicability: Literal["required", "not_applicable"]
    evidence_checked: list[str] = []
    rationale: str | None = None


class CoverageObligation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: list[str]
    category: str
    risk: Literal["normal", "critical"]
    rule: str
    required_splits: dict[Literal["dev", "validation", "acceptance"], list[str]]
    variant_exclusions: dict[str, str] = {}


class CoverageObligations(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1]
    categories: list[CoverageCategory]
    obligations: list[CoverageObligation]
```

Use `Field(default_factory=list)` and `Field(default_factory=dict)` in production code. Add model validators for the cross-field rules, including critical `normal`, one of `boundary|conflict|adversarial`, and exclusion reasons for the other critical variants.

- [ ] **Step 4: Implement safe loading, source validation, and canonical hashing**

```python
def coverage_obligations_hash(path: Path) -> str:
    try:
        content = Path(path).read_bytes()
    except OSError as error:
        raise CaseSetupError(f"unable to hash coverage obligations: {error}") from error
    return hashlib.sha256(content).hexdigest()


def load_coverage_obligations(path: Path, repo_root: Path) -> CoverageObligations:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    try:
        value = CoverageObligations.model_validate(raw)
    except Exception as error:
        raise CaseSetupError(_format_validation_error(error)) from error
    root = Path(repo_root).resolve(strict=False)
    for obligation in value.obligations:
        for source in obligation.source:
            candidate = (root / source).resolve(strict=False)
            if not candidate.is_relative_to(root) or not candidate.is_file():
                raise CaseSetupError(f"unknown obligation source: {source}")
    return value
```

- [ ] **Step 5: Run the schema tests**

Run: `rtk pytest tests/test_validate_cases.py -k "coverage_obligations or not_applicable or obligation_hash" -q`

Expected: PASS.

- [ ] **Step 6: Commit obligation parsing**

```bash
rtk git add scripts/validate_cases.py tests/test_validate_cases.py
rtk git commit -m "feat: validate coverage obligation assets"
```

---

### Task 2: Case coverage metadata and global scenario identity

**Files:**
- Modify: `scripts/validate_cases.py:31-58,256-404,407-435`
- Modify: `tests/test_validate_cases.py`
- Modify: `tests/integration_support.py`

**Interfaces:**
- Consumes: `CoverageObligations` from Task 1.
- Produces: `CaseCoverage`, required `EvalCase.coverage`, and `_scenario_key(case: EvalCase) -> tuple[str, str, str]`.
- Changes: `load_case_suite(paths: object, schema: type[_SchemaT], *, obligations: CoverageObligations) -> CaseSuite`; the complete-suite loader always receives the already parsed obligation document explicitly.
- Preserves: `load_case_split(path, schema, split)` reads only the selected case file for runner isolation and validates the coverage field structurally without opening the obligation asset.

- [ ] **Step 1: Add failing case metadata tests**

Add reusable YAML mutators with explicit behavior:

```python
def read_cases(path: Path) -> list[dict[str, object]]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write_cases(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")


def remove_coverage(path: Path, *, case_id: str) -> None:
    cases = read_cases(path)
    next(case for case in cases if case["id"] == case_id).pop("coverage")
    write_cases(path, cases)


def duplicate_scenario_with_new_input(
    case_files: Mapping[str, Path], *, source_split: str, target_split: str
) -> None:
    source = read_cases(case_files[source_split])[0]
    target_cases = read_cases(case_files[target_split])
    target_cases[0]["coverage"] = dict(source["coverage"])
    target_cases[0]["input"] = {
        "variables": {"text": "different wording only"}, "context": {}
    }
    write_cases(case_files[target_split], target_cases)
```

```python
def test_case_requires_coverage_metadata(case_files, output_schema, coverage_obligations):
    remove_coverage(case_files["dev"], case_id="dev-000")
    with pytest.raises(CaseSetupError, match="coverage"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_global_scenario_key_rejects_wording_only_variation(
    case_files, output_schema, coverage_obligations
):
    duplicate_scenario_with_new_input(case_files, source_split="dev", target_split="validation")
    with pytest.raises(CaseSetupError, match="scenario key"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )
```

Add passing coverage where the same obligation uses a different variant or substantive `condition_id` and a different input fingerprint in another split. Assert secondary obligations never change scenario identity.

- [ ] **Step 2: Run the metadata tests and verify failure**

Run: `rtk pytest tests/test_validate_cases.py -k "coverage_metadata or scenario_key" -q`

Expected: FAIL because `EvalCase` has no `coverage` field.

- [ ] **Step 3: Implement metadata and normalized scenario keys**

```python
class CaseCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    primary_obligation: str
    secondary_obligations: list[str] = Field(default_factory=list)
    variant: str
    condition_id: str
    distinction: str | None = None


class EvalCase(BaseModel):
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


def _scenario_key(case: EvalCase) -> tuple[str, str, str]:
    coverage = case.coverage
    return (
        _normalized_family(coverage.primary_obligation),
        _normalized_family(coverage.variant),
        _normalized_family(coverage.condition_id),
    )
```

Reject blank or non-slug `condition_id`, duplicate secondary obligations, primary repeated as secondary, unknown fixed variants, and duplicate scenario keys globally. Extend exact canonical input fingerprint rejection to all pairs, including pairs within one split; keep normalized-input and semantic-family leakage checks across splits.

Add `test_rejects_exact_duplicate_inputs_within_one_split`: give two dev cases different IDs, families, obligations, variants, and conditions but the same complete input, then assert `CaseSetupError` contains `input fingerprint`.

Change `load_case_suite` to require the keyword-only `obligations` argument. During full-suite validation, reject unknown primary or secondary obligation IDs and variants not declared for that obligation/split. Update every existing complete-suite call in `tests/test_validate_cases.py`; do not change `load_case_split` or runner call sites.

- [ ] **Step 4: Update deterministic fixture case builders**

Use a shared helper instead of hand-writing 90 YAML objects:

```python
def make_case(split: str, index: int, *, obligation: str = "classify-input", variant: str = "normal") -> dict[str, object]:
    return {
        "id": f"{split}-{index:03d}",
        "semantic_family": f"{split}-family-{index:03d}",
        "source": ["target_app/production.py"],
        "input": {"payload": {"text": f"{split} evidenced condition {index:03d}"}},
        "expect": {"output": {"label": "allowed"}},
        "priority": "normal",
        "dimensions": ["coverage"],
        "rationale": f"evidenced {split} condition {index:03d}",
        "coverage": {
            "primary_obligation": obligation,
            "secondary_obligations": [],
            "variant": variant,
            "condition_id": f"{split}-condition-{index:03d}",
            "distinction": None,
        },
    }
```

- [ ] **Step 5: Run case parsing and runner-isolation tests**

Run: `rtk pytest tests/test_validate_cases.py tests/test_run_prompt_eval.py -k "case or split or scenario or acceptance" -q`

Expected: PASS; selected-split runner tests confirm `load_case_split` does not open the other splits or `coverage-obligations.yaml`.

- [ ] **Step 6: Commit case coverage identity**

```bash
rtk git add scripts/validate_cases.py tests/test_validate_cases.py tests/integration_support.py
rtk git commit -m "feat: attach coverage identity to evaluation cases"
```

---

### Task 3: Minimum counts, quotas, and mechanical coverage gates

**Files:**
- Modify: `scripts/validate_cases.py:60-114,319-435,521-534`
- Modify: `tests/test_validate_cases.py`

**Interfaces:**
- Consumes: validated `CoverageObligations` and cases with `CaseCoverage`.
- Produces: immutable `CoverageAudit` attached to `CaseSuite`, with counts, distributions, missing quotas, and mechanical gate results.

- [ ] **Step 1: Add failing gate tests**

Use these exact mutators:

```python
def remove_last_case(path: Path) -> None:
    cases = read_cases(path)
    write_cases(path, cases[:-1])


def require_variant(
    obligations: CoverageObligations, obligation_id: str, *, split: str, variant: str
) -> CoverageObligations:
    payload = obligations.model_dump(mode="json")
    matches = [value for value in payload["obligations"] if value["id"] == obligation_id]
    if matches:
        matches[0]["required_splits"].setdefault(split, []).append(variant)
    else:
        payload["obligations"].append({
            "id": obligation_id,
            "source": ["evidence.py"],
            "category": "normal_path",
            "risk": "normal",
            "rule": "exercise a quota that secondary references cannot satisfy",
            "required_splits": {split: [variant]},
            "variant_exclusions": {},
        })
    return CoverageObligations.model_validate(payload)


def add_secondary_reference_to_every_case(path: Path, obligation_id: str) -> None:
    cases = read_cases(path)
    for case in cases:
        case["coverage"]["secondary_obligations"].append(obligation_id)
    write_cases(path, cases)
```

```python
def test_each_split_requires_thirty_valid_cases(
    case_files, output_schema, coverage_obligations
):
    remove_last_case(case_files["validation"])
    with pytest.raises(CaseSetupError, match="validation.*29.*30"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_secondary_obligations_do_not_satisfy_quota(
    case_files, output_schema, coverage_obligations
):
    coverage_obligations = require_variant(
        coverage_obligations,
        "reject-unrelated",
        split="acceptance",
        variant="adversarial",
    )
    add_secondary_reference_to_every_case(case_files["acceptance"], "reject-unrelated")
    with pytest.raises(CaseSetupError, match="missing.*acceptance.*adversarial"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )
```

Using the deterministic builders, implement named cases for exactly 30 passing, 31 independent cases passing, a missing required split/variant quota, unknown primary and secondary obligation IDs, a case referencing a `not_applicable` category, a critical obligation missing `normal` or all risky variants, and a critical obligation missing exclusions for unused risky variants. The two passing tests assert exact `audit.counts`; every failing test asserts the obligation ID, split, or missing variant appears in `CaseSetupError`.

- [ ] **Step 2: Run the gate tests and verify failure**

Run: `rtk pytest tests/test_validate_cases.py -k "thirty or quota or secondary_obligations or critical" -q`

Expected: FAIL because counts and obligation quotas are not enforced.

- [ ] **Step 3: Implement the audit data contract**

```python
@dataclass(frozen=True)
class CoverageAudit:
    counts: Mapping[str, Mapping[str, int]]
    distributions: Mapping[str, object]
    missing_quotas: tuple[tuple[str, str, str], ...]
    hard_duplicates: tuple[Mapping[str, object], ...]
    near_duplicates: tuple[Mapping[str, object], ...]
    mechanical_gates: Mapping[str, bool]


@dataclass(frozen=True)
class CaseSuite:
    dev: tuple[ValidatedCase, ...]
    validation: tuple[ValidatedCase, ...]
    acceptance: tuple[ValidatedCase, ...]
    coverage_audit: CoverageAudit
```

- [ ] **Step 4: Implement count and quota calculation**

Build quota keys exclusively from primary coverage:

```python
observed = {
    (case.coverage.primary_obligation, split, case.coverage.variant)
    for split in _SPLITS
    for validated in split_cases[split]
    for case in (validated.case,)
}
required = {
    (obligation.id, split, variant)
    for obligation in obligations.obligations
    for split, variants in obligation.required_splits.items()
    for variant in variants
}
missing = tuple(sorted(required - observed))
```

Raise one `CaseSetupError` that names every below-minimum split and missing quota. Populate separate category, obligation, risk, split, and variant distributions plus booleans for `minimum_counts`, `required_quotas`, `category_declarations`, `critical_coverage`, `hard_duplicates`, `near_duplicate_explanations`, and `coverage_matrix`.

- [ ] **Step 5: Run the focused tests**

Run: `rtk pytest tests/test_validate_cases.py -k "thirty or quota or secondary_obligations or critical or distribution" -q`

Expected: PASS.

- [ ] **Step 6: Commit mechanical gates**

```bash
rtk git add scripts/validate_cases.py tests/test_validate_cases.py
rtk git commit -m "feat: enforce minimum cases and coverage quotas"
```

---

### Task 4: Deterministic near-duplicate audit and CLI output

**Files:**
- Modify: `scripts/validate_cases.py:179-216,319-435,521-599`
- Modify: `tests/test_validate_cases.py`

**Interfaces:**
- Consumes: `CaseSuite`, normalized production expected objects, and primary obligation IDs.
- Produces: `_near_duplicate_pairs(split_cases: Mapping[str, tuple[ValidatedCase, ...]]) -> tuple[Mapping[str, object], ...]`, `requires_user_review`, audit details, and stable content hashes in `CASE_SUITE_JSON`.

- [ ] **Step 1: Add failing near-duplicate tests**

Add a deterministic pair mutator:

```python
def make_near_duplicate_pair(
    case_files: Mapping[str, Path], *, distinction: str | None
) -> None:
    cases = read_cases(case_files["dev"])
    left = cases[0]
    right = copy.deepcopy(cases[1])
    right["expect"] = copy.deepcopy(left["expect"])
    right["coverage"]["primary_obligation"] = left["coverage"]["primary_obligation"]
    right["input"] = copy.deepcopy(left["input"])
    right["input"]["variables"]["text"] += "!"
    left["coverage"]["distinction"] = distinction
    right["coverage"]["distinction"] = distinction
    cases[0], cases[1] = left, right
    write_cases(case_files["dev"], cases)
```

```python
def test_near_duplicate_without_distinction_fails(
    case_files, output_schema, coverage_obligations
):
    make_near_duplicate_pair(case_files, distinction=None)
    with pytest.raises(CaseSetupError, match="near duplicate.*distinction"):
        load_case_suite(
            case_files.values(), output_schema, obligations=coverage_obligations
        )


def test_near_duplicate_with_distinction_requires_review(
    case_files, output_schema, coverage_obligations
):
    make_near_duplicate_pair(case_files, distinction="different evidenced decision boundary")
    suite = load_case_suite(
        case_files.values(), output_schema, obligations=coverage_obligations
    )
    assert suite.coverage_audit.near_duplicates
```

Add exact threshold tests at `0.85`, below-threshold tests, NFKC/case/punctuation/whitespace normalization, multiple string fields, non-string leaf mismatch, strings shorter than three characters, stable pair ordering, and normalized expected-object grouping.

- [ ] **Step 2: Run near-duplicate tests and verify failure**

Run: `rtk pytest tests/test_validate_cases.py -k "near_duplicate or jaccard or nfkc" -q`

Expected: FAIL because no near-duplicate audit exists.

- [ ] **Step 3: Implement one deterministic signature function**

```python
def _text_signature(value: object) -> tuple[tuple[str, object], frozenset[str]]:
    strings: list[tuple[str, str]] = []
    scalars: list[tuple[str, object]] = []
    _flatten_input(value, path="$", strings=strings, scalars=scalars)
    normalized = "\n".join(
        f"{path}={_normalize_similarity_text(text)}"
        for path, text in sorted(strings)
    )
    grams = (
        {normalized[index:index + 3] for index in range(len(normalized) - 2)}
        if len(normalized) >= 3
        else {normalized}
    )
    return tuple(sorted(scalars)), frozenset(grams)


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)
```

Implement `_normalize_similarity_text(text: str) -> str` as NFKC, casefold, Unicode punctuation removal, and whitespace collapse. Implement `_flatten_input(value, *, path, strings, scalars) -> None` recursively: mappings traverse keys in sorted string order, lists include numeric indexes in the path, string leaves append to `strings`, and all other JSON leaves append canonical `(path, value)` pairs to `scalars`.

Compare only pairs with the same primary obligation, canonical production expected object, and non-string signature. Emit each pair once in `(split order, case id)` order. A score `>= 0.85` is near-duplicate; a missing/blank distinction on either case fails validation, otherwise the pair remains valid and requires user review.

- [ ] **Step 4: Extend the stable CLI payload**

```python
return {
    "status": "valid",
    "requires_user_review": bool(audit.near_duplicates),
    "eval_root": str(eval_root.resolve(strict=False)),
    "schema": schema_ref,
    "case_suite_hash": dataset_hash(suite),
    "coverage_obligations_hash": coverage_obligations_hash(obligations_path),
    "case_file_hashes": {
        split: hashlib.sha256(path.read_bytes()).hexdigest()
        for split, path in split_paths.items()
    },
    "counts": audit.counts,
    "coverage": {
        "distributions": audit.distributions,
        "missing_quotas": [list(item) for item in audit.missing_quotas],
        "mechanical_gates": dict(audit.mechanical_gates),
    },
    "duplicates": {
        "hard": list(audit.hard_duplicates),
        "near": list(audit.near_duplicates),
    },
    "splits": {
        split: [_redact_cli_value(_canonical_case(item)) for item in suite[split]]
        for split in _SPLITS
    },
}
```

Keep top-level status limited to `valid|error`. The CLI reads `<eval-root>/coverage-obligations.yaml`, derives the repository root from `git rev-parse --show-toplevel`, and never reads a confirmation record.
It calls `load_coverage_obligations(obligations_path, repo_root)` first and passes that exact object to `load_case_suite(paths, schema, obligations=obligations)`; no second parse or implicit sibling-file lookup is allowed.

- [ ] **Step 5: Run validator tests**

Run: `rtk pytest tests/test_validate_cases.py -q`

Expected: PASS with stable JSON and no model calls.

- [ ] **Step 6: Commit duplicate audit and output**

```bash
rtk git add scripts/validate_cases.py tests/test_validate_cases.py
rtk git commit -m "feat: audit near-duplicate evaluation cases"
```

---

### Task 5: Freeze and deliver the new evaluation asset

**Files:**
- Modify: `scripts/manage_worktree.py:34-59`
- Modify: `tests/test_manage_worktree.py:71-170`
- Modify: `SKILL.md:79-147,239-297,299-314`
- Modify: `references/business-contract.md:1-73`
- Modify: `references/case-schema.md:1-89`
- Modify: `references/evaluation-method.md`
- Modify: `tests/test_skill_instructions.py`

**Interfaces:**
- Consumes: `coverage-obligations.yaml`, validator hashes, mechanical audit, and Codex's evidence scan.
- Produces: confirmation semantics and success/failure delivery allowlists containing `.prompt-evals/<prompt-id>/coverage-obligations.yaml`.
- Does not produce: a manifest hash field, confirmation file, coverage-summary asset, or new CLI command.

- [ ] **Step 1: Add failing delivery and instruction tests**

```python
def test_success_and_failure_allowlists_include_coverage_obligations(completed_cycle):
    relative = f".prompt-evals/{completed_cycle.prompt_id}/coverage-obligations.yaml"
    success = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST, result="success")
    failure = build_delivery_patch(completed_cycle, FAILURE_ALLOWLIST, result="failure")
    assert relative in success.paths
    assert relative in failure.paths
```

Add this instruction-order assertion:

```python
def test_tune_separates_mechanical_coverage_from_human_saturation(skill_text):
    tune = skill_text.split("## `verify`", 1)[0]
    mechanical = tune.index("mechanical coverage")
    saturation = tune.index("saturation statement")
    confirmation = tune.index("user confirmation")
    probe = tune.index("model probe")
    assert mechanical < saturation < confirmation < probe
    assert "invalidate" in tune
    assert "coverage_obligations_hash" in tune
    assert "run manifest binds" not in tune
```

- [ ] **Step 2: Run focused tests and verify failure**

Run: `rtk pytest tests/test_manage_worktree.py tests/test_skill_instructions.py -k "coverage_obligations or saturation" -q`

Expected: FAIL because the asset is not allowlisted or documented.

- [ ] **Step 3: Add the asset to canonical delivery sets**

Add `"coverage-obligations.yaml"` to both `SUCCESS_ALLOWLIST` through its base set and `_ASSET_NAMES`; `FAILURE_ALLOWLIST` continues to be `SUCCESS_ALLOWLIST - {"prompt"}`. Update `completed_cycle` to write and commit the file before patch generation.

- [ ] **Step 4: Document the two-layer confirmation gate**

Update the skill and references with this exact ownership:

```text
validate_cases.py:
  validates schemas, counts, hard duplicates, explained near duplicates,
  quotas, category declarations, critical coverage, and matrix completeness

Codex + user:
  review scanned evidence, unregistered evidenced boundaries, near-duplicate
  distinctions, total call slots, and the saturation statement
```

State that the confirmation record binds `coverage_obligations_hash` and `case_suite_hash`; it remains cycle state, not a project asset or CLI input. Any frozen asset change invalidates confirmation and all old runs. Do not state that a run manifest binds the obligation hash.

- [ ] **Step 5: Run delivery and documentation tests**

Run: `rtk pytest tests/test_manage_worktree.py tests/test_skill_structure.py tests/test_skill_instructions.py -q`

Expected: PASS.

- [ ] **Step 6: Commit freeze and delivery behavior**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py SKILL.md references/business-contract.md references/case-schema.md references/evaluation-method.md tests/test_skill_instructions.py
rtk git commit -m "docs: freeze and deliver coverage obligations"
```

---

### Task 6: Coverage-aware integration workflow

**Files:**
- Modify: `tests/integration_support.py`
- Modify: `tests/test_integration_tune.py`
- Modify: `tests/test_behavior_contract.py`
- Modify: `tests/test_integration_verify.py`

**Interfaces:**
- Consumes: all coverage validation and delivery behavior from Tasks 1-5.
- Produces: deterministic offline evidence for pre-model failure, actual slot counts, confirmation invalidation, acceptance isolation, and delivery.

- [ ] **Step 1: Build deterministic 30-case split fixtures**

Use `make_case(split, index, obligation="classify-input", variant="normal")` from Task 2 to write exactly 30 distinct cases per split and a complete obligation document. Vary `condition_id`, semantic family, and input by split and index; do not create wording-only variants. Keep fake expected objects compatible with the production fixture Schema.

Add this shared integration helper to `tests/integration_support.py`:

```python
def remove_last_case(path: Path) -> None:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump(cases[:-1], sort_keys=False), encoding="utf-8")
```

- [ ] **Step 2: Add failing integration assertions**

```python
def test_tune_stops_before_transport_when_mechanical_coverage_fails(target_repo):
    remove_last_case(target_repo / ".prompt-evals" / "classify--abc123" / "validation-cases.yaml")
    transport = CountingTransport()
    result = run_tune_with_fake_transport(target_repo, transport=transport)
    assert result.stop_reason == "setup_error"
    assert transport.calls == 0


def test_actual_case_counts_drive_fixed_repeat_slots(target_repo):
    result = run_tune_with_fake_transport(target_repo, scenario="no-change")
    assert len(result.baseline_dev.slots) == 30 * 5
    assert len(result.baseline_validation.slots) == 30 * 5
```

Implement four named integration cases: `test_changed_coverage_hash_invalidates_confirmation_and_baseline`, `test_acceptance_remains_unread_before_candidate_freeze_with_coverage`, `test_explained_near_duplicate_requires_confirmation_before_probe`, and `test_delivery_contains_coverage_obligations_for_both_results` parameterized over `success|failure`. The first three assert `transport.calls == 0` before renewed confirmation. The delivery test asserts the canonical `.prompt-evals/<prompt-id>/coverage-obligations.yaml` path is present and the production prompt remains absent for `failure`.

- [ ] **Step 3: Run the integration tests and verify failure**

Run: `rtk pytest tests/test_integration_tune.py tests/test_behavior_contract.py tests/test_integration_verify.py -q`

Expected: FAIL until the integration harness builds and confirms coverage-aware assets.

- [ ] **Step 4: Update the fake tune harness**

Make the harness execute this order without adding a production orchestrator:

```text
write obligations and 30+ cases
-> validate CASE_SUITE_JSON mechanical gates
-> record evidence_checked and saturation_statement
-> simulate explicit user confirmation bound to both hashes
-> probe fake transport
-> asset commit
-> run existing baseline/candidate/acceptance lifecycle
```

Compute displayed slot estimates from actual `D`, `V`, and `A`: baseline `5D + 5V`, each promoted full round `5D + 5V + affected-dev pre-run slots`, and paired acceptance `10A + 10A`.

- [ ] **Step 5: Run all integration and behavior tests**

Run: `rtk pytest tests/test_integration_tune.py tests/test_integration_verify.py tests/test_behavior_contract.py -q`

Expected: PASS with fake transport only.

- [ ] **Step 6: Run the complete offline suite**

Run: `rtk pytest -q`

Expected: PASS.

- [ ] **Step 7: Commit integration coverage**

```bash
rtk git add tests/integration_support.py tests/test_integration_tune.py tests/test_integration_verify.py tests/test_behavior_contract.py
rtk git commit -m "test: cover evidence-driven evaluation workflow"
```
