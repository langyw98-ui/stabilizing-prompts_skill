# Final fix report: project-local worktree

Date: 2026-09-14

## Changes

- `scripts/manage_worktree.py`: removed target `.gitignore` from delivery allowlists; rejected managed-root violations, external/legacy state, and link/reparse-point aliases during load and delivery; converted NUL branch failures and existing managed-root files to `WorktreeError`; rejected branch namespace collisions before creating `.worktrees/`.
- `SKILL.md`, `references/worktree-lifecycle.md`: require the user to establish a `.worktrees/` ignore rule before `tune`; explicitly prohibit editing, staging, or committing the target `.gitignore`; document managed-root delivery rejection without migration.
- `tests/fixtures/target_repo/.gitignore`, `tests/integration_support.py`, `tests/test_manage_worktree.py`, `tests/test_behavior_contract.py`, `tests/test_integration_tune.py`, `tests/test_skill_instructions.py`: model user-owned ignore setup, narrow snapshots to the managed subtree, add sibling visibility coverage, and add regressions for all final-review findings.

## Verification

Focused final-review tests:

```text
rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -k "gitignore or loaded_cycle or external_worktree or symlink_alias or stabilizing_prompts_file or nul_custom_branch or namespace_collision" -q
9 passed, 39 deselected in 13.41s

rtk conda run -n kds python -m pytest tests/test_skill_instructions.py -k "ignore_setup or gitignore" -q
2 passed, 10 deselected in 0.02s

rtk conda run -n kds python -m pytest tests/test_integration_tune.py -k "workspace_snapshot" -q
1 passed, 12 deselected in 1.82s
```

Combined worktree, integration, behavior, and instruction suite:

```text
rtk conda run -n kds python -m pytest tests/test_manage_worktree.py tests/test_integration_tune.py tests/test_behavior_contract.py tests/test_skill_instructions.py -q
79 passed in 174.63s (0:02:54)
```

Full offline suite:

```text
rtk conda run -n kds python -m pytest -q
205 passed in 206.12s (0:03:26)
```

`rtk git diff --check` completed with exit code 0 and no output.

## Commits

- Implementation and regression fixes: `3737a7595642ba4136a5b13f51afee5a2d6a5fc5` (`fix: harden project-local worktree delivery`).
- This report is a documentation-only follow-up commit; its SHA is included in the handoff after it is committed.

## Concerns

- Legacy or externally located cycle state is intentionally rejected; no migration path is provided.
- Test fixtures establish `.worktrees/` in `.git/info/exclude` to model the required user precondition. Production code never edits, stages, or commits the target `.gitignore`.
- Existing unrelated `scripts/__pycache__/` and `tests/__pycache__/` directories were preserved.
