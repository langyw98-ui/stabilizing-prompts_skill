# Final review fix report — coverage-driven cases

Baseline: `af038e4bb84769d3041351b3f559815bcc6c926b`

Implementation commit: `e4d14a836555c128800db61bd1c821ddc8f1f15f`
(`fix: close coverage audit review gaps`)

## Fixes

- Required categories now need both an evidence-backed obligation and observed
  eligible primary coverage. The audit emits deterministic category rows and an
  obligation/split/variant coverage matrix. Categories that are absent must be
  explicitly `not_applicable` with evidence and rationale. The integration
  fixture now assigns substantive catalog boundaries to required categories and
  marks only unsupported categories not applicable.
- `CASE_SUITE_JSON` now includes category applicability/evidence/rationale,
  explicit `coverage_matrix`, and deterministic structured input/scenario
  conflicts. Invalid duplicate suites remain rejected; CLI errors carry
  `duplicates.hard` and conflict details instead of only a generic message.
- A shared recursive canonical serializer sorts set/frozenset values, retains
  ordered sequence order, normalizes production expected objects, and is used
  for input fingerprints and suite hashes. Subprocess tests cover multiple
  `PYTHONHASHSEED` values, including a Pydantic set field.
- The CLI validates coverage obligations and repository identity before schema
  import. Repository-root lookup fails closed unless a test explicitly injects
  `repo_root`.
- `_split_paths` rejects unordered sets; the skill/docs distinguish exact raw
  per-file hashes from canonical `case_suite_hash`; and the dangling `SKILL.md`
  sentence was corrected.

## Verification

All commands were run from the implementation worktree with the pinned `kds`
environment.

```text
rtk conda run -n kds python -m pytest tests/test_validate_cases.py tests/test_skill_instructions.py tests/test_skill_structure.py -q
..............s......................................................... [ 75%]
........................                                                 [100%]
95 passed, 1 skipped in 19.01s

rtk conda run -n kds python -m ruff check scripts/validate_cases.py tests/test_validate_cases.py tests/integration_support.py
All checks passed!

rtk git diff --check
(no output; exit 0)

rtk conda run -n kds python -m pytest -q
........................................................................ [ 25%]
........................................................................ [ 50%]
.............................................................s.......... [ 75%]
.......................................................................  [100%]
286 passed, 1 skipped in 402.87s (0:06:42)
```

The pre-existing untracked `scripts/__pycache__/` and `tests/__pycache__/`
directories were preserved and not staged.

## Final review fix report — Round 2

Round 2 baseline: `e4d14a836555c128800db61bd1c821ddc8f1f15f`

Implementation commit: `24df1b60882dc22468ebe5cdfe940420fb37ac30`
(`fix: harden canonical serialization and conflict diagnostics`)

### Fixes

- The shared recursive serializer now pairs each production model's
  `model_dump(mode="python")` container tree with its
  `model_dump(mode="json")` values. Unordered set/frozenset members are
  canonicalized and sorted by JSON representation, while list/tuple order is
  retained. Pydantic-core conversion covers Decimal, UUID, bytes, date/time,
  enum, path, and other supported JSON scalar encodings. The same serializer
  is used by expected normalization and the canonical suite hash.
- Structured input conflict diagnostics now expose only SHA-256 fingerprints,
  stable split/case references, and conflict kinds. Raw canonical input JSON is
  retained only in the in-memory duplicate index and never enters
  `CASE_SUITE_JSON`; semantic-family diagnostics no longer echo the authored
  family value. Regression coverage uses nested password, token, secret, and
  access-token values and verifies useful conflict identity remains.

### Verification

All commands were run from the implementation worktree with the pinned `kds`
environment.

```text
rtk conda run -n kds python -m pytest tests/test_validate_cases.py -q -k "redacts_structured_conflict or preserves_pydantic_json_scalars"
..                                                                       [100%]
2 passed, 75 deselected in 1.66s

rtk conda run -n kds python -m pytest tests/test_validate_cases.py -q
..............s......................................................... [ 93%]
.....                                                                    [100%]
76 passed, 1 skipped in 15.96s

rtk conda run -n kds python -m pytest tests/test_integration_tune.py tests/test_integration_verify.py -q
..............................                                           [100%]
30 passed in 190.11s (0:03:10)

rtk conda run -n kds python -m pytest tests/test_validate_cases.py tests/test_integration_tune.py tests/test_integration_verify.py -q
..............s......................................................... [ 67%]
...................................                                      [100%]
106 passed, 1 skipped in 204.39s (0:03:24)

rtk conda run -n kds python -m pytest tests/test_skill_instructions.py tests/test_skill_structure.py -q
.....................                                                    [100%]
21 passed in 0.07s

rtk conda run -n kds ruff check scripts/validate_cases.py tests/test_validate_cases.py
All checks passed!

rtk git diff --check
(no output; exit 0)

rtk conda run -n kds python -m pytest -q
........................................................................ [ 24%]
........................................................................ [ 49%]
.............................................................s.......... [ 74%]
........................................................................ [ 99%]
.                                                                        [100%]
288 passed, 1 skipped in 398.31s (0:06:38)
```

The pre-existing untracked `scripts/__pycache__/` and `tests/__pycache__/`
directories were preserved and not staged in Round 2.
