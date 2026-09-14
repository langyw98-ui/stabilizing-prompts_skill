from __future__ import annotations

from dataclasses import replace
import hashlib
import os
from pathlib import Path
import subprocess
import uuid

import pytest

from scripts import manage_worktree
from scripts.manage_worktree import (
    FAILURE_ALLOWLIST,
    SUCCESS_ALLOWLIST,
    DeliveryConflict,
    DeliveryError,
    WorktreeCycle,
    WorktreeError,
    apply_delivery_patch,
    build_delivery_patch,
    create_cycle,
    main,
    preflight_patch,
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
        if path.is_file() and ".git" not in path.parts and ".worktrees" not in path.parts
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
    (path / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
    git(path, "add", ".gitignore", "prompts/classify.md", ".prompt-evals")
    git(path, "commit", "-m", "initial prompt assets")
    return path, prompt_id


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


def test_create_cycle_rejects_special_child_only_ignore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, prompt_id = make_repo(tmp_path / "repo")
    _freeze_cycle_uuid(monkeypatch)
    (original / ".gitignore").write_text(
        "**/.stabilizing-prompts-probe\n", encoding="utf-8"
    )
    git(original, "add", ".gitignore")
    git(original, "commit", "-m", "ignore only probe")
    before_branches = git(original, "branch", "--format=%(refname:short)")
    before_files = snapshot_filesystem(original)

    with pytest.raises(WorktreeError, match="ignored"):
        create_cycle(original, prompt_id)

    assert git(original, "branch", "--format=%(refname:short)") == before_branches
    assert snapshot_filesystem(original) == before_files
    assert not (original / ".worktrees").exists()


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


def test_create_cycle_rejects_target_reincluded_by_negation_rule(
    repo: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    original, prompt_id = repo
    _freeze_cycle_uuid(monkeypatch)
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

    cycle = create_cycle(superproject / "prompt-submodule", prompt_id)
    assert cycle.original_repo == (superproject / "prompt-submodule").resolve()


def test_success_allowlist_resolves_prompt_symbol_to_canonical_target(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert "prompts/classify.md" in patch.paths
    assert "prompt" not in patch.paths
    assert all(Path(path).is_absolute() is False for path in patch.paths)


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
