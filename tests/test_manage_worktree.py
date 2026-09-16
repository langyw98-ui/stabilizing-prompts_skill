from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from scripts import manage_worktree
from scripts.manage_worktree import (
    AllowlistError,
    CleanupProgress,
    FAILURE_ALLOWLIST,
    DeliveryPatch,
    FinalizationState,
    SUCCESS_ALLOWLIST,
    DeliveryConflict,
    DeliveryError,
    WorktreeCycle,
    WorktreeError,
    apply_delivery_patch,
    build_delivery_patch,
    create_cycle,
    load_cycle,
    main,
    preflight_patch,
    save_patch,
    save_cycle_atomic,
)


def git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def snapshot_workspace(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if (
            path.is_file()
            and ".git" not in path.parts
            and path.relative_to(root).parts[:2]
            != (".worktrees", "stabilizing-prompts")
        )
    }


def snapshot_filesystem(root: Path) -> dict[str, tuple[str, object]]:
    snapshot: dict[str, tuple[str, object]] = {}
    for path in root.rglob("*"):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", "")
        elif path.is_file():
            snapshot[relative] = ("file", path.read_bytes())
        else:
            snapshot[relative] = ("other", "")
    return snapshot


def make_repo(path: Path) -> tuple[Path, str]:
    path.mkdir()
    git(path, "init")
    (path / ".git" / "info" / "exclude").write_text(
        ".worktrees/\n", encoding="utf-8"
    )
    git(path, "config", "user.email", "tests@example.invalid")
    git(path, "config", "user.name", "Worktree Tests")
    prompt = path / "prompts" / "classify.md"
    prompt.parent.mkdir()
    prompt.write_text("original prompt\n", encoding="utf-8")
    prompt_id = "classify--abc123"
    contract = path / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        "prompt_path: prompts/classify.md\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text("", encoding="utf-8")
    git(path, "add", ".gitignore", "prompts/classify.md", ".prompt-evals")
    git(path, "commit", "-m", "initial prompt assets")
    return path, prompt_id


def _exclude_path(repo: Path) -> Path:
    value = Path(git(repo, "rev-parse", "--git-path", "info/exclude"))
    return value if value.is_absolute() else repo / value


def test_create_cycle_initializes_repository_local_excludes(tmp_path: Path) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    (original / ".gitignore").write_text("", encoding="utf-8")
    _exclude_path(original).write_bytes(b"")

    cycle = create_cycle(original, prompt_id)

    text = _exclude_path(original).read_text(encoding="utf-8")
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
    monkeypatch.setattr(
        manage_worktree,
        "verify_runtime_ignores",
        lambda cycle: (_ for _ in ()).throw(
            WorktreeError("runtime path is not ignored")
        ),
        raising=False,
    )

    with pytest.raises(WorktreeError, match="runtime path is not ignored"):
        create_cycle(original, prompt_id, state_path=state)

    retained = load_cycle(state, require_current=True)
    assert retained.worktree.is_dir()
    assert retained.branch_created_by_cycle is True
    assert retained.runtime_ignores_verified is False


