# Task 6 report — exact scoring, comparison, and phase gates

## TDD evidence

The initial Task 6 focused run was RED because the scoring modules did not
exist:

```text
rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py -v
2 collection errors: ModuleNotFoundError for scripts.score_results and scripts.compare_runs
```

Additional boundary cases were kept RED before each corresponding fix:

- a compatibility error initially allowed a perfect gate;
- a Pydantic private-state mismatch exposed disagreement between online
  serialized comparison and persisted scoring;
- canonical persisted names for an alias-only Schema initially could not be
  reconstructed;
- one-sided case evidence initially did not reject comparison.

Each case was fixed and rerun GREEN. The final focused result is:

```text
rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py -q
23 passed
```

## Implementation

- `score_results.py` consumes only completed `RunManifest` evidence, resolves
  and verifies the recorded production Schema import reference, reconstructs
  complete expected/actual Pydantic objects, compares canonical declared
  fields, and emits deterministic recursive field diffs.
- Scoring counts only `parse_error`, `schema_error`, `business_error`, and
  `pass`; incomplete, setup, transport, protocol, unknown, or malformed
  evidence raises `ScoreError` without final metrics.
- `schema_valid_rate`, `run_accuracy`, and `stable_case_rate` use Decimal
  arithmetic. Case-level classifications and diffs remain available for
  traceability.
- `compare_runs.py` compares compatible manifests while allowing only prompt
  hash and timestamps to differ. It does not load case files, including the
  acceptance dataset.
- Development, validation, and acceptance gates enforce the shared schema,
  critical-case, normal-case, regression, and stability requirements; only
  validation requires strict core improvement, while acceptance allows equal
  perfect metrics.
- The CLIs write sanitized JSON reports and never construct a model client or
  read a real Token.

## Verification

```text
rtk conda run -n kds python -m pytest tests -v
91 passed
rtk git diff --check
clean
```

The implementation and tests were committed as `41ce11b829f4b9ada41b5724bad84e165def6dc8`.
This report is the follow-up documentation commit for Task 6.

## Fix round 1 — reviewer regressions

The first review identified four safety/contract gaps.  RED regressions were
added before the corresponding implementation changes:

- adapter-local Schemas were persisted as generated module names that could
  not be imported by a fresh process;
- phase gates accepted 4/4 development or validation repeats and 9/9
  acceptance repeats without proving the required 5/5 and 10/10 plans;
- Pydantic equality and canonical field-diff behavior needed an explicit runtime-state boundary;
- completed comparisons allowed missing phase identity, including a missing
  dataset.

The fix stores adapter Schema references as a versioned adapter-file path,
qualname, and SHA-256 content identity.  Scoring and comparison validate the
adapter filename, manifest/prompt boundary, content hash, and safe qualname
before loading it, so persisted score and compare CLIs reconstruct the Schema
in a new process without treating a manifest as an arbitrary import request.
The runner defaults development/validation to exactly 5 repeats and
acceptance to exactly 10; comparison rejects manifests with another count,
and gates retain the 4/5 and 9/10 pass thresholds.  The later ruling-5 fix
defines equality and diffs only over canonical production Schema fields; private
attributes, caches, and other runtime-only state are neither persisted nor scored.
Comparisons also fail closed for missing dataset, cycle, prompt, Schema,
prompt hash, slot, case, or client identity evidence and enforce phase/dataset
isolation.

Fix-round verification:

```text
rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py tests/test_run_prompt_eval.py -q
61 passed
rtk conda run -n kds python -m pytest tests -q
104 passed
rtk git diff --check
clean
```

## Fix round 2 — ruling 5 canonical field semantics

The ruling-5 regression replaces Pydantic's default model equality with a
comparison of canonical serialization restricted to the production Schema's
declared fields.  The runner and persisted scorer now use the same canonical
field-name representation, so differing `PrivateAttr` state cannot become a
`business_error` or an artificial runtime-state diff.  Declared-field changes
still produce deterministic, complete nested paths and values.  The misleading
runtime-state implementation and regression were removed.

Fix-round-2 verification:

```text
rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py tests/test_run_prompt_eval.py -q
62 passed
rtk conda run -n kds python -m pytest tests -q
105 passed
rtk git diff --check
clean
```
