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
- symbolic `"prompt"` allowlist resolution through the canonical, committed
  `prompt-contract.yaml` path, never as a literal path or an uncommitted
  contract fallback;
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

## Fix round 1

Addressed the delivery review findings with regression coverage for:

- complete fail-closed Git patch grammar (required section headers and hunks,
  quoted/unquoted paths with spaces, and rejection of binary, rename, copy,
  malformed, duplicate, and unsafe structures);
- canonical allowlist derivation from cycle metadata and result, with exact
  persisted-manifest consistency checks;
- failure deliveries rejecting the canonical Prompt even when named by its
  explicit path;
- rejection of deletion deliveries and mandatory destination existence/hash
  verification;
- worktree paths that are outside the original repository using Windows-safe
  case normalization and path-boundary checks.

The delivery parser now validates every patch section before `git apply`, and
the application path snapshots exact targets and restores them after any
application or destination-verification failure.

## Fix round 2

Closed the remaining critical trust-boundary issue.  `preflight_patch(...)` and
`apply_delivery_patch(...)` now regenerate a canonical patch and manifest from
the trusted cycle metadata, immutable `cycle_base_commit`, current cycle
worktree HEAD/content, and the Ruling 1 allowlist.  The persisted patch text,
paths, source hashes, destination hashes, Prompt path, allowlist paths, and
commit identities must match the regenerated values item-for-item; persisted
delivery fields are never used as the source of truth for Git application.

The checks fail closed when the original HEAD, cycle worktree HEAD/branch,
committed worktree target, or committed Prompt contract changes.  Source and
worktree checks are repeated adjacent to application and after application,
while exact target snapshots and destination hashes retain rollback behavior.
Regression coverage includes a complete legal allowlisted patch with forged
text and hashes, changed/unstaged cycle targets, worktree-adjacent TOCTOU, and
an uncommitted `prompt-contract.yaml` fallback.  Final delivery therefore
requires the contract to have been generated, confirmed, and committed before
the cycle's immutable base.

## Fix round 3

Re-aligned Task 7 with Ruling 6's local single-user, non-adversarial threat
model. Delivery now treats the cycle base, current committed worktree HEAD,
and committed target content as the trusted state and regenerates the patch at
delivery time. Git-reported changed paths are checked against the derived
result allowlist and the generated patch path set; an extra patch section or
file is rejected. Immediately before apply, the original `HEAD` and committed
canonical Prompt path are rechecked; only non-path contract fields such as the
current Prompt hash may change. The apply sequence remains `git apply --check`,
exact-target snapshot, unstaged apply, actual path/destination-hash validation,
and rollback on any failure.

The fix removes adversarial-only persisted patch/hash/manifest trust machinery
and the custom complete patch grammar. It retains low-cost correctness
coverage for allowlist paths, deletion, failure Prompt exclusion, worktree
boundaries, conflicts, rollback, and unstaged/uncommitted delivery. No real
credential or Authorization value was read, stored, or committed.
