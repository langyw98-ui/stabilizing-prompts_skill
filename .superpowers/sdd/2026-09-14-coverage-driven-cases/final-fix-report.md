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
