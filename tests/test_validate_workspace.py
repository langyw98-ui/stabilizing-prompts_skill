from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.validate_workspace import (
    WorkspaceError,
    prompt_id_for_path,
    validate_workspace,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init")
    _git(path, "config", "user.email", "tests@example.invalid")
    _git(path, "config", "user.name", "Workspace Tests")
    (path / "README.md").write_text("repository\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


@pytest.fixture
def repo_with_prompt(tmp_path: Path) -> tuple[Path, Path]:
    repo = make_git_repo(tmp_path / "repo")
    prompt = repo / "prompts" / "classify.md"
    prompt.parent.mkdir()
    prompt.write_text("classify\n", encoding="utf-8")
    _git(repo, "add", "prompts/classify.md")
    _git(repo, "commit", "-m", "add prompt")
    return repo, prompt


def test_prompt_id_uses_canonical_path_and_hash() -> None:
    value = prompt_id_for_path("src/agent-a/prompts/classify.md")

    assert value.startswith("src--agent-a--prompts--classify--")
    assert len(value.rsplit("--", 1)[1]) == 12


def test_prompt_id_normalizes_windows_separators() -> None:
    assert prompt_id_for_path(r"src\agent-a\prompts\classify.md") == prompt_id_for_path(
        "src/agent-a/prompts/classify.md"
    )


def test_rejects_prompt_outside_repo(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("prompt", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="outside repository"):
        validate_workspace(repo, outside, ())


def test_rejects_non_markdown_prompt(repo_with_prompt: tuple[Path, Path]) -> None:
    repo, _ = repo_with_prompt
    other = repo / "notes.txt"
    other.write_text("not a prompt\n", encoding="utf-8")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-m", "add notes")

    with pytest.raises(WorkspaceError, match=r"\.md"):
        validate_workspace(repo, other, ())


def test_rejects_multiple_prompt_targets(repo_with_prompt: tuple[Path, Path]) -> None:
    repo, _ = repo_with_prompt
    second = repo / "prompts" / "other.md"
    second.write_text("other\n", encoding="utf-8")
    _git(repo, "add", "prompts/other.md")
    _git(repo, "commit", "-m", "add second prompt")

    with pytest.raises(WorkspaceError, match="multiple"):
        validate_workspace(repo, repo / "prompts" / "*.md", ())


def test_rejects_untracked_prompt(repo_with_prompt: tuple[Path, Path], tmp_path: Path) -> None:
    repo, _ = repo_with_prompt
    untracked = repo / "prompts" / "new.md"
    untracked.write_text("new\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="tracked"):
        validate_workspace(repo, untracked, ())


def test_rejects_dirty_prompt_but_allows_unrelated_dirty_file(
    repo_with_prompt: tuple[Path, Path],
) -> None:
    repo, prompt = repo_with_prompt
    (repo / "notes.txt").write_text("dirty", encoding="utf-8")

    assert validate_workspace(repo, prompt, ()).prompt_path == "prompts/classify.md"

    prompt.write_text("dirty prompt", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="must match HEAD"):
        validate_workspace(repo, prompt, ())


def test_rejects_staged_prompt(repo_with_prompt: tuple[Path, Path]) -> None:
    repo, prompt = repo_with_prompt
    prompt.write_text("staged prompt", encoding="utf-8")
    _git(repo, "add", "prompts/classify.md")

    with pytest.raises(WorkspaceError, match="must match HEAD"):
        validate_workspace(repo, prompt, ())


def test_rejects_dirty_dependency(repo_with_prompt: tuple[Path, Path]) -> None:
    repo, prompt = repo_with_prompt
    dependency = repo / "src" / "renderer.py"
    dependency.parent.mkdir()
    dependency.write_text("render\n", encoding="utf-8")
    _git(repo, "add", "src/renderer.py")
    _git(repo, "commit", "-m", "add renderer")
    dependency.write_text("changed\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="dependency"):
        validate_workspace(repo, prompt, (dependency,))


def test_rejects_untracked_dependency(repo_with_prompt: tuple[Path, Path]) -> None:
    repo, prompt = repo_with_prompt
    dependency = repo / "src" / "renderer.py"
    dependency.parent.mkdir()
    dependency.write_text("render\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="tracked"):
        validate_workspace(repo, prompt, (dependency,))


def test_rejects_recorded_prompt_contract_path_mismatch(
    repo_with_prompt: tuple[Path, Path],
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("prompt_path: prompts/other.md\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_recorded_prompt_contract_path_outside_repository(
    repo_with_prompt: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    contract.write_text(f"prompt_path: '{outside.as_posix()}'\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_invalid_recorded_prompt_contract_path_value(
    repo_with_prompt: tuple[Path, Path],
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("prompt_path: 123\n", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_invalid_later_prompt_contract_alias(
    repo_with_prompt: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    contract.write_text(
        "prompt_path: prompts/classify.md\n"
        f"target_prompt: '{outside.as_posix()}'\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_invalid_later_nested_prompt_contract_alias(
    repo_with_prompt: tuple[Path, Path], tmp_path: Path
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    contract.write_text(
        "prompt:\n"
        "  path: prompts/classify.md\n"
        f"  target_prompt: '{outside.as_posix()}'\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_conflicting_recorded_prompt_contract_aliases(
    repo_with_prompt: tuple[Path, Path],
) -> None:
    repo, prompt = repo_with_prompt
    prompt_id = prompt_id_for_path("prompts/classify.md")
    contract = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        "prompt_path: prompts/classify.md\n"
        "target_prompt: prompts/other.md\n",
        encoding="utf-8",
    )

    with pytest.raises(WorkspaceError, match="prompt-contract"):
        validate_workspace(repo, prompt, ())


def test_rejects_non_kds_environment(
    repo_with_prompt: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, prompt = repo_with_prompt
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "base")

    with pytest.raises(WorkspaceError, match="kds"):
        validate_workspace(repo, prompt, ())


def test_snapshot_contains_head_hash_and_prompt_metadata(
    repo_with_prompt: tuple[Path, Path],
) -> None:
    repo, prompt = repo_with_prompt

    snapshot = validate_workspace(repo, prompt, ())

    assert snapshot.repo_root == repo.resolve()
    assert snapshot.head_commit == _git(repo, "rev-parse", "HEAD")
    assert len(snapshot.prompt_hash) == 64
    assert snapshot.prompt_id == prompt_id_for_path("prompts/classify.md")
    assert snapshot.python_command == ("conda", "run", "-n", "kds", "python")
