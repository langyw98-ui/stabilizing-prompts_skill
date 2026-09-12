# Task 8 Report: Complete Skill Workflow and CLI Contracts

## Scope

Task 8 turns the initial Skill shell into a complete, human-gated `tune`
state machine and a read-only `verify` workflow. The documentation consumes
the Task 2–7 script boundaries and keeps model execution and credentials out
of the implementation and tests.

## TDD evidence

Added `tests/test_skill_instructions.py` before expanding `SKILL.md`. The
required focused run was intentionally RED against the initial shell:

```text
rtk conda run -n kds python -m pytest tests/test_skill_structure.py tests/test_skill_instructions.py -v
9 failed, 2 passed
```

The failures covered the missing acceptance guard, state sequence, CLI
contracts, confirmation ordering, cycle/worktree continuity, phase isolation,
delivery outcomes, and read-only verification policy. After the workflow and
reference changes, the same focused run is GREEN:

```text
rtk conda run -n kds python -m pytest tests/test_skill_structure.py tests/test_skill_instructions.py -v
13 passed
```

## Implemented behavior

- `SKILL.md` documents the exact sequence
  `preflight → worktree → contract/cases/adapter → user confirmation → model
  probe/smoke → asset commit → dev/validation baseline → no-change exit or
  candidate loop → candidate freeze → single acceptance activity → failure
  exit or delivery confirmation → worktree commit → allowlisted
  synchronization`.
- Missing-asset `bootstrap` is explicitly internal to `tune` and stays in the
  same cycle/worktree through baseline and candidate work.
- Contract/cases/adapter confirmation precedes all model calls. The fixed
  local model probe is post-confirmation, development-only, and uses the
  Skill-local credential boundary without embedding a credential.
- Development, validation, and acceptance ownership is explicit. Acceptance
  runs once per cycle after candidate hash freeze; `no_change_needed` skips
  candidate creation and acceptance.
- Candidate iteration stops at five rounds, after two consecutive rounds with
  no improvement, or on contract conflict, adapter distortion, model failure,
  incomplete evidence, or other stated stop reasons.
- Both successful production delivery and failure-asset-only synchronization
  require an explicit delivery confirmation. Failure delivery excludes the
  production Prompt and candidate.
- Ruling 6's local single-user/non-adversarial delivery boundary is documented:
  fresh patch generation, exact Git path checks, canonical Prompt binding,
  `git apply --check`, exact snapshots, unstaged apply, hash verification, and
  rollback, without adversarial trust machinery or automatic cleanup.
- `verify` rejects `--dataset acceptance` before loading files or constructing
  the client, never reads acceptance cases, and only writes ignored reports or
  caches.
- All required `validate_workspace.py`, `validate_cases.py`,
  `run_prompt_eval.py`, `score_results.py`, `compare_runs.py`, and
  `manage_worktree.py` command shapes are included verbatim.

The five references now carry the corresponding contract, split, adapter,
scoring, report, cycle, and delivery details instead of leaving the states
implicit in `SKILL.md`.

## Verification

```text
rtk conda run -n kds python -m pytest tests -q
137 passed in 68.30s

rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
Skill is valid!

rtk conda run -n kds python -m ruff check tests/test_skill_structure.py tests/test_skill_instructions.py
All checks passed!

rtk conda run -n kds python -m compileall -q scripts tests
passed

rtk git diff --check
clean
```

The repository-wide Ruff check still reports eight pre-existing findings in
Task 4/5 files (`scripts/local_model_client.py`,
`scripts/run_prompt_eval.py`, and `tests/test_run_prompt_eval.py`); Task 8 did
not modify those files. No real model call was made and no real Token was read
or stored.

## Fix round 1: executable CLI and mode boundary

The validation scripts now expose the documented `argparse` interfaces,
`main()` functions, and `__main__` entry points. They reuse the existing
validation functions, write stable JSON snapshots/suite summaries, redact
credential-shaped output, and return exit code 2 for setup or validation
errors. Help and unknown-argument behavior is delegated to argparse.

`run_prompt_eval.py` now accepts `--mode tune|verify` (defaulting to `tune` to
preserve existing callers), persists the mode in manifests, and rejects
`--mode verify --dataset acceptance` immediately after argument parsing. The
guard runs before manifest/case reads, adapter loading, client construction,
or model invocation; acceptance remains available to the upper `tune` state
machine. SKILL.md and the runner references include the same optional mode
argument and explicit ownership rule.

The fix-round regression coverage includes direct `main()` success/failure
checks, subprocess help/unknown-argument probes for all three CLIs, and a
mocked early-rejection test proving that verify acceptance does not touch case,
adapter, or client boundaries. No real model call or credential read was
performed.

Fix-round verification:

```text
rtk conda run -n kds python -m pytest tests/test_validate_workspace.py tests/test_validate_cases.py tests/test_run_prompt_eval.py -q
65 passed

rtk conda run -n kds python -m pytest tests -q
146 passed in 72.51s

rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
Skill is valid!

rtk git diff --check
clean
```

## Commit

Implementation commit: `d859968fe44ae32074ac5372cd2e8a58499c2ebf`
(`feat: define stabilizing prompts workflow`). This report is recorded in a
follow-up documentation commit.
