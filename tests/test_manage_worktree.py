from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path
import subprocess

import pytest

from scripts.manage_worktree import (
    FAILURE_ALLOWLIST,
    SUCCESS_ALLOWLIST,
    DeliveryConflict,
    WorktreeCycle,
    apply_delivery_patch,
    build_delivery_patch,
    create_cycle,
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
    git(path, "add", "prompts/classify.md", ".prompt-evals")
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


def test_cycle_base_is_original_head(repo: tuple[Path, str]) -> None:
    original, prompt_id = repo
    expected_head = git(original, "rev-parse", "HEAD")

    cycle = create_cycle(original, prompt_id)

    assert cycle.cycle_base_commit == expected_head
    assert cycle.original_repo == original.resolve()
    assert cycle.worktree.is_dir()
    assert cycle.branch


def test_success_allowlist_resolves_prompt_symbol_to_canonical_target(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)

    assert "prompts/classify.md" in patch.paths
    assert "prompt" not in patch.paths
    assert all(Path(path).is_absolute() is False for path in patch.paths)


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


def test_preflight_rejects_crafted_unallowlisted_patch(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    crafted = replace(
        patch,
        paths=patch.paths + ("unrelated.txt",),
        source_hashes={**patch.source_hashes, "unrelated.txt": None},
        destination_hashes={**patch.destination_hashes, "unrelated.txt": None},
    )

    with pytest.raises(ValueError, match="allowlist"):
        preflight_patch(completed_cycle, crafted)


def test_apply_verification_failure_rolls_back_exact_targets(
    completed_cycle: WorktreeCycle,
) -> None:
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST - {".gitignore"})
    original = completed_cycle.original_repo
    tracked_before = (original / "prompts" / "classify.md").read_bytes()
    tampered = replace(
        patch,
        destination_hashes={
            path: ("0" * 64 if digest is not None else None)
            for path, digest in patch.destination_hashes.items()
        },
    )

    with pytest.raises(ValueError, match="destination hash"):
        apply_delivery_patch(completed_cycle, tampered)

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
