# Project-Local Worktree Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every tuning cycle start from a primary checkout and create its dedicated Git worktree only under the target repository's ignored `.worktrees/stabilizing-prompts/` directory.

**Architecture:** Keep `scripts/manage_worktree.py` as the sole owner of worktree identity, creation, state persistence, and delivery. Add read-only primary-checkout and ignore preflight helpers, then derive the worktree path internally; preserve the existing `WorktreeCycle` and delivery patch contracts.

**Tech Stack:** Python 3.14, standard library, Git CLI, pytest

**Spec:** `docs/superpowers/specs/2026-09-14-stabilizing-prompts-project-local-worktree-design.md`

## Global Constraints

- The only managed worktree root is `<repo>/.worktrees/stabilizing-prompts/`.
- The final worktree directory must be ignored before any directory or branch is created.
- A linked worktree, detached `HEAD`, unresolved repository identity, or invalid branch is a fail-closed setup error.
- Never fall back to tuning in the original workspace.
- Do not delete worktrees or branches automatically.
- Preserve `WorktreeCycle`, immutable `cycle_base_commit`, fresh-generated allowlisted delivery patches, exact rollback, and unstaged delivery.
- `--branch` remains supported; the `--worktree PATH` CLI option and `worktree=` Python keyword are removed.
- All commands in this repository are run through `rtk`.

---

### Task 1: Primary-checkout identity preflight

**Files:**
- Modify: `scripts/manage_worktree.py:84-126,394-440`
- Test: `tests/test_manage_worktree.py`

**Interfaces:**
- Consumes: `_git(repo: Path, *arguments: str, check: bool = True)` and `_git_text(repo: Path, *arguments: str)`.
- Produces: `_primary_workspace_head(root: Path) -> str`, returning the verified commit hash or raising `WorktreeError` before writes.

- [ ] **Step 1: Add failing primary-workspace tests**

```python
def test_create_cycle_rejects_linked_worktree_before_writes(repo, tmp_path):
    original, prompt_id = repo
    linked = tmp_path / "linked"
    git(original, "worktree", "add", "-b", "linked-test", str(linked), "HEAD")

    with pytest.raises(WorktreeError, match="primary workspace|linked worktree"):
        create_cycle(linked, prompt_id)

    assert not (linked / ".worktrees").exists()


def test_create_cycle_rejects_detached_head(repo):
    original, prompt_id = repo
    git(original, "checkout", "--detach", "HEAD")

    with pytest.raises(WorktreeError, match="detached HEAD"):
        create_cycle(original, prompt_id)
```

Use this submodule regression so the guard is exercised with a real `.git` indirection:

```python
def test_create_cycle_accepts_primary_submodule_checkout(tmp_path):
    source, prompt_id = make_repo(tmp_path / "source")
    superproject, _ = make_repo(tmp_path / "superproject")
    git(
        superproject,
        "-c", "protocol.file.allow=always",
        "submodule", "add", str(source), "prompt-submodule",
    )
    git(superproject, "commit", "-am", "add prompt submodule")

    cycle = create_cycle(superproject / "prompt-submodule", prompt_id)
    assert cycle.original_repo == (superproject / "prompt-submodule").resolve()
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `rtk pytest tests/test_manage_worktree.py -k "linked_worktree or detached_head or submodule" -q`

Expected: FAIL because `create_cycle` does not yet distinguish a primary checkout from a linked worktree or detached `HEAD`.

- [ ] **Step 3: Implement resolved Git-directory identity checks**

```python
def _resolved_git_directory(root: Path, argument: str) -> Path:
    value = Path(_git_text(root, "rev-parse", argument))
    if not value.is_absolute():
        value = root / value
    return value.resolve(strict=False)


def _primary_workspace_head(root: Path) -> str:
    git_dir = _resolved_git_directory(root, "--git-dir")
    common_dir = _resolved_git_directory(root, "--git-common-dir")
    superproject = _git_text(root, "rev-parse", "--show-superproject-working-tree")
    if git_dir != common_dir and not superproject:
        raise WorktreeError("tune must start from the primary workspace, not a linked worktree")
    branch = _git_text(root, "branch", "--show-current")
    if not branch:
        raise WorktreeError("tune cannot start from detached HEAD")
    head = _git_text(root, "rev-parse", "--verify", "HEAD")
    if not head:
        raise WorktreeError("repository HEAD cannot be verified")
    return head
