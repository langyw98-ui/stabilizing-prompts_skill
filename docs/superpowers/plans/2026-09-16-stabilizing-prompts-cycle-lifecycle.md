# Stabilizing Prompts Cycle Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement automatic repository-local excludes, unified scored-result finalization, result-aware delivery, and recoverable cleanup for every new `stabilizing-prompts` tuning cycle.

**Architecture:** Keep `scripts/manage_worktree.py` as the sole owner of Git identity, atomic cycle state, delivery, rollback, and exact cleanup. Add `scripts/evaluation_summary.py` for pure result/evidence normalization and deterministic Markdown rendering, and `scripts/finalize_cycle.py` for compact history, prepared/delivery commits, and confirmation-independent finalization. The Skill remains the user-facing state machine; production helpers mechanize every path, while the integration harness proves the full lifecycle without a real model service.

**Tech Stack:** Python 3.14, standard library, PyYAML, Git CLI, pytest

**Spec:** `docs/superpowers/specs/2026-09-16-stabilizing-prompts-cycle-lifecycle-consolidated-design.md`

## Global Constraints

- `tune` starts only from a primary workspace on a non-detached branch and manages one canonical repository-relative Markdown Prompt.
- Managed worktrees remain direct children of `<repo>/.worktrees/stabilizing-prompts/`; no caller-selected worktree path is accepted.
- Automatically modify only Git's resolved repository-local exclude file; never modify or deliver project `.gitignore`, `.git/config`, global excludes, or global Git configuration.
- Exclude initialization is byte-preserving, idempotent, atomic, and limited to one optimistic recomputation after concurrent drift.
- The pre-create worktree ignore gate and post-create `reports/`/`.runtime/` gates run before business confirmation, evaluation assets, or model calls.
- `verify` stays read-only and fails before output/model work when its runtime paths are not ignored.
- Every scored terminal outcome produces one deterministic Markdown summary and appends compact history before `prepared_commit`.
- Only acceptance-pass delivery may modify the production Prompt; every other formal result is asset-only.
- Delivery remains fresh-generated from `cycle_base_commit` and committed cycle content, allowlisted, unstaged, snapshot-protected, verified, and precisely rolled back on failure.
- A failed atomic delivery-state update is a delivery failure: restore the pre-apply snapshot and never start cleanup.
- Cleanup starts only after explicit combined delivery/cleanup confirmation and verified delivery; it never uses `git worktree remove --force`, `git worktree prune`, reflog expiry, or Git garbage collection.
- Cleanup deletes only the recorded managed worktree, its cycle-created local branch, the empty managed parent when applicable, and the exact `STATE_PATH`.
- Cleanup saves each successful destructive step atomically; retry uses saved progress and current postconditions rather than reapplying first-start existence checks.
- Old states missing current ownership/finalization/cleanup fields are never migrated or automatically cleaned.
- Runtime reports, `.runtime/`, raw responses, manifests, credentials, tokens, temporary patches, and `__pycache__` never enter prepared/delivery commits or patches.
- All repository commands are run through `rtk`.
- Every Python command runs through `rtk conda run -n kds python`; do not use the ambient Python, `.venv`, uv, Poetry, or another environment.
- If the plan is executed with subagents, the primary orchestrator must first obtain the required run-level confirmation; every implementer, reviewer, fix agent, and final reviewer must be spawned explicitly with `model: "gpt-5.6-luna"`, `reasoning_effort: "max"`, and non-`all` fork context.

---

### Task 1: Versioned atomic cycle state and branch ownership

**Files:**
- Modify: `scripts/manage_worktree.py:433-493,560-644,1421-1438`
- Modify: `tests/test_manage_worktree.py:103-194,227-283,438-486,897-947`

**Interfaces:**
- Consumes: existing `WorktreeCycle`, `_resolved_git_directory()`, `_validate_managed_worktree_path()`, and `create_cycle()`.
- Produces: `CleanupProgress`, `FinalizationState`, current-state `WorktreeCycle` fields, `save_cycle_atomic(cycle: WorktreeCycle, path: Path) -> None`, and `load_cycle(path: Path, *, require_current: bool = False) -> WorktreeCycle`.
- Preserves: `save_cycle()` as a compatibility alias for `save_cycle_atomic()` and existing delivery APIs that consume `WorktreeCycle`.

- [ ] **Step 1: Add failing serialization, ownership, and atomic-write tests**

```python
def test_new_cycle_records_branch_ownership(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    generated = create_cycle(original, prompt_id)
    assert generated.state_version == 2
    assert generated.branch_ref == f"refs/heads/{generated.branch}"
    assert generated.branch_origin == "generated"
    assert generated.branch_created_by_cycle is True


def test_custom_cycle_records_custom_branch_ownership(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id, branch="custom/cycle")
    assert cycle.branch_ref == "refs/heads/custom/cycle"
    assert cycle.branch_origin == "custom"
    assert cycle.branch_created_by_cycle is True


def test_atomic_cycle_save_preserves_previous_bytes_on_replace_failure(
    completed_cycle: WorktreeCycle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "cycle.json"
    state.write_text('{"sentinel": true}\n', encoding="utf-8")
    monkeypatch.setattr(manage_worktree.os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(WorktreeError, match="replace failed"):
        save_cycle_atomic(completed_cycle, state)
    assert state.read_bytes() == b'{"sentinel": true}\n'
    assert list(tmp_path.glob(f".{state.name}.*.tmp")) == []


def test_cleanup_loader_rejects_legacy_state(tmp_path: Path) -> None:
    state = tmp_path / "legacy.json"
    state.write_text(json.dumps({"original_repo": "x", "worktree": "y", "branch": "z", "cycle_base_commit": "a"}), encoding="utf-8")
    with pytest.raises(WorktreeError, match="current lifecycle state"):
        load_cycle(state, require_current=True)
```

