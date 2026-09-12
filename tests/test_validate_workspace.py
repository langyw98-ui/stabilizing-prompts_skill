from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_workspace import (
    WorkspaceError,
    main,
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


def test_workspace_cli_writes_machine_readable_snapshot(
    repo_with_prompt: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, prompt = repo_with_prompt
    monkeypatch.setattr("scripts.validate_workspace._ensure_kds_environment", lambda: None)
    output = tmp_path / "workspace.json"

    assert (
        main(
            [
                "--repo",
                str(repo),
                "--prompt",
                str(prompt.relative_to(repo)),
                "--mode",
                "verify",
                "--output",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "valid"
    assert payload["mode"] == "verify"
    assert payload["prompt_path"] == "prompts/classify.md"
    assert payload["prompt_id"] == prompt_id_for_path("prompts/classify.md")
    assert payload["python_command"] == ["conda", "run", "-n", "kds", "python"]


def test_workspace_cli_writes_explicit_error_for_validation_failure(
    repo_with_prompt: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, _prompt = repo_with_prompt
    monkeypatch.setattr("scripts.validate_workspace._ensure_kds_environment", lambda: None)
    output = tmp_path / "workspace-error.json"

    assert (
        main(
            [
                "--repo",
                str(repo),
                "--prompt",
                "missing.md",
                "--mode",
                "tune",
                "--output",
                str(output),
            ]
        )
        == 2
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "error"
    assert "error" in payload and payload["error"]


def test_workspace_cli_help_and_unknown_argument_are_real_argparse_contracts() -> None:
    root = Path(__file__).resolve().parents[1]
    help_result = subprocess.run(
        [sys.executable, "-m", "scripts.validate_workspace", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    garbage_result = subprocess.run(
        [sys.executable, "-m", "scripts.validate_workspace", "--garbage"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "--mode" in help_result.stdout
    assert garbage_result.returncode != 0