def test_verify_ignores_cli_updates_state_only_after_both_paths_pass(
    repo: tuple[Path, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original, prompt_id = repo
    state = tmp_path / "cycle.json"
    cycle = create_cycle(original, prompt_id, state_path=state)
    assert cycle.runtime_ignores_verified is True

    exclude = _exclude_path(original)
    before = exclude.read_bytes()
    result = main(["verify-ignores", "--state", str(state)])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["status"] == "verified"
    assert exclude.read_bytes() == before
    assert load_cycle(state, require_current=True).runtime_ignores_verified is True


def test_local_exclude_preserves_bytes_without_trailing_newline(
    repo: tuple[Path, str],
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    existing = b"# user rule\r\ncustom-pattern"
    exclude.write_bytes(existing)
    worktree = original / ".worktrees" / "stabilizing-prompts" / "path with spaces"

    result = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    updated = exclude.read_bytes()
    assert result.changed is True
    assert updated.startswith(existing + b"\n")
    assert updated.count(b"# stabilizing-prompts managed local excludes") == 1


def test_local_exclude_second_call_is_byte_identical(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"

    first = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)
    before = _exclude_path(original).read_bytes()
    second = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert first.changed is True
    assert second.changed is False
    assert _exclude_path(original).read_bytes() == before


def test_local_exclude_partial_rules_only_fill_missing_rules(
    repo: tuple[Path, str],
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    exclude.write_bytes(b"/.worktrees/stabilizing-prompts/\n")
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"

    manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    updated = exclude.read_text(encoding="utf-8")
    assert updated.count("/.worktrees/stabilizing-prompts/") == 1
    assert updated.count("/.prompt-evals/*/reports/") == 1
    assert updated.count("/.prompt-evals/*/.runtime/") == 1


def test_local_exclude_wider_rules_do_not_change_bytes(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    existing = b"/.worktrees/\n/.prompt-evals/\n"
    exclude.write_bytes(existing)
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"

    result = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert result.changed is False
    assert exclude.read_bytes() == existing


def test_local_exclude_recomputes_once_after_first_drift(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    exclude.write_bytes(b"user-rule\n")
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"
    real_read = manage_worktree._read_exclude_bytes
    reads = 0

    def drift_once(path: Path) -> tuple[bool, bytes]:
        nonlocal reads
        reads += 1
        if reads == 2:
            path.write_bytes(path.read_bytes() + b"concurrent-rule\n")
        return real_read(path)

    monkeypatch.setattr(manage_worktree, "_read_exclude_bytes", drift_once)

    result = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert result.changed is True
    assert reads == 4
    assert b"concurrent-rule\n" in exclude.read_bytes()
    assert list(exclude.parent.glob(f".{exclude.name}.*.tmp")) == []


def test_local_exclude_fails_closed_after_second_drift(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    exclude.write_bytes(b"user-rule\n")
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"
    real_read = manage_worktree._read_exclude_bytes
    reads = 0

    def drift_twice(path: Path) -> tuple[bool, bytes]:
        nonlocal reads
        reads += 1
        if reads in {2, 4}:
            path.write_bytes(path.read_bytes() + f"drift-{reads}\n".encode())
        return real_read(path)

    monkeypatch.setattr(manage_worktree, "_read_exclude_bytes", drift_twice)

    with pytest.raises(WorktreeError, match="changed concurrently"):
        manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert reads == 4
    assert b"drift-4\n" in exclude.read_bytes()
    assert list(exclude.parent.glob(f".{exclude.name}.*.tmp")) == []


def test_local_exclude_replace_failure_preserves_original_and_cleans_temp(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    existing = b"user-rule\n"
    exclude.write_bytes(existing)
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"

    monkeypatch.setattr(
        manage_worktree.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(WorktreeError, match="replace failed"):
        manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert exclude.read_bytes() == existing
    assert list(exclude.parent.glob(f".{exclude.name}.*.tmp")) == []


def test_another_local_exclude_initializer_completes_remaining_rules(
    repo: tuple[Path, str],
) -> None:
    original, prompt_id = repo
    exclude = _exclude_path(original)
    exclude.write_bytes(b"/.worktrees/stabilizing-prompts/\n")
    worktree = original / ".worktrees" / "stabilizing-prompts" / "cycle"

    first = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)
    second = manage_worktree.initialize_local_excludes(original, prompt_id, worktree)

    assert first.changed is True
    assert second.changed is False
    text = exclude.read_text(encoding="utf-8")
    assert text.count("# stabilizing-prompts managed local excludes") == 1
    assert all(rule in text for rule in manage_worktree.MANAGED_EXCLUDES)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    return make_repo(tmp_path / "repo")


@pytest.fixture
def completed_cycle(repo: tuple[Path, str]) -> WorktreeCycle:
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id)
    eval_dir = cycle.worktree / ".prompt-evals" / prompt_id
    (cycle.worktree / "prompts" / "classify.md").write_text(
        "candidate prompt\n", encoding="utf-8"
    )
    (eval_dir / "eval-config.yaml").write_text("repeats: 5\n", encoding="utf-8")
    (eval_dir / "optimization-history.yaml").write_text(
        "cycles:\n  - id: cycle-1\n", encoding="utf-8"
    )
    (eval_dir / "coverage-obligations.yaml").write_text(
        "version: 1\ncategories: []\nobligations: []\n", encoding="utf-8"
    )
    (eval_dir / "reports").mkdir()
    (eval_dir / "reports" / "run.json").write_text("raw report\n", encoding="utf-8")
    (eval_dir / ".runtime").mkdir()
    (eval_dir / ".runtime" / "candidate.md").write_text(
        "temporary candidate\n", encoding="utf-8"
    )
    (cycle.worktree / "unrelated.txt").write_text("do not deliver\n", encoding="utf-8")
    git(cycle.worktree, "add", "-A")
    git(cycle.worktree, "commit", "-m", "complete cycle")
    return cycle


@pytest.fixture
def finalized_state(repo: tuple[Path, str], tmp_path: Path) -> Path:
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id)
    eval_dir = cycle.worktree / ".prompt-evals" / prompt_id
    for name in (
        "eval-config.yaml",
        "dev-cases.yaml",
        "validation-cases.yaml",
        "acceptance-cases.yaml",
        "coverage-obligations.yaml",
        "adapter.py",
        "optimization-history.yaml",
    ):
        (eval_dir / name).write_text(f"name: {name}\n", encoding="utf-8")
    summary = eval_dir / "evaluation-summaries" / "2026-09-16-080910-no_change_needed.md"
    summary.parent.mkdir(parents=True)
    summary.write_text("# Evaluation summary\n", encoding="utf-8")
    (eval_dir / "reports").mkdir()
    (eval_dir / ".runtime").mkdir()
    git(cycle.worktree, "add", "-A")
    git(cycle.worktree, "commit", "-m", "prepare finalized result")
    prepared_commit = git(cycle.worktree, "rev-parse", "HEAD")
    finalization = FinalizationState(
        result_kind="no_change_needed",
        stop_reason="all baseline slots passed",
        finished_at_utc="2026-09-16T08:09:10Z",
        summary_path=summary.relative_to(cycle.worktree).as_posix(),
        delivery_profile="assets",
        prepared_commit=prepared_commit,
        delivery_commit=prepared_commit,
        delivery_confirmed=True,
        cleanup=CleanupProgress(authorized=True),
    )
    finalized = replace(
        cycle,
        final_worktree_commit=prepared_commit,
        finalization=finalization,
    )
    state = tmp_path / "finalized-state.json"
    save_cycle_atomic(finalized, state)
    return state


@pytest.fixture
def finalized_cycle(finalized_state: Path) -> WorktreeCycle:
    return load_cycle(finalized_state, require_current=True)


@pytest.fixture
def spaced_path_cycle(repo: tuple[Path, str]) -> WorktreeCycle:
    original, prompt_id = repo
    git(original, "mv", "prompts/classify.md", "prompts/classify prompt.md")
    contract = original / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.write_text("prompt_path: prompts/classify prompt.md\n", encoding="utf-8")
    git(original, "add", ".prompt-evals")
    git(original, "commit", "-m", "use prompt path with spaces")
    cycle = create_cycle(original, prompt_id)
    (cycle.worktree / "prompts" / "classify prompt.md").write_text(
        "candidate prompt\n", encoding="utf-8"
    )
    git(cycle.worktree, "add", "-A")
    git(cycle.worktree, "commit", "-m", "candidate with spaces")
    return cycle


def test_cycle_base_is_original_head(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    expected_head = git(original, "rev-parse", "HEAD")

    cycle = create_cycle(original, prompt_id)

    assert cycle.cycle_base_commit == expected_head
    assert cycle.original_repo == original.resolve()
    assert cycle.worktree.is_dir()
    assert cycle.branch


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
    state.write_bytes(b'{"sentinel": true}\n')
    monkeypatch.setattr(
        manage_worktree.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(WorktreeError, match="replace failed"):
        save_cycle_atomic(completed_cycle, state)

    assert state.read_bytes() == b'{"sentinel": true}\n'
    assert list(tmp_path.glob(f".{state.name}.*.tmp")) == []


def test_cleanup_loader_rejects_legacy_state(tmp_path: Path) -> None:
    state = tmp_path / "legacy.json"
    state.write_text(
        json.dumps(
            {
                "original_repo": "x",
                "worktree": "y",
                "branch": "z",
                "cycle_base_commit": "a",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(WorktreeError, match="current lifecycle state"):
        load_cycle(state, require_current=True)


def _freeze_cycle_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manage_worktree.uuid,
        "uuid4",
        lambda: uuid.UUID("0123456789ab00000000000000000000"),
    )


def _make_directory_symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as symlink_error:
        if os.name != "nt":
            pytest.skip(f"directory symlinks are unavailable: {symlink_error}")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        pytest.skip(f"directory junctions are unavailable: {detail}")


def test_cycle_uses_fixed_project_local_path(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)

    cycle = create_cycle(original, prompt_id)

    assert cycle.worktree.parent == original / ".worktrees" / "stabilizing-prompts"
    assert cycle.worktree.name.startswith("classify--abc123-")
    assert git(original, "status", "--short") == ""


def test_gitignore_is_not_a_delivery_allowlist_entry(
    completed_cycle: WorktreeCycle,
) -> None:
    assert ".gitignore" not in SUCCESS_ALLOWLIST
    assert ".gitignore" not in FAILURE_ALLOWLIST

    with pytest.raises(AllowlistError, match="unsupported|allowlist|gitignore"):
        build_delivery_patch(completed_cycle, {".gitignore"})


def test_gitignore_change_is_never_built_or_delivered(
    completed_cycle: WorktreeCycle,
) -> None:
    original = completed_cycle.original_repo / ".gitignore"
    before = original.read_bytes()
    worktree_ignore = completed_cycle.worktree / ".gitignore"
    worktree_ignore.write_text(
        worktree_ignore.read_text(encoding="utf-8") + "should-not-be-delivered\n",
        encoding="utf-8",
    )
    git(completed_cycle.worktree, "add", ".gitignore")
    git(completed_cycle.worktree, "commit", "-m", "change target ignore rules")

    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert ".gitignore" not in patch.paths
    apply_delivery_patch(completed_cycle, patch)
    assert original.read_bytes() == before


def test_loaded_cycle_rejects_external_worktree_before_delivery(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    original, prompt_id = repo
    external = tmp_path / "legacy-worktree"
    git(original, "worktree", "add", "-b", "legacy-cycle", str(external), "HEAD")
    state = {
        "original_repo": str(original),
        "worktree": str(external),
        "branch": "legacy-cycle",
        "cycle_base_commit": git(original, "rev-parse", "HEAD"),
        "prompt_id": prompt_id,
        "prompt_path": "prompts/classify.md",
    }
    state_path = tmp_path / "legacy-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WorktreeError, match=r"managed|\.worktrees/stabilizing-prompts"):
        load_cycle(state_path)


def test_delivery_rejects_external_worktree_cycle(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    original, prompt_id = repo
    external = tmp_path / "external-worktree"
    git(original, "worktree", "add", "-b", "external-cycle", str(external), "HEAD")
    cycle = WorktreeCycle(
        original_repo=original,
        worktree=external,
        branch="external-cycle",
        cycle_base_commit=git(original, "rev-parse", "HEAD"),
        prompt_id=prompt_id,
        prompt_path="prompts/classify.md",
    )

    with pytest.raises(WorktreeError, match=r"managed|\.worktrees/stabilizing-prompts"):
        build_delivery_patch(cycle)
    with pytest.raises(WorktreeError, match=r"managed|\.worktrees/stabilizing-prompts"):
        apply_delivery_patch(cycle)


def test_loaded_cycle_rejects_symlink_alias_inside_managed_root(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id)
    alias = cycle.worktree.parent / "alias"
    _make_directory_symlink(alias, cycle.worktree)
    state = cycle.to_dict()
    state["worktree"] = str(alias)
    state_path = tmp_path / "symlink-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(WorktreeError, match="symlink|junction|managed"):
        load_cycle(state_path)


def test_create_cycle_initializes_when_project_has_no_managed_ignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    _freeze_cycle_uuid(monkeypatch)
    (original / ".gitignore").write_text(
        "**/.stabilizing-prompts-probe\n", encoding="utf-8"
    )
    (original / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    git(original, "add", ".gitignore")
    git(original, "commit", "-m", "ignore only probe")
    cycle = create_cycle(original, prompt_id)

    assert cycle.worktree.is_dir()
    assert "/.worktrees/stabilizing-prompts/" in _exclude_path(original).read_text(
        encoding="utf-8"
    )


def test_create_cycle_api_rejects_removed_worktree_keyword(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    original, prompt_id = repo
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(TypeError, match="worktree"):
        create_cycle(original, prompt_id, worktree=tmp_path / "custom")

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_worktrees_file_before_git_add(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    (original / ".worktrees").write_text("not a directory\n", encoding="utf-8")
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match=r"\.worktrees is not a directory"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_stabilizing_prompts_file_before_writes(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    (original / ".worktrees").mkdir()
    (original / ".worktrees" / "stabilizing-prompts").write_text(
        "not a directory\n", encoding="utf-8"
    )
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match=r"stabilizing-prompts.*directory"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_nul_custom_branch_before_git_argument(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    calls: list[tuple[str, ...]] = []
    real_git = manage_worktree._git

    def recording_git(repo_path: Path, *arguments: str, **kwargs: object):
        calls.append(arguments)
        return real_git(repo_path, *arguments, **kwargs)

    monkeypatch.setattr(manage_worktree, "_git", recording_git)

    with pytest.raises(WorktreeError, match="invalid branch"):
        create_cycle(original, prompt_id, branch="custom\x00branch")

    assert calls == []


@pytest.mark.parametrize(
    ("existing", "requested"),
    [
        ("stabilizing-prompts", "stabilizing-prompts/new"),
        ("stabilizing-prompts/new", "stabilizing-prompts"),
    ],
)
def test_create_cycle_rejects_branch_namespace_collision_before_parent_creation(
    repo: tuple[Path, str], existing: str, requested: str
) -> None:
    original, prompt_id = repo
    git(original, "branch", existing)
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="branch.*already exists|namespace"):
        create_cycle(original, prompt_id, branch=requested)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files
    assert not (original / ".worktrees").exists()


def test_create_cycle_rejects_target_reincluded_by_negation_rule(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    (original / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    target_name = "classify--abc123-0123456789ab"
    (original / ".gitignore").write_text(
        ".worktrees/*\n"
        "!.worktrees/stabilizing-prompts/\n"
        f"!.worktrees/stabilizing-prompts/{target_name}/\n",
        encoding="utf-8",
    )
    git(original, "add", ".gitignore")
    git(original, "commit", "-m", "re-include fixed target")
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="not ignored"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_existing_derived_target(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    target = original / ".worktrees" / "stabilizing-prompts" / "classify--abc123-0123456789ab"
    target.mkdir(parents=True)
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="already exists"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_existing_custom_branch(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    git(original, "branch", "existing-cycle")
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="already exists"):
        create_cycle(original, prompt_id, branch="existing-cycle")

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files
    assert not (
        original / ".worktrees" / "stabilizing-prompts" / "classify--abc123-0123456789ab"
    ).exists()


def test_create_cycle_accepts_safe_custom_branch_without_changing_path(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)

    cycle = create_cycle(original, prompt_id, branch="custom/cycle")

    assert cycle.branch == "custom/cycle"
    assert cycle.worktree == (
        original / ".worktrees" / "stabilizing-prompts" / "classify--abc123-0123456789ab"
    ).resolve()


@pytest.mark.parametrize("branch", ["bad branch", "refs/heads/main", "topic..bad"])
def test_create_cycle_rejects_invalid_custom_branch(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch, branch: str
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="invalid branch"):
        create_cycle(original, prompt_id, branch=branch)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files
    assert not (
        original / ".worktrees" / "stabilizing-prompts" / "classify--abc123-0123456789ab"
    ).exists()


@pytest.mark.parametrize("component", [".worktrees", "stabilizing-prompts"])
def test_create_cycle_rejects_redirected_worktree_component_before_writes(
    repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    external = tmp_path / "redirected"
    external.mkdir()
    if component == ".worktrees":
        link = original / ".worktrees"
    else:
        (original / ".worktrees").mkdir()
        link = original / ".worktrees" / "stabilizing-prompts"
    _make_directory_symlink(link, external)
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)
    before_external = snapshot_filesystem(external)

    with pytest.raises(WorktreeError, match="symlink|junction|escape"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files
    assert snapshot_filesystem(external) == before_external


def test_create_cycle_rejects_dangling_derived_target_before_writes(
    repo: tuple[Path, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
    target_parent = original / ".worktrees" / "stabilizing-prompts"
    target_parent.mkdir(parents=True)
    target = target_parent / "classify--abc123-0123456789ab"
    missing_target = target_parent / "missing-target"
    missing_target.mkdir()
    _make_directory_symlink(target, missing_target)
    missing_target.rmdir()
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="symlink|junction|exists"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files


def test_create_cycle_rejects_linked_worktree_before_writes(
    repo: tuple[Path, str], tmp_path: Path
) -> None:
    original, prompt_id = repo
    linked = tmp_path / "linked"
    git(original, "worktree", "add", "-b", "linked-test", str(linked), "HEAD")

    with pytest.raises(WorktreeError, match="primary workspace|linked worktree"):
        create_cycle(linked, prompt_id)

    assert not (linked / ".worktrees").exists()


def test_create_cycle_rejects_detached_head(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    git(original, "checkout", "--detach", "HEAD")

    with pytest.raises(WorktreeError, match="detached HEAD"):
        create_cycle(original, prompt_id)


def test_create_cycle_accepts_primary_submodule_checkout(tmp_path: Path) -> None:
    source, prompt_id = make_repo(tmp_path / "source")
    superproject, _ = make_repo(tmp_path / "superproject")
    git(
        superproject,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(source),
        "prompt-submodule",
    )
    git(superproject, "commit", "-am", "add prompt submodule")
    module_exclude = (
        superproject
        / ".git"
        / "modules"
        / "prompt-submodule"
        / "info"
        / "exclude"
    )
    module_exclude.write_text(".worktrees/\n", encoding="utf-8")

    cycle = create_cycle(superproject / "prompt-submodule", prompt_id)
    assert cycle.original_repo == (superproject / "prompt-submodule").resolve()


def test_success_allowlist_resolves_prompt_symbol_to_canonical_target(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert "prompts/classify.md" in patch.paths
    assert "prompt" not in patch.paths
    assert all(Path(path).is_absolute() is False for path in patch.paths)


def test_success_and_failure_allowlists_include_coverage_obligations(
    completed_cycle: WorktreeCycle,
) -> None:
    relative = (
        f".prompt-evals/{completed_cycle.prompt_id}/coverage-obligations.yaml"
    )

    success = build_delivery_patch(
        completed_cycle, SUCCESS_ALLOWLIST, result="success"
    )
    failure = build_delivery_patch(
        completed_cycle, FAILURE_ALLOWLIST, result="failure"
    )

    assert relative in success.paths
    assert relative in failure.paths


def test_delivery_supports_prompt_path_with_spaces(
    spaced_path_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(spaced_path_cycle, {"prompt"})

    assert patch.paths == ("prompts/classify prompt.md",)
    apply_delivery_patch(spaced_path_cycle, patch)
    assert (
        spaced_path_cycle.original_repo / "prompts" / "classify prompt.md"
    ).read_text(encoding="utf-8") == "candidate prompt\n"


def test_patch_excludes_runtime_reports_and_unlisted_files(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert ".runtime" not in " ".join(patch.paths)
    assert "reports" not in " ".join(patch.paths)
    assert "unrelated.txt" not in patch.paths


def test_failure_allowlist_never_delivers_prompt(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, FAILURE_ALLOWLIST)

    assert "prompts/classify.md" not in patch.paths
    assert ".prompt-evals/classify--abc123/eval-config.yaml" in patch.paths


def test_unallowlisted_change_is_rejected_from_patch_before_application(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert "unrelated.txt" not in patch.paths


def test_patch_allows_only_recorded_evaluation_summary(
    finalized_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(finalized_cycle)

    assert finalized_cycle.finalization is not None
    assert finalized_cycle.finalization.summary_path in patch.paths
    assert all(
        "reports/" not in path and "/.runtime/" not in path
        for path in patch.paths
    )


def test_apply_invokes_verified_callback_for_an_empty_patch(
    repo: tuple[Path, str],
) -> None:
    original, prompt_id = repo
    cycle = create_cycle(original, prompt_id)
    patch = build_delivery_patch(cycle)
    assert patch.text == ""
    observed: list[DeliveryPatch] = []

    apply_delivery_patch(cycle, patch, on_verified=observed.append)

    assert observed == [patch]


@pytest.mark.parametrize("name", ["outside.patch", "../escape.patch"])
def test_build_cli_rejects_output_outside_exact_runtime_output(
    finalized_state: Path,
    tmp_path: Path,
    name: str,
) -> None:
    code = main(
        [
            "build-patch",
            "--state",
            str(finalized_state),
            "--out",
            str(tmp_path / name),
            "--out-manifest",
            str(tmp_path / "manifest.json"),
            "--result",
            "failure",
        ]
    )

    assert code == 2


def test_build_rejects_original_head_drift_before_writing_runtime_outputs(
    finalized_state: Path,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    original = cycle.original_repo
    (original / "head-drift.txt").write_text("user commit\n", encoding="utf-8")
    git(original, "add", "head-drift.txt")
    git(original, "commit", "-m", "move original repository HEAD")
    runtime = cycle.worktree / ".prompt-evals" / cycle.prompt_id / ".runtime"
    patch_path = runtime / "head-drift.patch"
    manifest_path = runtime / "head-drift-manifest.json"

    with pytest.raises(DeliveryConflict, match="HEAD|cycle base"):
        build_delivery_patch(cycle)

    assert not patch_path.exists()
    assert not manifest_path.exists()
    assert (
        main(
            [
                "build-patch",
                "--state",
                str(finalized_state),
                "--out",
                str(patch_path),
                "--out-manifest",
                str(manifest_path),
            ]
        )
        == 2
    )
    assert not patch_path.exists()
    assert not manifest_path.exists()


def test_current_cli_requires_finalization_before_build_or_apply(
    repo: tuple[Path, str],
    tmp_path: Path,
) -> None:
    original, prompt_id = repo
    state_path = tmp_path / "cycle-state.json"
    cycle = create_cycle(original, prompt_id, state_path=state_path)
    runtime = cycle.worktree / ".prompt-evals" / prompt_id / ".runtime"
    runtime.mkdir(parents=True)
    (cycle.worktree / "prompts" / "classify.md").write_text(
        "candidate prompt\n", encoding="utf-8"
    )
    git(cycle.worktree, "add", "prompts/classify.md")
    git(cycle.worktree, "commit", "-m", "candidate before finalization")

    legacy_patch = build_delivery_patch(cycle)
    legacy_patch_path = runtime / "legacy.patch"
    legacy_manifest_path = runtime / "legacy-manifest.json"
    save_patch(legacy_patch, legacy_patch_path, legacy_manifest_path)
    output_path = runtime / "delivery.patch"
    output_manifest_path = runtime / "delivery-manifest.json"
    before = snapshot_filesystem(original)

    assert (
        main(
            [
                "build-patch",
                "--state",
                str(state_path),
                "--out",
                str(output_path),
                "--out-manifest",
                str(output_manifest_path),
            ]
        )
        == 2
    )
    assert not output_path.exists()
    assert not output_manifest_path.exists()

    assert (
        main(
            [
                "apply-patch",
                "--state",
                str(state_path),
                "--patch",
                str(legacy_patch_path),
                "--patch-manifest",
                str(legacy_manifest_path),
            ]
        )
        == 2
    )
    assert snapshot_filesystem(original) == before


def test_apply_cli_records_verified_delivery_and_retains_cleanup_authorization(
    finalized_state: Path,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    runtime = cycle.worktree / ".prompt-evals" / cycle.prompt_id / ".runtime"
    patch_path = runtime / "delivery.patch"
    manifest_path = runtime / "delivery-manifest.json"
    assert (
        main(
            [
                "build-patch",
                "--state",
                str(finalized_state),
                "--out",
                str(patch_path),
                "--out-manifest",
                str(manifest_path),
            ]
        )
        == 0
    )

    assert (
        main(
            [
                "apply-patch",
                "--state",
                str(finalized_state),
                "--patch",
                str(patch_path),
                "--patch-manifest",
                str(manifest_path),
            ]
        )
        == 0
    )
    applied = load_cycle(finalized_state, require_current=True)
    assert applied.finalization is not None
    assert applied.finalization.delivery_applied is True
    assert applied.finalization.delivery_verified is True
    assert applied.finalization.cleanup.authorized is True


def test_build_rejects_unrecorded_summary(finalized_state: Path) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    tampered = replace(
        cycle,
        finalization=replace(
            cycle.finalization,
            summary_path=(
                f".prompt-evals/{cycle.prompt_id}/evaluation-summaries/not-recorded.md"
            ),
        ),
    )

    with pytest.raises(DeliveryError, match="summary|allowlist|finalization"):
        build_delivery_patch(tampered)


def test_build_rejects_summary_from_another_prompt(finalized_state: Path) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    tampered = replace(
        cycle,
        finalization=replace(
            cycle.finalization,
            summary_path=(
                ".prompt-evals/another-prompt/evaluation-summaries/summary.md"
            ),
        ),
    )

    with pytest.raises(DeliveryError, match="summary|prompt|allowlist"):
        build_delivery_patch(tampered)


def test_build_rejects_deleted_recorded_summary(finalized_state: Path) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    summary = cycle.worktree / Path(cycle.finalization.summary_path)
    summary.unlink()
    git(cycle.worktree, "add", "-u", str(summary.relative_to(cycle.worktree)))
    git(cycle.worktree, "commit", "-m", "delete recorded summary")

    with pytest.raises(DeliveryError, match="deletion|summary|delivery"):
        build_delivery_patch(cycle)


def test_build_rejects_wrong_finalization_delivery_profile(finalized_state: Path) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    tampered = replace(
        cycle,
        finalization=replace(cycle.finalization, delivery_profile="success"),
    )

    with pytest.raises(DeliveryError, match="profile|result|finalization"):
        build_delivery_patch(tampered)


def test_build_rejects_extra_changed_path(finalized_state: Path) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    extra = cycle.worktree / "extra-delivery.txt"
    extra.write_text("must not ship\n", encoding="utf-8")
    git(cycle.worktree, "add", "extra-delivery.txt")
    git(cycle.worktree, "commit", "-m", "add unallowlisted delivery path")

    with pytest.raises(DeliveryError, match="allowlist|unexpected|changed|HEAD"):
        build_delivery_patch(cycle)


def test_build_rejects_head_that_does_not_match_delivery_commit(
    finalized_state: Path,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    tampered = replace(
        cycle,
        finalization=replace(cycle.finalization, delivery_commit=cycle.cycle_base_commit),
    )

    with pytest.raises(DeliveryError, match="HEAD|delivery commit|final_worktree"):
        build_delivery_patch(tampered)


def test_build_rejects_nonignored_untracked_worktree_dirt(
    finalized_state: Path,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    (cycle.worktree / "untracked-delivery.txt").write_text(
        "must not be silently ignored\n", encoding="utf-8"
    )

    with pytest.raises(DeliveryError, match="untracked|dirty|worktree"):
        build_delivery_patch(cycle)


def test_build_rejects_dirty_submodule_even_when_repo_config_ignores_it(
    tmp_path: Path,
) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    submodule, _ = make_repo(tmp_path / "submodule")
    git(
        original,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(submodule),
        "vendor",
    )
    git(original, "add", ".gitmodules", "vendor")
    git(original, "commit", "-m", "add local submodule")
    cycle = create_cycle(original, prompt_id)
    git(
        cycle.worktree,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        "--recursive",
    )
    git(cycle.worktree, "config", "submodule.vendor.ignore", "all")
    nested_prompt = cycle.worktree / "vendor" / "prompts" / "classify.md"
    nested_prompt.write_text("dirty submodule\n", encoding="utf-8")

    with pytest.raises(DeliveryConflict, match="submodule|dirty|worktree"):
        build_delivery_patch(cycle)


def test_cli_rejects_persisted_manifest_tampering(
    finalized_state: Path,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    assert cycle.finalization is not None
    runtime = cycle.worktree / ".prompt-evals" / cycle.prompt_id / ".runtime"
    patch_path = runtime / "delivery.patch"
    manifest_path = runtime / "delivery-manifest.json"
    patch = build_delivery_patch(cycle)
    save_patch(patch, patch_path, manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_path = patch.paths[0]
    manifest["source_hashes"][first_path] = "0" * 64
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    code = main(
        [
            "apply-patch",
            "--state",
            str(finalized_state),
            "--patch",
            str(patch_path),
            "--patch-manifest",
            str(manifest_path),
        ]
    )

    assert code == 2


def test_apply_rolls_back_when_verified_state_save_fails(
    finalized_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle = load_cycle(finalized_state, require_current=True)
    patch = build_delivery_patch(cycle)
    before = snapshot_filesystem(cycle.original_repo)

    monkeypatch.setattr(
        manage_worktree,
        "save_cycle_atomic",
        lambda cycle, path: (_ for _ in ()).throw(
            WorktreeError("state replace failed")
        ),
    )
    with pytest.raises(WorktreeError, match="state replace failed"):
        apply_delivery_patch(
            cycle,
            patch,
            on_verified=lambda applied: manage_worktree.save_cycle_atomic(
                cycle, finalized_state
            ),
        )

    assert snapshot_filesystem(cycle.original_repo) == before


def test_apply_conflict_leaves_original_workspace_unchanged(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    original = completed_cycle.original_repo
    prompt = original / "prompts" / "classify.md"
    prompt.write_text("user edit wins\n", encoding="utf-8")
    before = prompt.read_bytes()

    with pytest.raises(DeliveryConflict):
        apply_delivery_patch(completed_cycle, patch)

    assert prompt.read_bytes() == before
    assert git(original, "diff", "--cached", "--quiet", "--", "prompts/classify.md", check=False) == ""


def test_source_hash_preflight_rejects_changed_target_without_mutation(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    target = completed_cycle.original_repo / "prompts" / "classify.md"
    target.write_text("user edit\n", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(DeliveryConflict, match="source hash"):
        preflight_patch(completed_cycle, patch)

    assert target.read_bytes() == before


def test_preflight_rejects_unstaged_cycle_target_change(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    worktree_prompt = completed_cycle.worktree / "prompts" / "classify.md"
    worktree_prompt.write_text("new unstaged candidate\n", encoding="utf-8")
    before = snapshot_workspace(completed_cycle.original_repo)

    with pytest.raises(DeliveryError, match="canonical|worktree|target"):
        preflight_patch(completed_cycle, patch)

    assert snapshot_workspace(completed_cycle.original_repo) == before


def test_apply_rechecks_cycle_worktree_near_application(
    completed_cycle: WorktreeCycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    worktree_prompt = completed_cycle.worktree / "prompts" / "classify.md"
    original_check_sources = manage_worktree._check_sources
    calls = 0

    def mutate_after_source_check(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original_check_sources(*args, **kwargs)
        calls += 1
        if calls == 1:
            worktree_prompt.write_text("TOCTOU candidate\n", encoding="utf-8")

    monkeypatch.setattr(manage_worktree, "_check_sources", mutate_after_source_check)
    before = snapshot_workspace(completed_cycle.original_repo)

    with pytest.raises(DeliveryError, match="canonical|worktree|target"):
        apply_delivery_patch(completed_cycle, patch)

    assert calls == 1
    assert snapshot_workspace(completed_cycle.original_repo) == before


def test_apply_fresh_generates_from_latest_committed_cycle_head(
    completed_cycle: WorktreeCycle,
) -> None:
    stale = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    worktree_prompt = completed_cycle.worktree / "prompts" / "classify.md"
    worktree_prompt.write_text("latest candidate\n", encoding="utf-8")
    git(completed_cycle.worktree, "add", "prompts/classify.md")
    git(completed_cycle.worktree, "commit", "-m", "replace candidate after patch build")

    apply_delivery_patch(completed_cycle, stale)

    assert (
        completed_cycle.original_repo / "prompts" / "classify.md"
    ).read_text(encoding="utf-8") == "latest candidate\n"


def test_delivery_allows_current_prompt_hash_update_without_path_redirect(
    completed_cycle: WorktreeCycle,
) -> None:
    contract = (
        completed_cycle.worktree
        / ".prompt-evals"
        / completed_cycle.prompt_id
        / "prompt-contract.yaml"
    )
    contract.write_text(
        "prompt_path: prompts/classify.md\ncurrent_prompt_hash: updated\n",
        encoding="utf-8",
    )
    git(completed_cycle.worktree, "add", str(contract.relative_to(completed_cycle.worktree)))
    git(completed_cycle.worktree, "commit", "-m", "record current prompt hash")

    apply_delivery_patch(completed_cycle)

    assert "current_prompt_hash: updated" in (
        completed_cycle.original_repo
        / ".prompt-evals"
        / completed_cycle.prompt_id
        / "prompt-contract.yaml"
    ).read_text(encoding="utf-8")


def test_delivery_rejects_committed_prompt_path_redirect(
    completed_cycle: WorktreeCycle,
) -> None:
    contract = (
        completed_cycle.worktree
        / ".prompt-evals"
        / completed_cycle.prompt_id
        / "prompt-contract.yaml"
    )
    contract.write_text("prompt_path: prompts/other.md\n", encoding="utf-8")
    (completed_cycle.worktree / "prompts" / "other.md").write_text(
        "candidate prompt\n", encoding="utf-8"
    )
    git(completed_cycle.worktree, "add", "-A")
    git(completed_cycle.worktree, "commit", "-m", "redirect prompt contract")

    with pytest.raises(DeliveryError, match="Prompt path|canonical"):
        build_delivery_patch(completed_cycle)


def test_delivery_rejects_original_head_drift(
    completed_cycle: WorktreeCycle,
) -> None:
    original = completed_cycle.original_repo
    (original / "notes.txt").write_text("user commit\n", encoding="utf-8")
    git(original, "add", "notes.txt")
    git(original, "commit", "-m", "user commit during cycle")

    with pytest.raises(DeliveryConflict, match="HEAD|cycle base"):
        preflight_patch(completed_cycle)


def test_preflight_rejects_extra_patch_section(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    crafted = replace(patch, text=patch.text + patch.text)

    with pytest.raises(DeliveryError, match="path|section|patch"):
        preflight_patch(completed_cycle, crafted)


def test_delivery_does_not_fallback_to_uncommitted_prompt_contract(
    repo: tuple[Path, str],
) -> None:
    original, prompt_id = repo
    contract = original / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    git(original, "rm", "-f", str(contract.relative_to(original)))
    git(original, "commit", "-m", "remove prompt contract")
    cycle = create_cycle(original, prompt_id)
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_text("prompt_path: prompts/classify.md\n", encoding="utf-8")
    prompt = cycle.worktree / "prompts" / "classify.md"
    prompt.write_text("candidate prompt\n", encoding="utf-8")
    git(cycle.worktree, "add", "prompts/classify.md")
    git(cycle.worktree, "commit", "-m", "candidate without base contract")

    with pytest.raises(DeliveryError, match="canonical Prompt|allowlist"):
        build_delivery_patch(cycle, {"prompt"})


def test_apply_verification_failure_rolls_back_exact_targets(
    completed_cycle: WorktreeCycle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    original = completed_cycle.original_repo
    tracked_before = (original / "prompts" / "classify.md").read_bytes()
    original_assert = manage_worktree._assert_worktree_snapshot
    calls = 0

    def fail_after_apply(cycle: WorktreeCycle, prepared: object) -> None:
        nonlocal calls
        calls += 1
        original_assert(cycle, prepared)
        if calls == 2:
            raise DeliveryError("post-apply verification failure")

    monkeypatch.setattr(manage_worktree, "_assert_worktree_snapshot", fail_after_apply)

    with pytest.raises(DeliveryError, match="verification failure"):
        apply_delivery_patch(completed_cycle, patch)

    assert (original / "prompts" / "classify.md").read_bytes() == tracked_before
    assert not (original / ".prompt-evals" / completed_cycle.prompt_id / "eval-config.yaml").exists()


def test_delivery_is_unstaged_and_worktree_is_not_auto_cleaned(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    apply_delivery_patch(completed_cycle, patch)
    original = completed_cycle.original_repo

    assert (original / "prompts" / "classify.md").read_text(encoding="utf-8") == "candidate prompt\n"
    assert git(original, "diff", "--cached", "--quiet", "--", "prompts/classify.md", check=False) == ""
    # The helper strips Git's leading whitespace, so an unstaged edit is
    # represented by the remaining ``M`` status letter here.
    assert git(original, "status", "--short", "--", "prompts/classify.md").startswith("M")
    assert completed_cycle.worktree.exists()
    assert completed_cycle.branch in git(original, "branch", "--format=%(refname:short)").splitlines()


def test_patch_records_source_and_destination_hashes(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    prompt_path = "prompts/classify.md"

    assert patch.source_hashes[prompt_path] == hashlib.sha256(
        (completed_cycle.original_repo / prompt_path).read_bytes()
    ).hexdigest()
    assert patch.destination_hashes[prompt_path] == hashlib.sha256(
        (completed_cycle.worktree / prompt_path).read_bytes()
    ).hexdigest()


def test_build_rejects_deleting_canonical_prompt(
    completed_cycle: WorktreeCycle,
) -> None:
    prompt = completed_cycle.worktree / "prompts" / "classify.md"
    prompt.unlink()
    git(completed_cycle.worktree, "add", "-A")
    git(completed_cycle.worktree, "commit", "-m", "delete prompt")

    with pytest.raises(DeliveryError, match="delet"):
        build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)


def test_create_cli_rejects_worktree_option(
    repo: tuple[Path, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original, prompt_id = repo

    with pytest.raises(SystemExit) as error:
        main(
            [
                "create",
                "--repo",
                str(original),
                "--prompt-id",
                prompt_id,
                "--state",
                str(tmp_path / "state.json"),
                "--worktree",
                str(tmp_path / "custom"),
            ]
        )

    assert error.value.code == 2


def test_create_cli_reports_created_identity_when_state_save_fails(
    repo: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original, prompt_id = repo

    def fail_save(cycle: WorktreeCycle, path: Path) -> None:
        raise OSError("state disk unavailable")

    monkeypatch.setattr(manage_worktree, "save_cycle", fail_save)
    result = main(
        [
            "create",
            "--repo",
            str(original),
            "--prompt-id",
            prompt_id,
            "--state",
            str(tmp_path / "state.json"),
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 2
    assert payload["status"] == "error"
    assert Path(payload["worktree"]).is_dir()
    assert payload["branch"].startswith("stabilizing-prompts/")