- [ ] **Step 2: Run the focused state tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -k "branch_ownership or atomic_cycle_save or cleanup_loader" -q`

Expected: FAIL because the new state types, ownership fields, atomic writer, and current-state gate do not exist.

- [ ] **Step 3: Add immutable lifecycle state types and round-trip validation**

```python
@dataclass(frozen=True, slots=True)
class CleanupProgress:
    authorized: bool = False
    worktree_removed: bool = False
    managed_parent_handled: bool = False
    branch_deleted: bool = False


@dataclass(frozen=True, slots=True)
class FinalizationState:
    result_kind: str
    stop_reason: str
    finished_at_utc: str
    summary_path: str
    delivery_profile: str
    prepared_commit: str
    frozen_candidate_path: str | None = None
    frozen_candidate_hash: str | None = None
    delivery_commit: str | None = None
    delivery_confirmed: bool = False
    delivery_applied: bool = False
    delivery_verified: bool = False
    cleanup: CleanupProgress = field(default_factory=CleanupProgress)
```

Extend `WorktreeCycle` with `state_version: int = 2`, `branch_ref: str | None`, `branch_origin: str | None`, `branch_created_by_cycle: bool`, `exclude_initialized: bool`, `precreate_ignore_verified: bool`, `runtime_ignores_verified: bool`, and `finalization: FinalizationState | None`. Serialize every field explicitly. `from_dict()` may load version-1 state for existing build/apply behavior, but `require_current=True` must require version 2, all branch ownership fields, and a syntactically valid `refs/heads/<branch-name>` ref. Add `_validate_state_path(cycle, path)` and call it before every save, load-for-mutation, and delete; the resolved state file must remain outside the managed cycle worktree.

- [ ] **Step 4: Implement atomic state persistence**

```python
def save_cycle_atomic(cycle: WorktreeCycle, path: Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(cycle.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorktreeError(f"unable to atomically save cycle state: {error}") from error
```

Set branch ownership only after `git worktree add -b` succeeds. Generated branches use `branch_origin="generated"`; caller-selected absent branches use `branch_origin="custom"`. Keep `branch_created_by_cycle=False` on every failure path before successful creation.

- [ ] **Step 5: Run state and existing create/load tests**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -k "cycle or branch or state" -q`

Expected: PASS, including existing legacy-path rejection and CLI state-save failure reporting.

- [ ] **Step 6: Commit the state foundation**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "feat: add atomic lifecycle state"
```

---

### Task 2: Repository-local exclude initialization and two-stage gates

**Files:**
- Modify: `scripts/manage_worktree.py:117-151,521-644,1463-1528`
- Modify: `scripts/validate_workspace.py`
- Modify: `tests/test_manage_worktree.py`
- Modify: `tests/test_integration_verify.py`
- Modify: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: atomic state and ownership fields from Task 1.
- Produces: `initialize_local_excludes(root: Path, prompt_id: str, worktree: Path) -> ExcludeResult`, `verify_runtime_ignores(cycle: WorktreeCycle) -> tuple[str, str]`, and CLI command `manage_worktree.py verify-ignores --state STATE_PATH`.
- Changes: `create_cycle(original_repo: Path, prompt_id: str, *, branch: str | None = None, prompt_path: Path | str | None = None, state_path: Path | None = None) -> WorktreeCycle`.
- Guarantees: `create_cycle()` initializes/checks before `git worktree add`; when `state_path` is supplied it persists the created identity before verifying both runtime paths, then records all three successful gates atomically.

- [ ] **Step 1: Replace the fixture's manual ignore setup with failing automatic-initialization tests**

```python
def test_create_cycle_initializes_repository_local_excludes(tmp_path: Path) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    (original / ".gitignore").write_text("", encoding="utf-8")
    git(original, "add", ".gitignore")
    git(original, "commit", "-m", "remove project ignore")
    cycle = create_cycle(original, prompt_id)
    exclude = Path(git(original, "rev-parse", "--git-path", "info/exclude"))
    if not exclude.is_absolute():
        exclude = original / exclude
    text = exclude.read_text(encoding="utf-8")
    assert "/.worktrees/stabilizing-prompts/" in text
    assert "/.prompt-evals/*/reports/" in text
    assert "/.prompt-evals/*/.runtime/" in text
    assert cycle.exclude_initialized is True
    assert cycle.precreate_ignore_verified is True
    assert cycle.runtime_ignores_verified is True


def test_postcreate_ignore_failure_retains_persisted_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    state = tmp_path / "cycle.json"
    monkeypatch.setattr(manage_worktree, "verify_runtime_ignores", lambda cycle: (_ for _ in ()).throw(WorktreeError("runtime path is not ignored")))
    with pytest.raises(WorktreeError, match="runtime path is not ignored"):
        create_cycle(original, prompt_id, state_path=state)
    retained = load_cycle(state, require_current=True)
    assert retained.worktree.is_dir()
    assert retained.branch_created_by_cycle is True
    assert retained.runtime_ignores_verified is False
```

Add named tests for byte preservation without a trailing newline, second-call byte identity, partially covered rules, an already-sufficient wider rule, project-level negation, a submodule repository, a path containing spaces, first drift followed by successful recomputation, another initializer completing the rules, second drift failing closed, replace failure, and precise temporary-file cleanup.

- [ ] **Step 2: Run exclude tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -k "local_exclude or runtime_ignore or initializes_repository" -q`

Expected: FAIL because `create_cycle()` still requires pre-existing project ignore behavior.

- [ ] **Step 3: Implement resolved shared-metadata exclude access and bounded optimistic replacement**

```python
MANAGED_EXCLUDES = (
    "/.worktrees/stabilizing-prompts/",
    "/.prompt-evals/*/reports/",
    "/.prompt-evals/*/.runtime/",
)


@dataclass(frozen=True, slots=True)
class ExcludeResult:
    path: Path
    changed: bool
    verified_paths: tuple[str, ...]


def _repository_local_exclude(root: Path) -> Path:
    common = _resolved_git_directory(root, "--git-common-dir")
    value = Path(_git_text(root, "rev-parse", "--git-path", "info/exclude"))
    resolved = (value if value.is_absolute() else root / value).resolve(strict=False)
    if not _path_is_within(resolved, common):
        raise WorktreeError("repository-local exclude is outside shared Git metadata")
    return resolved
```

Implement one helper that computes missing rules from real `git check-ignore --no-index` results, constructs a byte-preserving candidate with the fixed managed comment, compares the original bytes again immediately before `os.replace`, and performs at most one full recomputation. Use a unique same-directory temporary file and exact `unlink`, never a glob.

- [ ] **Step 4: Integrate both gates into creation and expose the defensive recheck**

Before creating any directory/branch/state, initialize and verify the final derived worktree target. After `git worktree add`, construct the owned cycle and, when `state_path` is supplied, save it atomically before calling `verify_runtime_ignores()` from the linked worktree context for:

```text
.prompt-evals/<prompt-id>/reports/
.prompt-evals/<prompt-id>/.runtime/
```

On post-create failure, retain the already-persisted cycle with `runtime_ignores_verified=False` and report the exact state, worktree, branch, and failed path; never delete it automatically. The `create` CLI always supplies `--state`, while direct API callers used by tune integration must also pass it. Add `verify-ignores --state` for the post-confirmation defensive recheck; it updates the same state atomically only after both paths pass.

- [ ] **Step 5: Keep verify mode read-only**

Add an integration test that removes runtime ignore coverage, invokes verify, and asserts: nonzero result, no local-exclude byte change, no output file, and zero model calls. Modify `validate_workspace.py` only enough to query/report missing ignore behavior; do not call the initializer from verify mode.

- [ ] **Step 6: Run setup, verify, and ordering tests**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py tests/test_integration_verify.py tests/test_behavior_contract.py -k "ignore or exclude or before_model_call" -q`

Expected: PASS.

- [ ] **Step 7: Commit exclude initialization**

```bash
rtk git add scripts/manage_worktree.py scripts/validate_workspace.py tests/test_manage_worktree.py tests/test_integration_verify.py tests/test_behavior_contract.py
rtk git commit -m "feat: initialize repository local excludes"
```

---

### Task 3: Formal-result model and deterministic Markdown summaries

**Files:**
- Create: `scripts/evaluation_summary.py`
- Create: `tests/test_evaluation_summary.py`

**Interfaces:**
- Consumes: normalized JSON-compatible evidence already saved by scoring, comparison, coverage, smoke, and manifest steps.
- Produces: `FormalResult`, `SummaryEvidence`, `normalize_formal_result(kind: str, *, finished_at_utc: str, stop_reason: str | None = None) -> FormalResult`, `render_summary(result: FormalResult, evidence: SummaryEvidence) -> str`, and `write_summary(eval_root: Path, result: FormalResult, evidence: SummaryEvidence) -> Path`.

- [ ] **Step 1: Add failing result-matrix and sensitive-content tests**

```python
@pytest.mark.parametrize(
    ("kind", "profile", "prompt_changes", "acceptance_status"),
    [
        ("no_change_needed", "assets", False, "not_run"),
        ("no_strict_improvement", "assets", False, "not_run"),
        ("validation_failed", "assets", False, "not_run"),
        ("no_improvement_limit", "assets", False, "not_run"),
        ("round_limit", "assets", False, "not_run"),
        ("acceptance_failed", "assets", False, "failed"),
        ("acceptance_passed", "success", True, "passed"),
    ],
)
def test_formal_result_matrix(kind, profile, prompt_changes, acceptance_status):
    result = normalize_formal_result(kind, finished_at_utc="2026-09-16T08:09:10Z")
    assert result.delivery_profile == profile
    assert result.prompt_should_change is prompt_changes
    assert result.acceptance_status == acceptance_status


def test_summary_is_deterministic_and_redacted(sample_result, sample_evidence):
    first = render_summary(sample_result, sample_evidence)
    second = render_summary(sample_result, sample_evidence)
    assert first == second
    assert "Authorization" not in first
    assert "Bearer secret" not in first
    assert "raw response" not in first.casefold()
    assert "C:\\private\\worktree" not in first
    assert "prepared_commit" not in first
    assert "delivered" not in first.casefold()
```

Add tests for missing evidence, contradictory acceptance data, invalid finished timestamps, Markdown escaping, non-run phase explanations, existing output collision, and the exact path `evaluation-summaries/2026-09-16-080910-<result>.md`.

- [ ] **Step 2: Run the new test module and verify import failure**

Run: `rtk conda run -n kds python -m pytest tests/test_evaluation_summary.py -q`

Expected: FAIL because `scripts.evaluation_summary` does not exist.

- [ ] **Step 3: Implement strict immutable input types**

```python
FORMAL_KINDS = frozenset({
    "no_change_needed",
    "no_strict_improvement",
    "validation_failed",
    "no_improvement_limit",
    "round_limit",
    "acceptance_failed",
    "acceptance_passed",
})


@dataclass(frozen=True, slots=True)
class FormalResult:
    kind: str
    stop_reason: str
    finished_at_utc: str
    delivery_profile: str
    prompt_should_change: bool
    acceptance_status: str


@dataclass(frozen=True, slots=True)
class SummaryEvidence:
    cycle_id: str
    evidence_identity: Mapping[str, str]
    prompt_name: str
    prompt_path: str
    model_name: str
    case_counts: Mapping[str, int]
    repeats: Mapping[str, int]
    planned_calls: Mapping[str, int]
    completed_calls: Mapping[str, int]
    metrics: Mapping[str, object]
    comparisons: Mapping[str, object]
    coverage: Mapping[str, object]
    smoke_passed: bool
    failure_summaries: tuple[Mapping[str, object], ...]
```

Reject unknown fields, missing required fields, non-finite/nondeterministic values, acceptance evidence inconsistent with `acceptance_status`, and absolute prompt paths.

- [ ] **Step 4: Render the seven fixed sections and safe path**

Use a fixed section order matching Spec 7.3. Render only normalized evidence, sort mapping keys and failure summaries by stable identifiers, normalize line endings to `\n`, and escape user-controlled Markdown metacharacters. Refuse an existing destination instead of overwriting or renaming it.

- [ ] **Step 5: Run summary tests**

Run: `rtk conda run -n kds python -m pytest tests/test_evaluation_summary.py -q`

Expected: PASS.

- [ ] **Step 6: Commit deterministic summaries**

```bash
rtk git add scripts/evaluation_summary.py tests/test_evaluation_summary.py
rtk git commit -m "feat: render deterministic evaluation summaries"
```

---

### Task 4: Prepared commits, compact history, and delivery-commit resolution

**Files:**
- Create: `scripts/finalize_cycle.py`
- Create: `tests/test_finalize_cycle.py`
- Modify: `scripts/manage_worktree.py` exports used by finalization

**Interfaces:**
- Consumes: `FormalResult`, `SummaryEvidence`, current `WorktreeCycle`, atomic state persistence, and canonical Prompt identity.
- Produces: `prepare_finalization(state_path: Path, result: FormalResult, evidence: SummaryEvidence, *, frozen_candidate: Path | None = None, frozen_candidate_hash: str | None = None) -> WorktreeCycle` and `resolve_delivery_commit(state_path: Path, *, confirmed: bool) -> WorktreeCycle`.
- CLI: `finalize_cycle.py prepare --state STATE_PATH --result RESULT --finished-at UTC --evidence SUMMARY_JSON [--candidate FROZEN_CANDIDATE --candidate-hash SHA256]` and `finalize_cycle.py approve --state STATE_PATH`.
- Guarantees: every formal result appends exactly one compact history entry before `prepared_commit`; asset-only delivery reuses that commit; acceptance-pass creates one child commit without duplicating history.

- [ ] **Step 1: Add failing prepared/delivery commit tests**

```python
def test_asset_only_prepares_history_summary_and_reuses_commit(cycle_state: Path, evidence):
    result = normalize_formal_result("acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z")
    prepared = prepare_finalization(cycle_state, result, evidence)
    final = resolve_delivery_commit(cycle_state, confirmed=True)
    assert prepared.finalization is not None
    assert final.finalization is not None
    assert final.finalization.delivery_commit == prepared.finalization.prepared_commit
    history = yaml.safe_load((prepared.worktree / ".prompt-evals/classify--abc123/optimization-history.yaml").read_text(encoding="utf-8"))
    assert [entry["stop_reason"] for entry in history["cycles"]].count("acceptance_failed") == 1


def test_acceptance_pass_creates_one_child_without_duplicate_history(cycle_state: Path, evidence, candidate: Path):
    result = normalize_formal_result("acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z")
    prepared = prepare_finalization(
        cycle_state,
        result,
        evidence,
        frozen_candidate=candidate,
        frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)
    assert delivered.finalization is not None
    assert git(delivered.worktree, "rev-parse", f"{delivered.finalization.delivery_commit}^") == prepared.finalization.prepared_commit
    history_text = git(delivered.worktree, "show", f"{delivered.finalization.delivery_commit}:.prompt-evals/classify--abc123/optimization-history.yaml")
    assert history_text.count("acceptance_passed") == 1
```

Add tests for all seven formal results, declined/ambiguous confirmation preserving `HEAD`, candidate path outside exact `.runtime`, frozen-candidate hash mismatch, non-pass result with a candidate, contract path redirect, existing summary collision, and a second finalization attempt.

- [ ] **Step 2: Run finalization tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_finalize_cycle.py -q`

Expected: FAIL because the finalization module and APIs do not exist.

- [ ] **Step 3: Implement compact-history append and prepared commit**

```python
def compact_history_entry(result: FormalResult, evidence: SummaryEvidence) -> dict[str, object]:
    return {
        "finished_at_utc": result.finished_at_utc,
        "result": result.kind,
        "stop_reason": result.stop_reason,
        "failure_categories": sorted({str(item["category"]) for item in evidence.failure_summaries}),
    }
```

Load `optimization-history.yaml` as `{"cycles": []}` when absent, validate its mapping/list shape, reject an entry with the same finished time/result, append once, render the summary, stage only canonical deliverable evaluation assets, commit, resolve `HEAD^{commit}`, and atomically save a `FinalizationState` whose `prepared_commit` is that resolved commit. Acceptance-pass preparation must also validate and record the repository-relative runtime candidate path and SHA-256; every asset-only result must reject candidate inputs.

- [ ] **Step 4: Implement delivery confirmation resolution**

For asset-only results, affirmative confirmation sets `delivery_commit=prepared_commit` without a Git commit. For acceptance pass, validate the frozen candidate under the exact cycle `.runtime`, verify its recorded hash, replace the production Prompt, update only contract non-path fields, commit one child, and record its commit. Declined or ambiguous confirmation records no delivery commit and does not modify `HEAD`.

- [ ] **Step 5: Run finalization and existing allowlist tests**

Implement the CLI after the module tests pass. `prepare` requires evidence JSON under the exact
cycle `.runtime`; acceptance pass additionally requires the exact runtime candidate and its
SHA-256. `approve` is called only after an explicit affirmative user answer and atomically records
both `delivery_confirmed=True` and `cleanup.authorized=True` while resolving the delivery commit.
An ambiguous or negative answer does not call `approve` and leaves the prepared cycle unchanged.

Run: `rtk conda run -n kds python -m pytest tests/test_finalize_cycle.py tests/test_manage_worktree.py -k "finaliz or prepared or delivery_commit or allowlist" -q`

Expected: PASS.

- [ ] **Step 6: Commit finalization**

```bash
rtk git add scripts/finalize_cycle.py scripts/manage_worktree.py tests/test_finalize_cycle.py tests/test_manage_worktree.py
rtk git commit -m "feat: prepare formal cycle results"
```

---

### Task 5: Result-aware patching and transactional delivery-state updates

**Files:**
- Modify: `scripts/manage_worktree.py:31-62,646-775,1008-1420,1440-1535`
- Modify: `tests/test_manage_worktree.py:589-895`
- Modify: `tests/test_behavior_contract.py:20-44`

**Interfaces:**
- Consumes: `FinalizationState.delivery_profile`, `prepared_commit`, `delivery_commit`, and summary path from Tasks 3-4.
- Produces: dynamic summary allowlisting, `_require_runtime_output(cycle: WorktreeCycle, path: Path, label: str) -> Path`, and `apply_delivery_patch(cycle: WorktreeCycle, patch: DeliveryPatch | Path | str | None = None, *, expected_source_hashes: Mapping[str, object] | None = None, on_verified: Callable[[DeliveryPatch], None] | None = None) -> DeliveryPatch`.
- CLI behavior: `build-patch` derives its result/profile from state; `apply-patch` atomically records verified delivery before returning success.

- [ ] **Step 1: Add failing summary allowlist and runtime-output tests**

```python
def test_patch_allows_only_recorded_evaluation_summary(finalized_cycle: WorktreeCycle) -> None:
    patch = build_delivery_patch(finalized_cycle)
    assert finalized_cycle.finalization is not None
    assert finalized_cycle.finalization.summary_path in patch.paths
    assert all("reports/" not in path and "/.runtime/" not in path for path in patch.paths)


@pytest.mark.parametrize("name", ["outside.patch", "../escape.patch"])
def test_build_cli_rejects_output_outside_exact_runtime(finalized_state: Path, tmp_path: Path, name: str) -> None:
    code = manage_worktree.main(["build-patch", "--state", str(finalized_state), "--out", str(tmp_path / name), "--out-manifest", str(tmp_path / "manifest.json")])
    assert code == 2
```

Add tests that reject an unrecorded summary, another prompt ID, summary deletion, extra changed paths, wrong profile, `HEAD != delivery_commit`, staged/tracked/nonignored-untracked dirt, and persisted patch/manifest tampering.

- [ ] **Step 2: Add the failing post-apply state-save rollback test**

```python
def test_apply_rolls_back_when_verified_state_save_fails(
    finalized_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    patch = build_delivery_patch(cycle)
    before = snapshot_filesystem(cycle.original_repo)
    monkeypatch.setattr(manage_worktree, "save_cycle_atomic", lambda cycle, path: (_ for _ in ()).throw(WorktreeError("state replace failed")))
    with pytest.raises(WorktreeError, match="state replace failed"):
        apply_delivery_patch(cycle, patch, on_verified=lambda applied: save_cycle_atomic(cycle, finalized_state))
    assert snapshot_filesystem(cycle.original_repo) == before
```

- [ ] **Step 3: Run delivery tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py tests/test_behavior_contract.py -k "summary or runtime_output or state_save_fails" -q`

Expected: FAIL because summary paths are not allowlisted, output locations are unrestricted, and verified callbacks do not participate in rollback.

- [ ] **Step 4: Make canonical state authoritative for patch profile and paths**

Permit fixed asset names plus exactly `finalization.summary_path` under the current prompt ID. Require actual changed paths to equal the result-derived selected set, reject deletions, and require worktree `HEAD == delivery_commit`. Validate `--out` and `--out-manifest` as regular non-symlink paths whose resolved parents are the exact cycle `.prompt-evals/<prompt-id>/.runtime` directory.

- [ ] **Step 5: Put the state transition inside the snapshot rollback boundary**

After destination hash verification and before returning, invoke `on_verified(prepared)` inside the existing `try` that owns `_Snapshot` rollback. The CLI callback uses `dataclasses.replace()` to set `delivery_applied=True`, `delivery_verified=True`, and the already-confirmed cleanup authorization, then calls `save_cycle_atomic()`. Any callback exception restores all target snapshots, prints an error, and leaves cleanup disabled.

- [ ] **Step 6: Run all delivery tests**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py tests/test_behavior_contract.py -k "delivery or patch or allowlist or rollback" -q`

Expected: PASS.

- [ ] **Step 7: Commit result-aware transactional delivery**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py tests/test_behavior_contract.py
rtk git commit -m "feat: record verified result aware delivery"
```

---

### Task 6: Exact cleanup CLI with phase-aware retry

**Files:**
- Modify: `scripts/manage_worktree.py:85-247,866-937,1421-1556`
- Modify: `tests/test_manage_worktree.py`

**Interfaces:**
- Consumes: current atomic state with verified delivery and cleanup authorization.
- Produces: `cleanup_cycle(state_path: Path) -> CleanupOutcome` and CLI `manage_worktree.py cleanup --state STATE_PATH`.
- Guarantees: first-start gates apply only while `cleanup.worktree_removed is False`; retry validates saved completed steps and current postconditions before continuing.

- [ ] **Step 1: Add failing first-start cleanup tests**

```python
def test_cleanup_removes_only_exact_verified_cycle(delivered_state: Path) -> None:
    cycle = load_cycle(delivered_state, require_current=True)
    original = cycle.original_repo
    branch_ref = cycle.branch_ref
    assert branch_ref is not None
    outcome = cleanup_cycle(delivered_state)
    assert outcome.status == "complete"
    assert not cycle.worktree.exists()
    assert git(original, "show-ref", "--verify", branch_ref, check=False) == ""
    assert not delivered_state.exists()
    assert (original / ".worktrees").is_dir()


@pytest.mark.parametrize("field", ["authorized", "delivery_applied", "delivery_verified"])
def test_cleanup_rejects_missing_gate(delivered_state: Path, field: str) -> None:
    cycle = load_cycle(delivered_state, require_current=True)
    finalization = cycle.finalization
    assert finalization is not None
    cleanup = replace(finalization.cleanup, authorized=False) if field == "authorized" else finalization.cleanup
    changed = replace(finalization, cleanup=cleanup, **({field: False} if field != "authorized" else {}))
    save_cycle_atomic(replace(cycle, finalization=changed), delivered_state)
    with pytest.raises(WorktreeError, match="cleanup gate"):
        cleanup_cycle(delivered_state)
```

Add tests for dirty tracked, staged, and nonignored untracked files; ignored runtime files being removable; external/symlink worktree paths; ref/commit drift; a branch used by another worktree; current/default branch protection; remote refs remaining untouched; custom cycle-created branch deletion; legacy/missing ownership rejection; other managed cycles; nonempty managed parent; and preservation of `.worktrees/`.

- [ ] **Step 2: Add failing partial-failure retry tests**

```python
def test_cleanup_retry_after_worktree_removed_does_not_reapply_first_start_gate(
    delivered_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_delete = manage_worktree._delete_cycle_branch
    monkeypatch.setattr(manage_worktree, "_delete_cycle_branch", lambda cycle: (_ for _ in ()).throw(WorktreeError("branch delete failed")))
    with pytest.raises(WorktreeError, match="branch delete failed"):
        cleanup_cycle(delivered_state)
    saved = load_cycle(delivered_state, require_current=True)
    assert saved.finalization is not None
    assert saved.finalization.cleanup.worktree_removed is True
    assert not saved.worktree.exists()
    monkeypatch.setattr(manage_worktree, "_delete_cycle_branch", real_delete)
    assert cleanup_cycle(delivered_state).status == "complete"
```

Add one injected failure per checkpoint: worktree removal, directory/registration postcondition, empty-parent removal, branch deletion, and state unlink. Assert every failure stops later dangerous steps and preserves the latest state except the final unlink failure, where the state remains with `branch_deleted=True`.

- [ ] **Step 3: Run cleanup tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -k "cleanup" -q`

Expected: FAIL because cleanup types, implementation, and CLI do not exist.

- [ ] **Step 4: Implement fixed cleanliness and exact worktree removal**

Define the returned status type before implementing mutations:

```python
@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    status: str
    worktree_removed: bool
    managed_parent_removed: bool
    branch_deleted: bool
```

Use exactly:

```python
("-c", "status.showUntrackedFiles=all", "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none")
```

On first start, revalidate original repo, managed direct-child path, worktree registration, branch/ref, `HEAD == delivery_commit`, delivery hashes, and empty porcelain output. Run `git worktree remove -- <exact path>` without `--force`; verify both directory and exact porcelain registration are absent; set `worktree_removed=True`; save atomically before any later destructive step.

- [ ] **Step 5: Implement container handling and branch deletion**

Attempt a nonrecursive `Path.rmdir()` only when `.worktrees/stabilizing-prompts/` is empty; a nonempty directory is a successful preserved condition. Record `managed_parent_handled=True`. Before `git branch -D -- <short-name>`, verify the full ref equals `delivery_commit`, is not used by any `git worktree list --porcelain -z` entry, is not the primary current branch, and is not the local target of a symbolic remote default branch. Record `branch_deleted=True`, save atomically, then unlink only the exact `STATE_PATH`.

- [ ] **Step 6: Implement retry semantics**

When a saved flag is true, require the corresponding current postcondition instead of rerunning the mutation. A missing worktree is acceptable only with `worktree_removed=True` and absent exact registration; a missing branch is acceptable only with `branch_deleted=True`. Never infer completion from absence alone.

- [ ] **Step 7: Run all worktree tests**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -q`

Expected: PASS.

- [ ] **Step 8: Commit cleanup**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "feat: clean verified tuning cycles"
```

---

### Task 7: End-to-end scored-result finalization matrix

**Files:**
- Modify: `tests/integration_support.py:1208-1281,1542-2154`
- Modify: `tests/test_integration_tune.py:384-566`
- Modify: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: automatic excludes, formal result normalization, finalization, transactional delivery, and cleanup from Tasks 2-6.
- Produces: a test-only tune orchestration that exercises every documented scored terminal outcome through prepared commit, confirmation, delivery profile, and cleanup.
- Extends: `TuneResult` with `formal_result`, `summary_path`, `prepared_commit`, `delivery_commit`, `delivery_profile`, and `cleanup_status`.

- [ ] **Step 1: Add failing no-change and declined-delivery lifecycle tests**

```python
def test_no_change_finalizes_assets_and_cleans_after_confirmation(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="no-change", confirm_delivery=True)
    assert result.stop_reason == "no_change_needed"
    assert result.delivery_profile == "assets"
    assert result.prepared_commit == result.delivery_commit
    assert result.summary_path is not None
    assert (target_repo / result.summary_path).is_file()
    assert result.cleanup_status == "complete"
    assert not (target_repo / ".worktrees" / "stabilizing-prompts").exists()


def test_declined_formal_result_retains_prepared_cycle_without_workspace_delivery(target_repo: Path) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(target_repo, scenario="no-change", confirm_delivery=False)
    assert result.prepared_commit is not None
    assert result.delivery_commit is None
    assert result.cleanup_status == "retained"
    assert workspace_snapshot(target_repo) == before
    assert len(tuple((target_repo / ".worktrees" / "stabilizing-prompts").iterdir())) == 1
```

- [ ] **Step 2: Add the full formal-result matrix**

Parameterize scenarios for `no_change_needed`, `no_strict_improvement`, `validation_failed`, `no_improvement_limit`, `round_limit`, `acceptance_failed`, and `acceptance_passed`. For each result, assert summary existence in prepared commit, exactly one compact-history entry, expected acceptance count, exact delivery profile, production Prompt unchanged except acceptance pass, runtime/report exclusion, and cleanup only after affirmative delivery.

- [ ] **Step 3: Run matrix tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_integration_tune.py tests/test_behavior_contract.py -k "finaliz or formal_result or no_change or acceptance" -q`

Expected: FAIL because the harness still returns early for several scored outcomes and retains successful worktrees.

- [ ] **Step 4: Replace result-specific exits with one harness finalizer**

Add `_finalize_scored_result()` in `tests/integration_support.py`. It writes normalized evidence under exact `.runtime`, calls `prepare_finalization()`, records the displayed result/delivery set, applies explicit confirmation, resolves `delivery_commit`, builds and applies the canonical patch, records verified delivery through the atomic callback, and calls `cleanup_cycle()`. It must not invoke finalization for setup, protocol, transport, model-service, identity, or user-confirmation failures.

- [ ] **Step 5: Add post-confirmation ignore drift and delivery-state failure scenarios**

Inject removal of runtime ignore behavior after business confirmation and assert `setup_error`, invalidated confirmation, zero new raw outputs, retained worktree, and no formal summary. Inject atomic delivery-state failure after target verification and assert exact original-workspace rollback, retained cycle, and no cleanup.

- [ ] **Step 6: Run integration suites**

Run: `rtk conda run -n kds python -m pytest tests/test_integration_tune.py tests/test_behavior_contract.py tests/test_integration_verify.py -q`

Expected: PASS.

- [ ] **Step 7: Commit lifecycle integration**

```bash
rtk git add tests/integration_support.py tests/test_integration_tune.py tests/test_behavior_contract.py tests/test_integration_verify.py
rtk git commit -m "test: cover complete tuning cycle lifecycle"
```

---

### Task 8: Skill, reference, CLI, and full-suite contract alignment

**Files:**
- Modify: `SKILL.md:72-406`
- Modify: `references/worktree-lifecycle.md:9-203`
- Modify: `tests/test_skill_instructions.py`
- Modify: `tests/test_skill_structure.py`
- Modify: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: final CLI names and observable behavior implemented in Tasks 1-7.
- Produces: one consistent user-facing lifecycle and documentation contract.

- [ ] **Step 1: Replace obsolete documentation assertions with failing final-state assertions**

```python
def test_tune_documents_automatic_local_excludes_and_unified_finalization(skill_text: str) -> None:
    tune = skill_text.split("## verify", 1)[0]
    assert "repository-local exclude" in tune
    assert "before invoking `tune`" not in tune
    assert "prepared_commit" in tune
    assert "delivery_commit" in tune
    assert "evaluation-summaries" in tune
    assert "cleanup --state STATE_PATH" in tune
    assert "does not auto-delete the worktree" not in tune


def test_verify_remains_read_only_for_missing_runtime_ignores(skill_text: str) -> None:
    verify = skill_text.split("## verify", 1)[1]
    assert "does not modify repository-local exclude" in verify
    assert "before any output or model call" in verify
```

Update the ordered tune-state assertion to include `repository-local exclude initialization`, `normalize final result`, `prepared_commit`, `delivery-and-cleanup confirmation`, `allowlisted synchronization`, and `verified cleanup`.

- [ ] **Step 2: Run documentation contract tests and verify failure**

Run: `rtk conda run -n kds python -m pytest tests/test_skill_instructions.py tests/test_skill_structure.py tests/test_behavior_contract.py -q`

Expected: FAIL on obsolete manual-ignore, direct no-change exit, failure-only asset delivery, and retained-success-worktree text.

- [ ] **Step 3: Rewrite the tune lifecycle and CLI contracts**

Document automatic initialization and both ignore gates before contract/cases/adapter, the defensive post-confirmation recheck, all seven formal outcomes, deterministic summary and compact history before confirmation, prepared/delivery commit distinction, combined delivery/cleanup confirmation, refusal retention, transactional delivery-state failure rollback, first-start cleanup gates, and phase-aware cleanup retry. Add these stable commands:

```text
manage_worktree.py verify-ignores --state STATE_PATH
manage_worktree.py cleanup --state STATE_PATH
finalize_cycle.py prepare --state STATE_PATH --result RESULT --finished-at UTC --evidence SUMMARY_JSON [--candidate FROZEN_CANDIDATE --candidate-hash SHA256]
finalize_cycle.py approve --state STATE_PATH
```

Retain the existing create/build/apply shapes and state that build derives the effective profile from current state even while the compatibility `--result` spelling remains accepted during this version.

- [ ] **Step 4: Align the lifecycle reference and remove obsolete claims**

Delete claims that users must establish ignore rules, `no_change_needed` skips asset delivery, acceptance failure owns a special delivery path, and successful worktrees are never automatically cleaned. Preserve non-scored interruption retention, refusal retention, `.gitignore` exclusion, verify read-only behavior, and single acceptance activity.

- [ ] **Step 5: Run documentation and complete regression suites**

Run: `rtk conda run -n kds python -m pytest tests/test_skill_instructions.py tests/test_skill_structure.py tests/test_behavior_contract.py -q`

Expected: PASS.

Run: `rtk conda run -n kds python -m pytest -q`

Expected: PASS with zero failures.

- [ ] **Step 6: Run repository hygiene checks**

Run: `rtk git diff --check`

Expected: no output and exit code 0.

Run: `rtk git status --short`

Expected: only the files intentionally changed by Tasks 1-8 before the final commit; no runtime reports, `.runtime`, temporary patch, temporary manifest, bytecode, or test scratch files.

- [ ] **Step 7: Commit final documentation alignment**

```bash
rtk git add SKILL.md references/worktree-lifecycle.md tests/test_skill_instructions.py tests/test_skill_structure.py tests/test_behavior_contract.py
rtk git commit -m "docs: align stabilizing prompts lifecycle"
```

---

## Final Verification

- [ ] Run: `rtk conda run -n kds python -m pytest -q`

  Expected: PASS with zero failures.

- [ ] Run: `rtk git diff --check HEAD~8..HEAD`

  Expected: no whitespace errors.

- [ ] Run: `rtk git status --short --branch`

  Expected: a clean worktree on the implementation branch.

- [ ] Inspect `rtk git log -8 --oneline` and confirm one reviewed commit per task, with no unrelated files.
