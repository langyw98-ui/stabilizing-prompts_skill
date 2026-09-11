"""Read-only validation for a prompt evaluation workspace.

The evaluator operates on one repository-relative Markdown prompt.  This module
keeps the checks that establish that boundary in one place so later runner
stages can consume an immutable snapshot instead of re-discovering it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import subprocess
import sys
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - kds includes PyYAML
    yaml = None  # type: ignore[assignment]


class WorkspaceError(RuntimeError):
    """Raised when a workspace cannot be safely evaluated."""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """The repository facts that must stay fixed for an evaluation cycle."""

    repo_root: Path
    head_commit: str
    prompt_path: str
    prompt_hash: str
    prompt_id: str
    python_command: tuple[str, ...] = ("conda", "run", "-n", "kds", "python")
    python_version: str = sys.version.split()[0]


def _canonical_posix_path(path: str) -> str:
    """Return a normalized repository-style path without changing its case."""

    value = str(path).replace("\\", "/")
    normalized = posixpath.normpath(value)
    if normalized == ".":
        return ""
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return PurePosixPath(normalized).as_posix()


def prompt_id_for_path(path: str) -> str:
    """Build the stable, readable ID for a repository-relative prompt path."""

    canonical = _canonical_posix_path(path)
    stem = canonical.removesuffix(".md")
    # Keep path components' ordinary hyphens readable while making separators
    # and other punctuation unambiguous.  The hash disambiguates any slug
    # collisions and is deliberately based on the complete canonical path.
    slug = re.sub(r"[^a-zA-Z0-9-]+", "--", stem).strip("-").lower()
    suffix = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{slug}--{suffix}"


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except OSError as exc:
        raise WorkspaceError(f"unable to run git: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            raise WorkspaceError(f"git {' '.join(arguments)} failed: {detail}")
        raise WorkspaceError(f"git {' '.join(arguments)} failed")
    return result


def _git_lines(repo_root: Path, *arguments: str) -> list[str]:
    result = _git(repo_root, *arguments)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _ensure_kds_environment() -> None:
    environment = os.environ.get("CONDA_DEFAULT_ENV")
    executable_parent = Path(sys.executable).resolve().parent.name.lower()
    if environment != "kds" or executable_parent != "kds":
        actual = environment or executable_parent or "unknown"
        raise WorkspaceError(
            f"workspace validation requires the kds Python environment (got {actual})"
        )


def _resolve_inside(repo_root: Path, path: Path, *, label: str) -> tuple[Path, str]:
    candidate = path if path.is_absolute() else repo_root / path
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise WorkspaceError(f"{label} is outside repository: {path}") from exc
    return resolved, relative.as_posix()


def _tracked_path(repo_root: Path, relative: str, *, label: str) -> None:
    matches = _git_lines(repo_root, "ls-files", "--full-name", "--", relative)
    if len(matches) > 1:
        raise WorkspaceError(f"{label} matches multiple targets: {', '.join(matches)}")
    if not matches:
        raise WorkspaceError(f"{label} must be Git tracked: {relative}")
    # A pathspec such as ``prompts/*.md`` may happen to match one file.  It is
    # still not a stable, explicit target and must not enter a run manifest.
    if _canonical_posix_path(matches[0]) != _canonical_posix_path(relative):
        raise WorkspaceError(f"{label} must name exactly one tracked path: {relative}")


def _differs_from_head(repo_root: Path, relative_paths: Sequence[str]) -> bool:
    # Check both worktree and index.  ``git diff --quiet --`` is intentionally
    # used for the former, while the cached check catches staged edits too.
    for arguments in (
        ("diff", "--quiet", "--", *relative_paths),
        ("diff", "--cached", "--quiet", "--", *relative_paths),
    ):
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if result.returncode == 1:
            return True
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise WorkspaceError(f"git diff failed: {detail or 'unknown error'}")
    return False


def _canonical_contract_path(repo_root: Path, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("prompt-contract.yaml contains an invalid prompt path")
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            _, relative = _resolve_inside(repo_root, candidate, label="recorded prompt path")
        except WorkspaceError as exc:
            raise WorkspaceError(
                "prompt-contract.yaml records a prompt path outside repository"
            ) from exc
        return relative
    canonical = _canonical_posix_path(value)
    if (
        not canonical
        or canonical == ".."
        or canonical.startswith("../")
        or "\x00" in value
        or any(character in canonical for character in "*?[")
    ):
        raise WorkspaceError("prompt-contract.yaml contains an invalid prompt path")
    return canonical


def _contract_recorded_path(repo_root: Path, contract_path: Path) -> str | None:
    if yaml is None:
        raise WorkspaceError("cannot read prompt-contract.yaml without PyYAML")
    try:
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise WorkspaceError(f"invalid prompt-contract.yaml: {exc}") from exc
    if not isinstance(data, dict):
        raise WorkspaceError("prompt-contract.yaml must contain a mapping")

    # The design names the field semantically rather than prescribing one
    # serialization key.  Support the names used by the CLI input and the
    # contract artifact, plus a nested prompt record for forward compatibility.
    recorded_paths: list[str] = []
    for key in ("prompt_path", "target_prompt", "path"):
        if key in data:
            recorded_paths.append(_canonical_contract_path(repo_root, data[key]))
    prompt_record = data.get("prompt")
    if isinstance(prompt_record, dict):
        for key in ("path", "prompt_path", "target_prompt"):
            if key in prompt_record:
                recorded_paths.append(_canonical_contract_path(repo_root, prompt_record[key]))
    if not recorded_paths:
        return None
    if len(set(recorded_paths)) > 1:
        raise WorkspaceError("prompt-contract.yaml contains conflicting prompt paths")
    return recorded_paths[0]


def _validate_recorded_contract(repo_root: Path, relative: str, prompt_id: str) -> None:
    expected_contract = repo_root / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    candidates: list[Path] = []
    if expected_contract.is_file():
        candidates.append(expected_contract)

    # If a prompt was moved, an older prompt-id directory may still record the
    # same path.  Detect that duplicate so callers must explicitly migrate it.
    eval_root = repo_root / ".prompt-evals"
    if eval_root.is_dir():
        candidates.extend(
            path
            for path in eval_root.glob("*/prompt-contract.yaml")
            if path.is_file() and path != expected_contract
        )

    for contract in candidates:
        recorded = _contract_recorded_path(repo_root, contract)
        if contract == expected_contract:
            if recorded is not None and recorded != relative:
                raise WorkspaceError(
                    "prompt-contract.yaml records a different prompt path: "
                    f"{recorded} (expected {relative})"
                )
        elif recorded == relative:
            raise WorkspaceError(
                "prompt-contract.yaml for another prompt-id already records "
                f"{relative}; migrate the existing evaluation directory"
            )


def _validate_file_target(repo_root: Path, path: Path, *, label: str) -> str:
    resolved, relative = _resolve_inside(repo_root, path, label=label)
    _tracked_path(repo_root, relative, label=label)
    if not resolved.is_file():
        raise WorkspaceError(f"{label} does not exist: {relative}")
    return relative


def validate_workspace(
    repo_root: Path, prompt_path: Path, dependency_paths: Sequence[Path]
) -> WorkspaceSnapshot:
    """Validate and snapshot one clean prompt plus its critical dependencies.

    The function only invokes read-only Git commands and reads files.  It does
    not stage, modify, or otherwise repair the repository.
    """

    _ensure_kds_environment()

    requested_root = Path(repo_root).resolve(strict=False)
    if not requested_root.is_dir():
        raise WorkspaceError(f"repository root does not exist: {repo_root}")
    git_root_text = _git(requested_root, "rev-parse", "--show-toplevel").stdout.strip()
    actual_root = Path(git_root_text).resolve(strict=False)
    if actual_root != requested_root:
        raise WorkspaceError(
            f"repository root must be the Git root: {requested_root} (actual {actual_root})"
        )
    head_commit = _git(requested_root, "rev-parse", "--verify", "HEAD").stdout.strip()

    prompt_candidate = Path(prompt_path)
    if prompt_candidate.suffix != ".md":
        raise WorkspaceError("target prompt must be a .md file")
    prompt_resolved, prompt_relative = _resolve_inside(
        requested_root, prompt_candidate, label="prompt"
    )
    if any(character in prompt_relative for character in "*?["):
        # The suffix check intentionally allows us to identify wildcard paths,
        # but a run must freeze one concrete repository-relative path.
        _tracked_path(requested_root, prompt_relative, label="prompt")
    prompt_relative = _validate_file_target(requested_root, prompt_resolved, label="prompt")

    dependency_relatives: list[str] = []
    for dependency in dependency_paths:
        dependency_relative = _validate_file_target(
            requested_root, Path(dependency), label="dependency"
        )
        if dependency_relative not in dependency_relatives:
            dependency_relatives.append(dependency_relative)

    if _differs_from_head(requested_root, [prompt_relative]):
        raise WorkspaceError("prompt must match HEAD")
    if dependency_relatives and _differs_from_head(requested_root, dependency_relatives):
        raise WorkspaceError("dependency must match HEAD")

    prompt_id = prompt_id_for_path(prompt_relative)
    _validate_recorded_contract(requested_root, prompt_relative, prompt_id)
    prompt_hash = hashlib.sha256(prompt_resolved.read_bytes()).hexdigest()

    return WorkspaceSnapshot(
        repo_root=requested_root,
        head_commit=head_commit,
        prompt_path=prompt_relative,
        prompt_hash=prompt_hash,
        prompt_id=prompt_id,
    )
