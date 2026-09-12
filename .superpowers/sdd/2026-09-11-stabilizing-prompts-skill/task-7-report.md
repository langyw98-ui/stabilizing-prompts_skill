# Task 7 Report: Worktree Cycle and Safe Delivery

## RED

Added `tests/test_manage_worktree.py` before the implementation and ran:

```text
rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -v
```

The focused run failed during collection with `ModuleNotFoundError` because
`scripts.manage_worktree` did not yet exist.

The implementation also exposed a Windows clean/smudge line-ending boundary:
the first GREEN attempt compared Git blob hashes (LF) with checked-out
worktree bytes (CRLF), so delivery tests failed with a false source conflict.
The source-hash tests drove the fix to verify Git base identity first and then
record/verify exact checked-out bytes.

An additional regression test was written for a tampered patch manifest that
adds an unallowlisted path.  It initially failed with a generic patch-content
error, then drove explicit persisted allowlist-path validation.

## GREEN

Implemented `scripts/manage_worktree.py` with:

- immutable `WorktreeCycle` state and `create_cycle(...)` rooted at the
  original workspace `HEAD`;
- symbolic `"prompt"` allowlist resolution through the canonical
  `prompt-contract.yaml` path (with a single changed-Markdown fallback only
  when no contract exists), never as a literal path;
- success and failure allowlists, with failure delivery excluding the
  production Prompt;
- base-to-final-commit binary patch generation and path filtering that keeps
  `.runtime/`, `reports/`, and unrelated files out of delivery;
- persisted source/destination hashes and allowlist metadata;
- fail-safe `preflight_patch(...)` checks for cycle identity, path scope,
  source hashes, staged target edits, and `git apply --check`;
- unstaged `git apply` with exact-target snapshots, destination hash
  verification, and rollback after any application or verification error;
- state/patch CLI artifact helpers; no automatic worktree or branch cleanup.

Focused verification:

```text
rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -q
11 passed

rtk conda run -n kds python -m py_compile scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git diff --check
clean
```

Final complete repository verification:

```text
rtk conda run -n kds python -m pytest tests -q
116 passed in 28.17s
```

## Files

- `scripts/manage_worktree.py`
- `tests/test_manage_worktree.py`
- this report

No real credential or Authorization value was read, stored, or committed.