```

Call `_primary_workspace_head(root)` immediately after `_repo_root(Path(original_repo))` and before prompt discovery, UUID generation, directory creation, or `git worktree add`.

- [ ] **Step 4: Run the focused tests and the existing cycle identity test**

Run: `rtk pytest tests/test_manage_worktree.py -k "linked_worktree or detached_head or submodule or cycle_base" -q`

Expected: PASS.

- [ ] **Step 5: Commit the identity preflight**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "fix: require primary workspace for tuning cycles"
```

---

### Task 2: Fixed project-local path and ignore gate

**Files:**
- Modify: `scripts/manage_worktree.py:389-440,1256-1291`
- Modify: `tests/test_manage_worktree.py:45-120,413-420`

**Interfaces:**
- Consumes: `_primary_workspace_head(root: Path) -> str` from Task 1.
- Produces: `_cycle_worktree_path(root: Path, prompt_id: str, identity: str) -> Path` and `_require_ignored_worktree(root: Path, target: Path) -> None`.
- Changes: `create_cycle(original_repo: Path, prompt_id: str, *, branch: str | None = None, prompt_path: Path | str | None = None) -> WorktreeCycle`.

- [ ] **Step 1: Make the repository fixture opt into `.worktrees/`**

Add this before the initial fixture commit:

```python
(path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
git(path, "add", ".gitignore", "prompts/classify.md", ".prompt-evals")
```

Remove the older `git add` call that omits `.gitignore`.

- [ ] **Step 2: Add failing path, ignore, and API tests**

```python
def test_cycle_uses_fixed_project_local_path(repo):
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id)
    assert cycle.worktree.parent == original / ".worktrees" / "stabilizing-prompts"
    assert cycle.worktree.name.startswith("classify--abc123-")
    assert git(original, "status", "--short") == ""


def test_create_cycle_rejects_special_child_only_ignore(tmp_path):
    original, prompt_id = make_repo(tmp_path / "repo")
    (original / ".gitignore").write_text("**/.stabilizing-prompts-probe\n", encoding="utf-8")
    git(original, "add", ".gitignore")
    git(original, "commit", "-m", "ignore only probe")

    with pytest.raises(WorktreeError, match="ignored"):
        create_cycle(original, prompt_id)

    assert not (original / ".worktrees").exists()


def test_create_cycle_api_rejects_removed_worktree_keyword(repo, tmp_path):
    original, prompt_id = repo
    with pytest.raises(TypeError, match="worktree"):
        create_cycle(original, prompt_id, worktree=tmp_path / "custom")
```

Use a fixed UUID (`0123456789ab`) in tests by monkeypatching `manage_worktree.uuid.uuid4`. Implement named tests for these exact inputs and outcomes:

- `test_create_cycle_rejects_worktrees_file_before_git_add`: create `.worktrees` as a regular file; expect `WorktreeError(".worktrees is not a directory")` and no new branch.
- `test_create_cycle_rejects_target_reincluded_by_negation_rule`: write ignore rules that ignore `.worktrees/*` but re-include the fixed final target; expect `WorktreeError` containing `not ignored`.
- `test_create_cycle_rejects_existing_derived_target`: pre-create the fixed final target directory; expect `WorktreeError` containing `already exists`.
- `test_create_cycle_rejects_existing_custom_branch`: create branch `existing-cycle`; call `create_cycle(..., branch="existing-cycle")`; expect a Git creation error and leave the directory absent.
- `test_create_cycle_accepts_safe_custom_branch_without_changing_path`: pass `branch="custom/cycle"`; assert the recorded branch is exact and the target remains `repo/.worktrees/stabilizing-prompts/classify--abc123-0123456789ab`.
- `test_create_cycle_rejects_invalid_custom_branch`: parameterize `"bad branch"`, `"refs/heads/main"`, and `"topic..bad"`; expect `WorktreeError` containing `invalid branch` before any directory appears.

Each rejection also compares `git branch --format=%(refname:short)` and the filesystem snapshot before and after the call.

- [ ] **Step 3: Run the new tests and verify failure**

Run: `rtk pytest tests/test_manage_worktree.py -k "fixed_project_local or special_child_only or removed_worktree or ignored or custom_branch" -q`

Expected: FAIL because paths are still repository siblings and custom worktree paths remain accepted.

- [ ] **Step 4: Implement fixed derivation and directory-level ignore validation**

```python
def _cycle_worktree_path(root: Path, prompt_id: str, identity: str) -> Path:
    target = (
        root / ".worktrees" / "stabilizing-prompts" / f"{_safe_slug(prompt_id)}-{identity}"
    ).resolve(strict=False)
    expected_parent = (root / ".worktrees" / "stabilizing-prompts").resolve(strict=False)
    if target.parent != expected_parent:
        raise WorktreeError("derived worktree path escapes .worktrees/stabilizing-prompts")
    return target


def _require_ignored_worktree(root: Path, target: Path) -> None:
    relative = target.relative_to(root).as_posix() + "/"
    result = _git(
        root,
        "check-ignore",
        "--no-index",
        "--quiet",
        "--",
        relative,
        check=False,
    )
    if result.returncode == 1:
        raise WorktreeError(f"worktree directory is not ignored: {relative}")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise WorktreeError(f"unable to verify worktree ignore rule: {detail}")
```

Before creating directories, reject `.worktrees` when it exists and is not a directory, validate the selected branch with `git check-ref-format --branch`, and reject an existing branch with `git show-ref --verify --quiet refs/heads/<branch>`. Then derive the target internally, verify it is absent, and call `_require_ignored_worktree`. Only after every check succeeds may the implementation create `target.parent` and run `git worktree add -b`.

- [ ] **Step 5: Remove the custom worktree CLI/API**

Delete `worktree: Path | None = None` from `create_cycle`, delete `create.add_argument("--worktree", type=Path)`, and remove `worktree=args.worktree` from `main`. Replace the obsolete inside-repository rejection test with parser coverage:

```python
def test_create_cli_rejects_worktree_option(repo, tmp_path, capsys):
    original, prompt_id = repo
    with pytest.raises(SystemExit) as error:
        main([
            "create", "--repo", str(original), "--prompt-id", prompt_id,
            "--state", str(tmp_path / "state.json"),
            "--worktree", str(tmp_path / "custom"),
        ])
    assert error.value.code == 2
```

- [ ] **Step 6: Run the worktree creation suite**

Run: `rtk pytest tests/test_manage_worktree.py -q`

Expected: PASS, including existing delivery tests.

- [ ] **Step 7: Commit project-local creation**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "feat: create tuning worktrees inside ignored project directory"
```

---

### Task 3: Actionable state-persistence failure

**Files:**
- Modify: `scripts/manage_worktree.py:1214-1220,1280-1310`
- Test: `tests/test_manage_worktree.py`

**Interfaces:**
- Consumes: the `WorktreeCycle` returned after successful Git creation.
- Produces: CLI error JSON with `status`, `error`, `worktree`, and `branch` when state persistence fails.

- [ ] **Step 1: Add a failing CLI persistence test**

```python
def test_create_cli_reports_created_identity_when_state_save_fails(repo, tmp_path, monkeypatch, capsys):
    original, prompt_id = repo

    def fail_save(cycle, path):
        raise OSError("state disk unavailable")

    monkeypatch.setattr(manage_worktree, "save_cycle", fail_save)
    result = main([
        "create", "--repo", str(original), "--prompt-id", prompt_id,
        "--state", str(tmp_path / "state.json"),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "error"
    assert Path(payload["worktree"]).is_dir()
    assert payload["branch"].startswith("stabilizing-prompts/")
```

- [ ] **Step 2: Run the test and verify failure**

Run: `rtk pytest tests/test_manage_worktree.py::test_create_cli_reports_created_identity_when_state_save_fails -q`

Expected: FAIL because the generic exception payload omits the created worktree and branch.

- [ ] **Step 3: Preserve created identity in the error payload**

Wrap only `save_cycle` after `create_cycle`:

```python
try:
    save_cycle(cycle, args.state)
except Exception as error:
    print(json.dumps({
        "status": "error",
        "error": str(error),
        "worktree": str(cycle.worktree),
        "branch": cycle.branch,
    }, ensure_ascii=False, sort_keys=True))
    return 2
```

Do not remove the created worktree or branch.

- [ ] **Step 4: Run the focused and full module tests**

Run: `rtk pytest tests/test_manage_worktree.py -q`

Expected: PASS.

- [ ] **Step 5: Commit failure reporting**

```bash
rtk git add scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "fix: report worktree identity after state save failure"
```

---

### Task 4: Worktree workflow documentation and behavior contract

**Files:**
- Modify: `SKILL.md:50-77,299-314`
- Modify: `references/worktree-lifecycle.md:1-176`
- Modify: `tests/test_skill_instructions.py`
- Modify: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: the finalized `manage_worktree.py create --repo PATH --prompt-id ID --state PATH [--branch BRANCH]` contract.
- Produces: user-facing primary-workspace, ignore, fixed-path, and fail-closed instructions.

- [ ] **Step 1: Add failing instruction assertions**

```python
def test_tune_documents_project_local_worktree_gate(skill_text):
    tune = skill_text.split("## `verify`", 1)[0]
    assert ".worktrees/stabilizing-prompts" in tune
    assert "primary workspace" in tune
    assert "git check-ignore" in tune
    assert "never" in tune and "fall back" in tune
    assert "--worktree" not in tune
```

Add a behavior-contract assertion that a linked-worktree or ignore failure occurs before the fake transport records a model call.

- [ ] **Step 2: Run the instruction and behavior tests and verify failure**

Run: `rtk pytest tests/test_skill_instructions.py tests/test_behavior_contract.py -k "worktree or project_local" -q`

Expected: FAIL because the current prose still describes only a generic dedicated worktree.

- [ ] **Step 3: Update the operational documentation**

Document the exact sequence:

```text
resolve primary checkout identity
-> reject linked worktree or detached HEAD
-> derive .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/
-> verify that directory is ignored
-> create branch and worktree
-> persist WorktreeCycle
```

State that `.gitignore` is never modified automatically, failures never run tuning in the original checkout, and retained worktrees/branches require manual inspection or cleanup.

- [ ] **Step 4: Run the documentation contract tests**

Run: `rtk pytest tests/test_skill_structure.py tests/test_skill_instructions.py tests/test_behavior_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit worktree documentation**

```bash
rtk git add SKILL.md references/worktree-lifecycle.md tests/test_skill_instructions.py tests/test_behavior_contract.py
rtk git commit -m "docs: define project-local tuning worktree lifecycle"
```

---

### Task 5: Worktree regression verification

**Files:**
- Test: `tests/test_manage_worktree.py`
- Test: `tests/test_integration_tune.py`
- Test: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: all worktree changes from Tasks 1-4.
- Produces: offline evidence that creation failures precede all model calls and delivery semantics remain unchanged.

- [ ] **Step 1: Run all worktree and integration tests**

Run: `rtk pytest tests/test_manage_worktree.py tests/test_integration_tune.py tests/test_behavior_contract.py -q`

Expected: PASS with no real model requests.

- [ ] **Step 2: Run the complete offline suite**

Run: `rtk pytest -q`

Expected: PASS.

- [ ] **Step 3: Confirm only expected files changed**

Run: `rtk git status --short`

Expected: only intentional worktree-plan files are modified; pre-existing `scripts/__pycache__/` and `tests/__pycache__/` remain untracked and unstaged.

- [ ] **Step 4: Record final worktree-plan verification evidence**

Run: `rtk git log --oneline -4`

Expected: the identity, project-local creation, persistence-reporting, and documentation commits from Tasks 1-4 are present. Do not create an empty verification-only commit.
