"""Create isolated prompt-tuning cycles and safely deliver allowlisted assets.

The module keeps the production workspace separate from a tuning worktree.  A
delivery is always a patch from the immutable cycle base commit to a committed
worktree result.  Before applying it, the original workspace is checked for
target collisions and exact source hashes; after applying it, destination
hashes are checked and the exact target snapshots are restored on failure.

No function in this module removes a worktree or branch.  Users can inspect a
failed cycle and clean it up explicitly when they are ready.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import subprocess
import uuid

try:
    import yaml
except ImportError:  # pragma: no cover - kds includes PyYAML
    yaml = None  # type: ignore[assignment]


SUCCESS_ALLOWLIST = {
    # ``prompt`` is a symbolic entry.  It is resolved to the one canonical
    # production Prompt path recorded by prompt-contract.yaml for this cycle.
    "prompt",
    "prompt-contract.yaml",
    "eval-config.yaml",
    "dev-cases.yaml",
    "validation-cases.yaml",
    "acceptance-cases.yaml",
    "coverage-obligations.yaml",
    "adapter.py",
    "optimization-history.yaml",
}
FAILURE_ALLOWLIST = SUCCESS_ALLOWLIST - {"prompt"}

_ASSET_NAMES = frozenset(
    {
        "prompt-contract.yaml",
        "eval-config.yaml",
        "dev-cases.yaml",
        "validation-cases.yaml",
        "acceptance-cases.yaml",
        "coverage-obligations.yaml",
        "adapter.py",
        "optimization-history.yaml",
    }
)
_FORBIDDEN_SEGMENTS = frozenset({".runtime", "reports"})

# These are the only repository-local rules this module may initialize.  The
# concrete paths used to prove each category are derived for the current
# cycle; the rules themselves deliberately remain stable and repository-root
# anchored.
MANAGED_EXCLUDES = (
    "/.worktrees/stabilizing-prompts/",
    "/.prompt-evals/*/reports/",
    "/.prompt-evals/*/.runtime/",
)
_MANAGED_EXCLUDES_COMMENT = b"# stabilizing-prompts managed local excludes"


class WorktreeError(RuntimeError):
    """Raised when a cycle cannot be created or loaded safely."""


@dataclass(frozen=True, slots=True)
class ExcludeResult:
    """The repository-local exclude file and the gates it satisfied."""

    path: Path
    changed: bool
    verified_paths: tuple[str, ...]


class DeliveryError(ValueError):
    """Raised when a delivery patch is invalid or cannot be applied safely."""


class DeliveryConflict(DeliveryError):
    """Raised when the original workspace no longer matches patch sources."""


class AllowlistError(DeliveryError):
    """Raised for an invalid delivery allowlist or patch path."""


# Descriptive compatibility aliases for callers that use shorter names.
PatchError = DeliveryError
WorktreeDeliveryError = DeliveryError


def _git(
    repo: Path,
    *arguments: str,
    input_data: bytes | str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git without invoking a shell and return captured bytes."""

    if isinstance(input_data, str):
        input_data = input_data.encode("utf-8")
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo,
            input=input_data,
            check=False,
            capture_output=True,
        )
    except (OSError, ValueError) as error:
        raise WorktreeError(f"unable to run git: {error}") from error
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        command = "git " + " ".join(arguments)
        raise WorktreeError(f"{command} failed{': ' + detail if detail else ''}")
    return result


def _git_text(repo: Path, *arguments: str) -> str:
    result = _git(repo, *arguments)
    return result.stdout.decode("utf-8", "replace").strip()


def _repo_root(repo: Path) -> Path:
    requested = Path(repo).resolve(strict=False)
    if not requested.is_dir():
        raise WorktreeError(f"repository does not exist: {repo}")
    actual_text = _git_text(requested, "rev-parse", "--show-toplevel")
    actual = Path(actual_text).resolve(strict=False)
    if actual != requested:
        raise WorktreeError(
            f"repository must be the Git root: {requested} (actual {actual})"
        )
    return actual


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


def _path_key(path: Path) -> str:
    """Return a case-aware boundary key for an absolute filesystem path."""

    # ``normcase`` is significant on Windows: two spellings of the same path
    # must not evade the repository-boundary check.  ``commonpath`` (rather
    # than a string prefix) keeps ``repo-two`` outside ``repo``.
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath((_path_key(path), _path_key(parent))) == _path_key(parent)
    except ValueError:
        # Different Windows drives, or another platform's incompatible roots,
        # are necessarily outside one another.
        return False


def _is_link_or_junction(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise WorktreeError(f"unable to inspect worktree path: {path}: {error}") from error
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_worktree_path_links(root: Path, target: Path) -> None:
    current = target
    while current != root:
        if not _path_is_within(current, root):
            raise WorktreeError("derived worktree path escapes the original repository")
        if _is_link_or_junction(current):
            raise WorktreeError(
                f"derived worktree path contains a symlink or junction: {current}"
            )
        current = current.parent


def _validate_managed_worktree_path(
    original_repo: Path,
    worktree: Path,
    *,
    require_exists: bool = True,
) -> Path:
    """Validate a cycle path against the fixed project-local worktree root.

    State files are untrusted transport data.  Check the lexical path before
    resolving it so a symlink alias cannot become an apparently safe managed
    path, then require the resolved path to remain a direct child of the
    managed directory.
    """

    root = Path(original_repo).resolve(strict=False)
    target = Path(worktree).absolute()
    managed_parent = root / ".worktrees" / "stabilizing-prompts"
    try:
        relative = target.relative_to(managed_parent)
    except ValueError as error:
        raise WorktreeError(
            "cycle worktree must be under the managed "
            f".worktrees/stabilizing-prompts directory: {target}"
        ) from error
    if len(relative.parts) != 1 or relative.parts[0] in {"", ".", ".."}:
        raise WorktreeError(
            "cycle worktree must be a direct child of the managed "
            f".worktrees/stabilizing-prompts directory: {target}"
        )

    _reject_worktree_path_links(root, target)
    if managed_parent.exists() and not managed_parent.is_dir():
        raise WorktreeError(f"managed worktree root is not a directory: {managed_parent}")
    canonical_parent = managed_parent.resolve(strict=False)
    canonical_target = target.resolve(strict=False)
    if (
        not _path_is_within(canonical_parent, root)
        or not _path_is_within(canonical_target, canonical_parent)
        or canonical_target.parent != canonical_parent
    ):
        raise WorktreeError(
            "cycle worktree escapes the managed .worktrees/stabilizing-prompts directory"
        )
    if require_exists:
        if not target.exists():
            raise WorktreeError(f"managed cycle worktree does not exist: {target}")
        if not target.is_dir():
            raise WorktreeError(f"managed cycle worktree is not a directory: {target}")
    return target


def _normalize_relative(value: str, *, label: str = "path") -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AllowlistError(f"{label} must be a concrete repository-relative path")
    candidate = value.replace("\\", "/")
    if candidate.startswith("/") or re.match(r"^[A-Za-z]:/", candidate):
        raise AllowlistError(f"{label} must be repository-relative: {value}")
    normalized = posixpath.normpath(candidate)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise AllowlistError(f"{label} escapes the repository: {value}")
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise AllowlistError(f"{label} is not a concrete path: {value}")
    if any(character in normalized for character in "*?["):
        raise AllowlistError(f"{label} must not contain a wildcard: {value}")
    return PurePosixPath(normalized).as_posix()


def _relative_path(repo: Path, value: Path | str, *, label: str = "path") -> str:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        try:
            relative = resolved.relative_to(repo)
        except ValueError as error:
            raise AllowlistError(f"{label} is outside repository: {value}") from error
        return _normalize_relative(relative.as_posix(), label=label)
    return _normalize_relative(str(value), label=label)


def _under_eval_root(path: str, prompt_id: str) -> bool:
    prefix = f".prompt-evals/{prompt_id}/"
    return path.startswith(prefix) and path != prefix


def _reject_unsafe_delivery_path(path: str, *, label: str = "path") -> str:
    normalized = _normalize_relative(path, label=label)
    segments = set(normalized.split("/"))
    if segments & _FORBIDDEN_SEGMENTS:
        raise AllowlistError(f"{label} cannot deliver runtime or report files: {path}")
    if normalized == ".git" or normalized.startswith(".git/"):
        raise AllowlistError(f"{label} cannot deliver Git metadata: {path}")
    return normalized


def _contract_values(data: Mapping[str, object]) -> list[object]:
    values: list[object] = []
    for key in ("prompt_path", "target_prompt", "path"):
        if key in data:
            values.append(data[key])
    nested = data.get("prompt")
    if isinstance(nested, Mapping):
        for key in ("path", "prompt_path", "target_prompt"):
            if key in nested:
                values.append(nested[key])
    return values


def _parse_contract_data(root: Path, data: object) -> str | None:
    if not isinstance(data, Mapping):
        raise DeliveryError("prompt-contract.yaml must contain a mapping")
    paths: list[str] = []
    for raw in _contract_values(data):
        if not isinstance(raw, str) or not raw.strip():
            raise DeliveryError("prompt-contract.yaml contains an invalid prompt path")
        try:
            paths.append(_relative_path(root, raw, label="recorded prompt path"))
        except AllowlistError as error:
            raise DeliveryError(
                "prompt-contract.yaml records a prompt path outside repository"
            ) from error
    if not paths:
        return None
    if len(set(paths)) != 1:
        raise DeliveryError("prompt-contract.yaml contains conflicting prompt paths")
    if not paths[0].casefold().endswith(".md"):
        raise DeliveryError("canonical Prompt path must name a Markdown file")
    return paths[0]


def _parse_contract_prompt_path(root: Path, contract: Path) -> str | None:
    if yaml is None:
        raise DeliveryError("cannot resolve prompt allowlist without PyYAML")
    try:
        data = yaml.safe_load(contract.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise DeliveryError(f"invalid prompt-contract.yaml: {error}") from error
    return _parse_contract_data(root, data)


def _evaluation_prefix(cycle: "WorktreeCycle") -> str | None:
    if cycle.prompt_id is None:
        return None
    if (
        not isinstance(cycle.prompt_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._-]+", cycle.prompt_id)
    ):
        raise AllowlistError("prompt_id is not a safe evaluation directory name")
    return f".prompt-evals/{cycle.prompt_id}/"


def _base_contract_prompt_path(cycle: "WorktreeCycle") -> tuple[bool, str | None]:
    """Read the cycle's contract from its immutable base commit."""

    if not cycle.prompt_id:
        return False, None
    prefix = _evaluation_prefix(cycle)
    assert prefix is not None
    relative = f"{prefix}prompt-contract.yaml"
    result = _git(
        cycle.original_repo,
        "show",
        f"{cycle.cycle_base_commit}:{relative}",
        check=False,
    )
    if result.returncode != 0:
        return False, None
    if yaml is None:
        raise DeliveryError("cannot resolve prompt allowlist without PyYAML")
    try:
        data = yaml.safe_load(result.stdout.decode("utf-8"))
    except Exception as error:
        raise DeliveryError(f"invalid prompt-contract.yaml: {error}") from error
    return True, _parse_contract_data(cycle.original_repo, data)


def _commit_contract_prompt_path(
    cycle: "WorktreeCycle", commit: str
) -> tuple[bool, str | None]:
    """Read the canonical Prompt path from a committed cycle contract."""

    prefix = _evaluation_prefix(cycle)
    if prefix is None:
        return False, None
    relative = f"{prefix}prompt-contract.yaml"
    result = _git(cycle.worktree, "show", f"{commit}:{relative}", check=False)
    if result.returncode != 0:
        return False, None
    if yaml is None:
        raise DeliveryError("cannot resolve prompt allowlist without PyYAML")
    try:
        data = yaml.safe_load(result.stdout.decode("utf-8"))
    except Exception as error:
        raise DeliveryError(f"invalid prompt-contract.yaml: {error}") from error
    return True, _parse_contract_data(cycle.original_repo, data)


def _assert_prompt_contract_identity(cycle: "WorktreeCycle", final: str) -> None:
    """Permit contract metadata updates but never a canonical path redirect."""

    base_exists, base_prompt = _base_contract_prompt_path(cycle)
    final_exists, final_prompt = _commit_contract_prompt_path(cycle, final)
    if not base_exists or base_prompt is None:
        raise AllowlistError(
            "canonical Prompt path cannot be resolved without committed prompt-contract.yaml"
        )
    if not final_exists or final_prompt != base_prompt:
        raise DeliveryConflict(
            "committed prompt-contract.yaml redirected or removed the canonical Prompt path"
        )
    if cycle.prompt_path is not None:
        explicit = _relative_path(cycle.original_repo, cycle.prompt_path, label="prompt path")
        if explicit != base_prompt:
            raise DeliveryConflict("cycle Prompt path does not match prompt-contract.yaml")


def _discover_prompt_path(cycle: "WorktreeCycle") -> str | None:
    base_has_contract, base_prompt = _base_contract_prompt_path(cycle)
    if base_has_contract:
        contract_prompt = base_prompt
    else:
        # A current-workspace contract is not a cycle trust anchor.  The
        # contract must have been generated, confirmed, and committed before
        # delivery; an uncommitted file must never provide the canonical
        # Prompt path.
        contract_prompt = None

    if cycle.prompt_path is not None:
        explicit = _relative_path(cycle.original_repo, cycle.prompt_path, label="prompt path")
        if not explicit.casefold().endswith(".md"):
            raise DeliveryError("canonical Prompt path must name a Markdown file")
        if contract_prompt is not None and explicit != contract_prompt:
            raise DeliveryError("cycle Prompt path conflicts with prompt-contract.yaml")
        return explicit
    return contract_prompt


@dataclass(frozen=True, slots=True)
class CleanupProgress:
    """Atomically persisted progress for the exact cleanup checkpoints."""

    authorized: bool = False
    worktree_removed: bool = False
    managed_parent_handled: bool = False
    branch_deleted: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "authorized": self.authorized,
            "worktree_removed": self.worktree_removed,
            "managed_parent_handled": self.managed_parent_handled,
            "branch_deleted": self.branch_deleted,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "CleanupProgress":
        if not isinstance(value, Mapping):
            raise WorktreeError("cycle cleanup state must contain an object")
        fields = (
            "authorized",
            "worktree_removed",
            "managed_parent_handled",
            "branch_deleted",
        )
        invalid = [name for name in fields if not isinstance(value.get(name, False), bool)]
        if invalid:
            raise WorktreeError(
                "cycle cleanup state contains non-boolean fields: "
                + ", ".join(invalid)
            )
        return cls(**{name: value.get(name, False) for name in fields})


@dataclass(frozen=True, slots=True)
class FinalizationState:
    """The final scored result and all delivery/cleanup state for a cycle."""

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

    def to_dict(self) -> dict[str, object]:
        return {
            "result_kind": self.result_kind,
            "stop_reason": self.stop_reason,
            "finished_at_utc": self.finished_at_utc,
            "summary_path": self.summary_path,
            "delivery_profile": self.delivery_profile,
            "prepared_commit": self.prepared_commit,
            "frozen_candidate_path": self.frozen_candidate_path,
            "frozen_candidate_hash": self.frozen_candidate_hash,
            "delivery_commit": self.delivery_commit,
            "delivery_confirmed": self.delivery_confirmed,
            "delivery_applied": self.delivery_applied,
            "delivery_verified": self.delivery_verified,
            "cleanup": self.cleanup.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FinalizationState":
        if not isinstance(value, Mapping):
            raise WorktreeError("cycle finalization state must contain an object")
        required = (
            "result_kind",
            "stop_reason",
            "finished_at_utc",
            "summary_path",
            "delivery_profile",
            "prepared_commit",
        )
        if any(not isinstance(value.get(name), str) or not value[name] for name in required):
            raise WorktreeError("cycle finalization state is missing required fields")
        optional = ("frozen_candidate_path", "frozen_candidate_hash", "delivery_commit")
        if any(value.get(name) is not None and not isinstance(value.get(name), str) for name in optional):
            raise WorktreeError("cycle finalization state contains invalid optional fields")
        boolean_fields = (
            "delivery_confirmed",
            "delivery_applied",
            "delivery_verified",
        )
        if any(not isinstance(value.get(name, False), bool) for name in boolean_fields):
            raise WorktreeError("cycle finalization state contains non-boolean fields")
        cleanup_value = value.get("cleanup", {})
        if not isinstance(cleanup_value, Mapping):
            raise WorktreeError("cycle finalization state cleanup must contain an object")
        return cls(
            result_kind=str(value["result_kind"]),
            stop_reason=str(value["stop_reason"]),
            finished_at_utc=str(value["finished_at_utc"]),
            summary_path=str(value["summary_path"]),
            delivery_profile=str(value["delivery_profile"]),
            prepared_commit=str(value["prepared_commit"]),
            frozen_candidate_path=value.get("frozen_candidate_path"),
            frozen_candidate_hash=value.get("frozen_candidate_hash"),
            delivery_commit=value.get("delivery_commit"),
            delivery_confirmed=bool(value.get("delivery_confirmed", False)),
            delivery_applied=bool(value.get("delivery_applied", False)),
            delivery_verified=bool(value.get("delivery_verified", False)),
            cleanup=CleanupProgress.from_dict(cleanup_value),
        )


@dataclass(frozen=True, slots=True)
class WorktreeCycle:
    """Identity of one isolated tuning cycle."""

    original_repo: Path
    worktree: Path
    branch: str
    cycle_base_commit: str
    prompt_id: str | None = None
    prompt_path: str | None = None
    final_worktree_commit: str | None = None
    state_version: int = 2
    branch_ref: str | None = None
    branch_origin: str | None = None
    branch_created_by_cycle: bool = False
    exclude_initialized: bool = False
    precreate_ignore_verified: bool = False
    runtime_ignores_verified: bool = False
    finalization: FinalizationState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "original_repo", Path(self.original_repo).resolve(strict=False))
        # Keep the lexical worktree spelling so delivery can reject a symlink
        # or reparse-point alias before resolving it to a safe-looking target.
        object.__setattr__(self, "worktree", Path(self.worktree).absolute())

    @property
    def repo(self) -> Path:
        """Compatibility alias for the original repository."""

        return self.original_repo

    @property
    def final_commit(self) -> str | None:
        return self.final_worktree_commit

    def to_dict(self) -> dict[str, object]:
        return {
            "original_repo": str(self.original_repo),
            "worktree": str(self.worktree),
            "branch": self.branch,
            "cycle_base_commit": self.cycle_base_commit,
            "prompt_id": self.prompt_id,
            "prompt_path": self.prompt_path,
            "final_worktree_commit": self.final_worktree_commit,
            "state_version": self.state_version,
            "branch_ref": self.branch_ref,
            "branch_origin": self.branch_origin,
            "branch_created_by_cycle": self.branch_created_by_cycle,
            "exclude_initialized": self.exclude_initialized,
            "precreate_ignore_verified": self.precreate_ignore_verified,
            "runtime_ignores_verified": self.runtime_ignores_verified,
            "finalization": (
                self.finalization.to_dict() if self.finalization is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "WorktreeCycle":
        required = ("original_repo", "worktree", "branch", "cycle_base_commit")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise WorktreeError("cycle state is missing required identity")
        state_version_value = value.get("state_version", 1)
        if type(state_version_value) is not int or state_version_value < 1:
            raise WorktreeError("cycle state has an invalid state version")
        finalization_value = value.get("finalization")
        if finalization_value is not None and not isinstance(finalization_value, Mapping):
            raise WorktreeError("cycle finalization state must contain an object")
        finalization = (
            FinalizationState.from_dict(finalization_value)
            if isinstance(finalization_value, Mapping)
            else None
        )
        original_repo = Path(str(value["original_repo"]))
        worktree = Path(str(value["worktree"]))
        # A cleanup retry may load the state after its exact worktree has been
        # removed.  The saved checkpoint is the only condition that permits
        # that missing path; all other states still require the directory.
        require_worktree = not (
            finalization is not None and finalization.cleanup.worktree_removed
        )
        _validate_managed_worktree_path(
            original_repo,
            worktree,
            require_exists=require_worktree,
        )
        return cls(
            original_repo=original_repo,
            worktree=worktree,
            branch=str(value["branch"]),
            cycle_base_commit=str(value["cycle_base_commit"]),
            prompt_id=value.get("prompt_id") if isinstance(value.get("prompt_id"), str) else None,
            prompt_path=value.get("prompt_path") if isinstance(value.get("prompt_path"), str) else None,
            final_worktree_commit=(
                value.get("final_worktree_commit")
                if isinstance(value.get("final_worktree_commit"), str)
                else None
            ),
            state_version=state_version_value,
            branch_ref=value.get("branch_ref") if isinstance(value.get("branch_ref"), str) else None,
            branch_origin=value.get("branch_origin") if isinstance(value.get("branch_origin"), str) else None,
            branch_created_by_cycle=(
                value.get("branch_created_by_cycle")
                if isinstance(value.get("branch_created_by_cycle"), bool)
                else False
            ),
            exclude_initialized=(
                value.get("exclude_initialized")
                if isinstance(value.get("exclude_initialized"), bool)
                else False
            ),
            precreate_ignore_verified=(
                value.get("precreate_ignore_verified")
                if isinstance(value.get("precreate_ignore_verified"), bool)
                else False
            ),
            runtime_ignores_verified=(
                value.get("runtime_ignores_verified")
                if isinstance(value.get("runtime_ignores_verified"), bool)
                else False
            ),
            finalization=finalization,
        )


def _validate_managed_cycle(cycle: WorktreeCycle) -> None:
    """Reject legacy or externally located cycle state before delivery."""

    root = _repo_root(cycle.original_repo)
    if root != cycle.original_repo:
        raise WorktreeError("cycle original repository is not canonical")
    _validate_managed_worktree_path(root, cycle.worktree)


def _validate_current_cycle(cycle: WorktreeCycle) -> None:
    """Require ownership metadata before a lifecycle mutation can proceed."""

    if cycle.state_version != 2:
        raise WorktreeError("current lifecycle state requires state_version 2")
    if not isinstance(cycle.branch_ref, str) or not cycle.branch_ref:
        raise WorktreeError("current lifecycle state is missing branch ownership")
    if cycle.branch_origin not in {"generated", "custom"}:
        raise WorktreeError("current lifecycle state has invalid branch origin")
    if cycle.branch_created_by_cycle is not True:
        raise WorktreeError("current lifecycle state is missing branch creation record")
    expected_ref = f"refs/heads/{cycle.branch}"
    if cycle.branch_ref != expected_ref:
        raise WorktreeError("current lifecycle state has a mismatched branch ref")
    if not cycle.branch_ref.startswith("refs/heads/"):
        raise WorktreeError("current lifecycle state has an invalid branch ref")
    branch_name = cycle.branch_ref[len("refs/heads/") :]
    if not branch_name:
        raise WorktreeError("current lifecycle state has an invalid branch ref")
    _validate_branch_name(branch_name)
    ref_check = _git(cycle.original_repo, "check-ref-format", cycle.branch_ref, check=False)
    if ref_check.returncode != 0:
        raise WorktreeError("current lifecycle state has an invalid branch ref")


def _validate_state_path(cycle: WorktreeCycle, path: Path) -> Path:
    """Ensure the authoritative state file survives removal of the worktree."""

    destination = Path(path).resolve(strict=False)
    worktree = Path(cycle.worktree).resolve(strict=False)
    if _path_is_within(destination, worktree):
        raise WorktreeError(
            "cycle state must remain outside the managed cycle worktree"
        )
    return destination


def _safe_slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return result or "prompt"


def _validate_branch_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or value.startswith("refs/")
    ):
        raise WorktreeError(f"invalid branch: {value!r}")
    return value


def _cycle_worktree_path(root: Path, prompt_id: str, identity: str) -> Path:
    root = Path(root).resolve(strict=False)
    expected_parent = root / ".worktrees" / "stabilizing-prompts"
    if expected_parent.exists() and not expected_parent.is_dir():
        raise WorktreeError(f".worktrees/stabilizing-prompts is not a directory: {expected_parent}")
    target = expected_parent / f"{_safe_slug(prompt_id)}-{identity}"
    _reject_worktree_path_links(root, target)
    target = target.resolve(strict=False)
    expected_parent = expected_parent.resolve(strict=False)
    if (
        not _path_is_within(expected_parent, root)
        or not _path_is_within(target, root)
        or target.parent != expected_parent
    ):
        raise WorktreeError("derived worktree path escapes .worktrees/stabilizing-prompts")
    return target


def _repository_local_exclude(root: Path) -> Path:
    """Resolve Git's repository-local exclude inside shared metadata."""

    root = _repo_root(Path(root))
    common = _resolved_git_directory(root, "--git-common-dir")
    value = Path(_git_text(root, "rev-parse", "--git-path", "info/exclude"))
    resolved = (value if value.is_absolute() else root / value).resolve(strict=False)
    if not _path_is_within(resolved, common):
        raise WorktreeError(
            "repository-local exclude is outside shared Git metadata: "
            f"{resolved} (common directory {common})"
        )
    return resolved


def _validate_exclude_prompt_id(prompt_id: str) -> str:
    if (
        not isinstance(prompt_id, str)
        or not prompt_id.strip()
        or "\x00" in prompt_id
        or "/" in prompt_id
        or "\\" in prompt_id
        or prompt_id in {".", ".."}
        or any(character in prompt_id for character in "*?[")
        or not re.fullmatch(r"[A-Za-z0-9._-]+", prompt_id)
    ):
        raise WorktreeError("prompt_id is not a safe evaluation directory name")
    return prompt_id


def _exclude_targets(
    root: Path, prompt_id: str, worktree: Path
) -> tuple[tuple[str, str], ...]:
    """Return concrete ignore probes paired with their managed rule."""

    root = Path(root).resolve(strict=False)
    target = Path(worktree).absolute()
    _validate_exclude_prompt_id(prompt_id)
    _reject_worktree_path_links(root, target)
    try:
        relative_worktree = target.relative_to(root).as_posix()
    except ValueError as error:
        raise WorktreeError(
            f"derived worktree path escapes the original repository: {target}"
        ) from error
    relative_worktree = relative_worktree.rstrip("/")
    if not relative_worktree or relative_worktree in {".", ".."}:
        raise WorktreeError("derived worktree path is not a concrete repository path")
    return (
        (relative_worktree + "/", MANAGED_EXCLUDES[0]),
        (f".prompt-evals/{prompt_id}/reports/", MANAGED_EXCLUDES[1]),
        (f".prompt-evals/{prompt_id}/.runtime/", MANAGED_EXCLUDES[2]),
    )


def _ignore_status(root: Path, relative: str) -> bool:
    result = _git(
        root,
        "check-ignore",
        "--no-index",
        "--quiet",
        "--",
        relative,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
    raise WorktreeError(
        f"unable to verify ignore rule for {relative}: "
        f"{detail or 'git check-ignore failed'}"
    )


def _ignore_diagnostic(root: Path, relative: str) -> str:
    result = _git(
        root,
        "check-ignore",
        "--no-index",
        "-v",
        "--",
        relative,
        check=False,
    )
    detail = (result.stdout or result.stderr).decode("utf-8", "replace").strip()
    return detail or "no matching ignore rule"


def _raise_unignored(root: Path, relative: str, *, label: str) -> None:
    if _ignore_status(root, relative):
        return
    diagnostic = _ignore_diagnostic(root, relative)
    raise WorktreeError(
        f"{label} is not ignored: {relative}; "
        f"git check-ignore -v: {diagnostic}"
    )


def _read_exclude_bytes(path: Path) -> tuple[bool, bytes]:
    try:
        return True, path.read_bytes()
    except FileNotFoundError:
        return False, b""
    except OSError as error:
        raise WorktreeError(
            f"unable to read repository-local exclude {path}: {error}"
        ) from error


def _exclude_candidate(existing: bytes, missing_rules: Sequence[str]) -> bytes:
    """Append missing managed rules without changing existing bytes."""

    lines = {line.rstrip(b"\r") for line in existing.split(b"\n")}
    missing = [
        rule.encode("ascii")
        for rule in missing_rules
        if rule.encode("ascii") not in lines
    ]
    if not missing:
        return existing
    additions: list[bytes] = []
    if _MANAGED_EXCLUDES_COMMENT not in lines:
        additions.append(_MANAGED_EXCLUDES_COMMENT)
    additions.extend(missing)
    suffix = b"\n".join(additions) + b"\n"
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    return existing + separator + suffix


def _unlink_exclude_temp(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise WorktreeError(
            f"unable to remove temporary repository-local exclude {path}: {error}"
        ) from error


def _write_exclude_temp(path: Path, payload: bytes, mode: int | None) -> None:
    created = False
    try:
        with path.open("xb") as stream:
            created = True
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(path, mode)
    except OSError as error:
        # ``xb`` may fail because a stale temp path already exists.  Only
        # unlink a file after this invocation successfully created it.
        if created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                raise WorktreeError(
                    f"unable to remove temporary repository-local exclude {path}: "
                    f"{cleanup_error}; original write error: {error}"
                ) from error
        raise WorktreeError(
            f"unable to write temporary repository-local exclude {path}: {error}"
        ) from error


def initialize_local_excludes(
    root: Path, prompt_id: str, worktree: Path
) -> ExcludeResult:
    """Initialize and verify this cycle's repository-local ignore rules.

    The replacement is intentionally bounded: one concurrent byte drift may
    trigger a complete recomputation, while a second drift fails closed.
    """

    root = _repo_root(Path(root))
    exclude = _repository_local_exclude(root)
    targets = _exclude_targets(root, prompt_id, worktree)

    for attempt in range(2):
        existed, original = _read_exclude_bytes(exclude)
        missing: list[tuple[str, str]] = []
        for relative, rule in targets:
            if not _ignore_status(root, relative):
                missing.append((relative, rule))
        if not missing:
            return ExcludeResult(
                path=exclude,
                changed=False,
                verified_paths=tuple(relative for relative, _ in targets),
            )

        candidate = _exclude_candidate(original, [rule for _, rule in missing])
        if candidate == original and existed:
            # The missing behavior is caused by a higher-precedence rule (for
            # example a project-level negation); duplicating identical text
            # cannot repair it and would violate idempotence.
            _raise_unignored(root, missing[0][0], label="ignore target")

        temporary = exclude.parent / f".{exclude.name}.{uuid.uuid4().hex}.tmp"
        mode: int | None = None
        if existed:
            try:
                mode = stat.S_IMODE(exclude.stat().st_mode)
            except OSError as error:
                raise WorktreeError(
                    f"unable to inspect repository-local exclude {exclude}: {error}"
                ) from error
        temporary_owned = False
        try:
            try:
                exclude.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise WorktreeError(
                    f"unable to prepare repository-local exclude directory "
                    f"{exclude.parent}: {error}"
                ) from error
            _write_exclude_temp(temporary, candidate, mode)
            temporary_owned = True
            current_exists, current = _read_exclude_bytes(exclude)
            if current_exists != existed or current != original:
                if attempt == 1:
                    raise WorktreeError(
                        "repository-local exclude changed concurrently before replacement"
                    )
                continue
            try:
                os.replace(temporary, exclude)
            except OSError as error:
                raise WorktreeError(
                    f"unable to replace repository-local exclude {exclude}: {error}"
                ) from error
        finally:
            if temporary_owned and temporary.exists():
                _unlink_exclude_temp(temporary)

        missing_after = [
            relative
            for relative, _ in targets
            if not _ignore_status(root, relative)
        ]
        if missing_after:
            _raise_unignored(root, missing_after[0], label="ignore target")
        return ExcludeResult(
            path=exclude,
            changed=True,
            verified_paths=tuple(relative for relative, _ in targets),
        )

    # The loop either returns or raises on the second drift.  Keep a defensive
    # guard in case its bounds are changed during future maintenance.
    raise WorktreeError("repository-local exclude initialization did not complete")


def _require_ignored_worktree(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root).as_posix() + "/"
    except ValueError as error:
        raise WorktreeError("derived worktree path escapes the original repository") from error
    _raise_unignored(root, relative, label="worktree directory")


def _runtime_ignore_paths(cycle: WorktreeCycle) -> tuple[str, str]:
    if cycle.prompt_id is None:
        raise WorktreeError("cycle prompt_id is required to verify runtime ignores")
    prompt_id = _validate_exclude_prompt_id(cycle.prompt_id)
    return (
        f".prompt-evals/{prompt_id}/reports/",
        f".prompt-evals/{prompt_id}/.runtime/",
    )


def verify_runtime_ignores(cycle: WorktreeCycle) -> tuple[str, str]:
    """Verify both runtime output directories from the linked worktree."""

    if not isinstance(cycle, WorktreeCycle):
        raise TypeError("verify_runtime_ignores expects a WorktreeCycle")
    _validate_managed_cycle(cycle)
    paths = _runtime_ignore_paths(cycle)
    for relative in paths:
        _raise_unignored(cycle.worktree, relative, label="runtime path")
    return paths


def create_cycle(
    original_repo: Path,
    prompt_id: str,
    *,
    branch: str | None = None,
    prompt_path: Path | str | None = None,
    state_path: Path | None = None,
) -> WorktreeCycle:
    """Create a dedicated branch/worktree rooted at the original ``HEAD``."""

    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise WorktreeError("prompt_id must be a non-empty string")
    if branch is not None:
        _validate_branch_name(branch)
    root = _repo_root(Path(original_repo))
    base = _primary_workspace_head(root)
    resolved_prompt: str | None
    if prompt_path is not None:
        resolved_prompt = _relative_path(root, prompt_path, label="prompt path")
    else:
        provisional = WorktreeCycle(root, root, "", base, prompt_id=prompt_id)
        resolved_prompt = _discover_prompt_path(provisional)

    identity = uuid.uuid4().hex[:12]
    slug = _safe_slug(prompt_id)
    selected_branch = branch if branch is not None else f"stabilizing-prompts/{slug}-{identity}"
    _validate_branch_name(selected_branch)
    worktrees_root = root / ".worktrees"
    if worktrees_root.exists() and not worktrees_root.is_dir():
        raise WorktreeError(".worktrees is not a directory")
    branch_result = _git(root, "check-ref-format", "--branch", selected_branch, check=False)
    if branch_result.returncode != 0:
        raise WorktreeError(f"invalid branch: {selected_branch}")
    existing_branch = _git(
        root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{selected_branch}",
        check=False,
    )
    if existing_branch.returncode == 0:
        raise WorktreeError(f"branch already exists: {selected_branch}")
    if existing_branch.returncode != 1:
        detail = (existing_branch.stderr or existing_branch.stdout).decode(
            "utf-8", "replace"
        ).strip()
        raise WorktreeError(f"unable to verify branch availability: {detail}")
    namespace_result = _git(
        root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/heads",
        check=False,
    )
    if namespace_result.returncode != 0:
        detail = (namespace_result.stderr or namespace_result.stdout).decode(
            "utf-8", "replace"
        ).strip()
        raise WorktreeError(f"unable to verify branch namespace availability: {detail}")
    existing_names = namespace_result.stdout.decode("utf-8", "replace").splitlines()
    for existing_name in existing_names:
        if (
            existing_name.startswith(selected_branch + "/")
            or selected_branch.startswith(existing_name + "/")
        ):
            raise WorktreeError(
                "branch namespace collision: "
                f"{selected_branch} conflicts with existing branch {existing_name}"
            )

    selected_worktree = _cycle_worktree_path(root, prompt_id, identity)
    if selected_worktree.exists():
        raise WorktreeError(f"worktree path already exists: {selected_worktree}")
    if state_path is not None:
        # Reject a state location inside the not-yet-created cycle before the
        # initializer or any worktree/branch creation can leave resources
        # without an authoritative recovery file.
        provisional = WorktreeCycle(root, selected_worktree, selected_branch, base)
        _validate_state_path(provisional, Path(state_path))
    exclude_result = initialize_local_excludes(root, prompt_id, selected_worktree)
    if selected_worktree.relative_to(root).as_posix() + "/" not in exclude_result.verified_paths:
        raise WorktreeError(
            "worktree directory is not covered by repository-local exclude initialization: "
            f"{selected_worktree.relative_to(root).as_posix()}/"
        )
    _require_ignored_worktree(root, selected_worktree)
    selected_worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", selected_branch, str(selected_worktree), base)
    cycle = WorktreeCycle(
        original_repo=root,
        worktree=selected_worktree,
        branch=selected_branch,
        cycle_base_commit=base,
        prompt_id=prompt_id,
        prompt_path=resolved_prompt,
        branch_ref=f"refs/heads/{selected_branch}",
        branch_origin="custom" if branch is not None else "generated",
        branch_created_by_cycle=True,
        exclude_initialized=True,
        precreate_ignore_verified=True,
    )
    if state_path is not None:
        try:
            save_cycle(cycle, Path(state_path))
        except BaseException as error:
            wrapped = WorktreeError(
                "unable to persist created cycle identity: "
                f"state={Path(state_path)}; worktree={cycle.worktree}; "
                f"branch={cycle.branch}: {error}"
            )
            setattr(wrapped, "cycle", cycle)
            raise wrapped from error
    try:
        verify_runtime_ignores(cycle)
    except BaseException as error:
        wrapped = WorktreeError(
            "post-create runtime ignore gate failed: "
            f"state={Path(state_path) if state_path is not None else '<not persisted>'}; "
            f"worktree={cycle.worktree}; branch={cycle.branch}: {error}"
        )
        setattr(wrapped, "cycle", cycle)
        raise wrapped from error
    cycle = replace(cycle, runtime_ignores_verified=True)
    if state_path is not None:
        try:
            save_cycle(cycle, Path(state_path))
        except BaseException as error:
            wrapped = WorktreeError(
                "unable to persist completed ignore gates: "
                f"state={Path(state_path)}; worktree={cycle.worktree}; "
                f"branch={cycle.branch}: {error}"
            )
            setattr(wrapped, "cycle", replace(cycle, runtime_ignores_verified=False))
            raise wrapped from error
    return cycle


def _changed_paths(cycle: WorktreeCycle, final_commit: str) -> tuple[str, ...]:
    result = _git(
        cycle.worktree,
        "diff",
        "--name-status",
        "--no-renames",
        "-z",
        cycle.cycle_base_commit,
        final_commit,
        "--",
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DeliveryError(f"unable to enumerate cycle changes: {detail}")
    fields = result.stdout.split(b"\x00")
    paths: list[str] = []
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode("ascii", "replace")
        index += 1
        if index >= len(fields) or not fields[index]:
            raise DeliveryError("Git returned a malformed changed-path list")
        if status not in {"A", "D", "M"}:
            raise DeliveryError(f"unsupported Git change status: {status}")
        raw_path = os.fsdecode(fields[index])
        index += 1
        # Runtime/report paths are intentionally enumerated here so the
        # allowlist can reject them from the generated patch.  Rejecting them
        # while parsing would prevent a safe filtered patch from being built.
        paths.append(_normalize_relative(raw_path, label="changed path"))
    if any(fields[index:]):
        raise DeliveryError("Git returned a malformed changed-path list")
    return tuple(dict.fromkeys(paths))


def _allowlist_paths(
    cycle: WorktreeCycle,
    allowlist: Sequence[str] | set[str] | frozenset[str] | Mapping[str, object],
    *,
    result: str,
    changed_paths: Sequence[str] | None = None,
) -> frozenset[str]:
    if isinstance(allowlist, (str, bytes)):
        raise AllowlistError("delivery allowlist must be a collection of paths")
    if isinstance(allowlist, Mapping):
        entries = list(allowlist.keys())
    else:
        entries = list(allowlist)
    if result not in {"success", "failure"}:
        raise DeliveryError("delivery result must be success or failure")
    resolved_prompt = _discover_prompt_path(cycle)
    if "prompt" in entries and result == "success":
        if resolved_prompt is None and changed_paths is not None:
            candidates = [
                path
                for path in changed_paths
                if path.casefold().endswith(".md")
                and not path.startswith(".prompt-evals/")
            ]
            if len(candidates) == 1:
                resolved_prompt = candidates[0]
        if resolved_prompt is None:
            raise AllowlistError(
                "symbolic prompt allowlist entry cannot resolve a canonical Prompt path"
            )
        resolved_prompt = _reject_unsafe_delivery_path(resolved_prompt, label="prompt path")
    elif resolved_prompt is not None:
        resolved_prompt = _reject_unsafe_delivery_path(resolved_prompt, label="prompt path")
    eval_prefix = _evaluation_prefix(cycle)
    resolved: set[str] = set()
    for entry in entries:
        if not isinstance(entry, str):
            raise AllowlistError("delivery allowlist entries must be strings")
        if entry == "prompt":
            if result == "success" and resolved_prompt is not None:
                resolved.add(resolved_prompt)
            continue
        if entry in _ASSET_NAMES:
            if eval_prefix is None:
                raise AllowlistError("prompt_id is required for evaluation assets")
            resolved.add(f"{eval_prefix}{entry}")
            continue
        canonical = _reject_unsafe_delivery_path(entry, label="allowlist path")
        if canonical == resolved_prompt:
            if result == "failure":
                continue
            resolved.add(canonical)
            continue
        if eval_prefix is not None and canonical.startswith(eval_prefix):
            resolved.add(canonical)
            continue
        # A custom path may name a repository file explicitly, but only an
        # exact canonical path is accepted; no glob or directory allowlists.
        if canonical.endswith("/"):
            raise AllowlistError("delivery allowlist cannot name a directory")
        resolved.add(canonical)
    if result == "failure" and resolved_prompt is not None:
        resolved.discard(resolved_prompt)
    return frozenset(resolved)


def _canonical_allowlist_paths(
    cycle: WorktreeCycle,
    *,
    result: str,
    changed_paths: Sequence[str] | None = None,
) -> frozenset[str]:
    """Derive the only allowlist accepted during delivery preflight.

    The persisted manifest is evidence about what was built, never an input
    to this decision.  The cycle identity and the fixed result policy are the
    trust anchors; the symbolic ``prompt`` entry is resolved from the cycle's
    immutable contract (Ruling 1).
    """

    if result not in {"success", "failure"}:
        raise DeliveryError("delivery result must be success or failure")
    base_has_contract, _ = _base_contract_prompt_path(cycle)
    if not base_has_contract:
        raise AllowlistError(
            "canonical Prompt path cannot be resolved without committed prompt-contract.yaml"
        )
    prompt = _discover_prompt_path(cycle)
    if prompt is None:
        raise AllowlistError("canonical Prompt path cannot be resolved")
    defaults = SUCCESS_ALLOWLIST if result == "success" else FAILURE_ALLOWLIST
    return _allowlist_paths(cycle, defaults, result=result, changed_paths=changed_paths)


def _commit_blob(cycle: WorktreeCycle, commit: str, path: str) -> bytes | None:
    type_result = _git(cycle.worktree, "cat-file", "-t", f"{commit}:{path}", check=False)
    if type_result.returncode != 0:
        return None
    object_type = type_result.stdout.decode("ascii", "replace").strip()
    if object_type != "blob":
        raise DeliveryError(f"delivery path is not a regular file: {path}")
    result = _git(cycle.worktree, "show", f"{commit}:{path}", check=False)
    if result.returncode != 0:
        return None
    return result.stdout


def _hash_bytes(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value) is not None


def _working_tree_matches_commit(cycle: WorktreeCycle, path: str) -> bool:
    """Compare a target with the cycle base through Git's clean filters."""

    for arguments in (
        ("diff", "--quiet", cycle.cycle_base_commit, "--", path),
        ("diff", "--cached", "--quiet", cycle.cycle_base_commit, "--", path),
    ):
        result = _git(cycle.original_repo, *arguments, check=False)
        if result.returncode == 1:
            return False
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DeliveryError(f"unable to compare cycle source {path}: {detail}")
    return True


def _build_source_hash(cycle: WorktreeCycle, path: str) -> str | None:
    """Hash the exact original-workspace bytes after proving base identity."""

    base_blob = _commit_blob(cycle, cycle.cycle_base_commit, path)
    current = _current_file_bytes(cycle.original_repo, path)
    if base_blob is None:
        if current is not None:
            raise DeliveryConflict(
                f"source hash conflict for {path}: expected None, got {_hash_bytes(current)}"
            )
        return None
    if not _working_tree_matches_commit(cycle, path):
        raise DeliveryConflict(f"source hash conflict for {path}: original target is dirty")
    if current is None:
        raise DeliveryConflict(f"source hash conflict for {path}: original target is missing")
    return _hash_bytes(current)


def _build_destination_hash(cycle: WorktreeCycle, final: str, path: str) -> str | None:
    """Hash the checked-out final worktree representation used by Git apply."""

    # A final result must be committed and clean.  The file bytes are read from
    # the worktree rather than the Git blob so Windows clean/smudge line-ending
    # conversion is represented exactly as it will be after application.
    result = _git(cycle.worktree, "diff", "--quiet", final, "--", path, check=False)
    if result.returncode == 1:
        raise DeliveryError(f"final worktree target is dirty: {path}")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DeliveryError(f"unable to inspect final worktree target: {detail}")
    return _hash_bytes(_current_file_bytes(cycle.worktree, path))


def _final_commit(cycle: WorktreeCycle, value: str | None) -> str:
    selected = value or cycle.final_worktree_commit
    if selected is None:
        selected = _git_text(cycle.worktree, "rev-parse", "--verify", "HEAD")
    result = _git(cycle.worktree, "rev-parse", "--verify", f"{selected}^{{commit}}", check=False)
    if result.returncode != 0:
        raise DeliveryError("final worktree commit is not a valid Git commit")
    resolved = result.stdout.decode("utf-8", "replace").strip()
    ancestry = _git(
        cycle.worktree,
        "merge-base",
        "--is-ancestor",
        cycle.cycle_base_commit,
        resolved,
        check=False,
    )
    if ancestry.returncode != 0:
        raise DeliveryConflict("final worktree commit is not based on the cycle base commit")
    return resolved


def _assert_cycle_base(cycle: WorktreeCycle) -> None:
    """Verify the immutable cycle base still anchors both repositories."""

    try:
        resolved = _git_text(
            cycle.original_repo,
            "rev-parse",
            "--verify",
            f"{cycle.cycle_base_commit}^{{commit}}",
        )
        original_head = _git_text(cycle.original_repo, "rev-parse", "--verify", "HEAD")
        worktree_base = _git_text(
            cycle.worktree,
            "rev-parse",
            "--verify",
            f"{cycle.cycle_base_commit}^{{commit}}",
        )
    except WorktreeError as error:
        raise DeliveryConflict("cycle base commit cannot be verified") from error
    if resolved != cycle.cycle_base_commit or worktree_base != cycle.cycle_base_commit:
        raise DeliveryConflict("cycle base commit is not an immutable canonical commit")
    if original_head != cycle.cycle_base_commit:
        raise DeliveryConflict("original repository HEAD moved after cycle creation")


def _current_worktree_head(cycle: WorktreeCycle) -> str:
    """Return the current cycle worktree HEAD after checking its identity."""

    try:
        worktree_root = Path(
            _git_text(cycle.worktree, "rev-parse", "--show-toplevel")
        ).resolve(strict=False)
        branch = _git_text(cycle.worktree, "branch", "--show-current")
        head = _git_text(cycle.worktree, "rev-parse", "--verify", "HEAD")
    except WorktreeError as error:
        raise DeliveryConflict("cycle worktree identity cannot be verified") from error
    if worktree_root != cycle.worktree:
        raise DeliveryConflict("cycle worktree path is not the recorded worktree")
    if branch != cycle.branch:
        raise DeliveryConflict("cycle worktree branch does not match cycle metadata")
    return _final_commit(cycle, head)


def _assert_worktree_clean(cycle: WorktreeCycle, final: str) -> None:
    """Reject tracked worktree edits relative to its current committed HEAD."""

    for arguments in (
        ("diff", "--quiet", final, "--"),
        ("diff", "--cached", "--quiet", final, "--"),
    ):
        result = _git(cycle.worktree, *arguments, check=False)
        if result.returncode == 1:
            raise DeliveryConflict("cycle worktree content changed after its final commit")
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DeliveryError(f"unable to inspect cycle worktree content: {detail}")


def _assert_worktree_snapshot(cycle: WorktreeCycle, patch: "DeliveryPatch") -> None:
    """Check the cycle HEAD and every delivered target immediately around apply."""

    final = _current_worktree_head(cycle)
    if final != patch.final_worktree_commit:
        raise DeliveryConflict("cycle worktree HEAD changed after canonical patch generation")
    _assert_worktree_clean(cycle, final)
    for path, expected in patch.destination_hashes.items():
        actual = _current_hash(cycle.worktree, path)
        if actual != expected:
            raise DeliveryConflict(f"cycle worktree target changed: {path}")


@dataclass(frozen=True, slots=True)
class DeliveryPatch:
    """A filtered, hash-bound patch ready for preflight/application."""

    text: str
    paths: tuple[str, ...]
    source_hashes: Mapping[str, str | None]
    destination_hashes: Mapping[str, str | None]
    cycle_base_commit: str
    final_worktree_commit: str
    result: str = "success"
    prompt_path: str | None = None
    allowlist_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "paths", tuple(self.paths))
        object.__setattr__(self, "source_hashes", dict(self.source_hashes))
        object.__setattr__(self, "destination_hashes", dict(self.destination_hashes))

    @property
    def patch(self) -> str:
        return self.text

    @property
    def diff(self) -> str:
        return self.text

    @property
    def content(self) -> str:
        return self.text

    def to_dict(self) -> dict[str, object]:
        return {
            "paths": list(self.paths),
            "source_hashes": dict(self.source_hashes),
            "destination_hashes": dict(self.destination_hashes),
            "cycle_base_commit": self.cycle_base_commit,
            "final_worktree_commit": self.final_worktree_commit,
            "result": self.result,
            "prompt_path": self.prompt_path,
            "allowlist_paths": list(self.allowlist_paths),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object], *, text: str) -> "DeliveryPatch":
        raw_paths = value.get("paths", ())
        if not isinstance(raw_paths, Sequence) or isinstance(raw_paths, (str, bytes)):
            raise DeliveryError("patch manifest paths must be a list")
        source = value.get("source_hashes", {})
        destination = value.get("destination_hashes", {})
        if not isinstance(source, Mapping) or not isinstance(destination, Mapping):
            raise DeliveryError("patch manifest hashes must be mappings")
        allowlist_paths = value.get("allowlist_paths", ())
        if not isinstance(allowlist_paths, Sequence) or isinstance(allowlist_paths, (str, bytes)):
            raise DeliveryError("patch manifest allowlist_paths must be a list")
        return cls(
            text=text,
            paths=tuple(str(path) for path in raw_paths),
            source_hashes={str(key): value for key, value in source.items()},
            destination_hashes={str(key): value for key, value in destination.items()},
            cycle_base_commit=str(value.get("cycle_base_commit", "")),
            final_worktree_commit=str(value.get("final_worktree_commit", "")),
            result=str(value.get("result", "success")),
            prompt_path=value.get("prompt_path") if isinstance(value.get("prompt_path"), str) else None,
            allowlist_paths=tuple(str(path) for path in allowlist_paths),
        )


Patch = DeliveryPatch


def build_delivery_patch(
    cycle: WorktreeCycle,
    allowlist: Sequence[str] | set[str] | frozenset[str] | Mapping[str, object] | None = None,
    *,
    final_commit: str | None = None,
    result: str = "success",
) -> DeliveryPatch:
    """Build a patch containing only paths rejected into the allowlist boundary."""

    if not isinstance(cycle, WorktreeCycle):
        raise TypeError("build_delivery_patch expects a WorktreeCycle")
    if result not in {"success", "failure"}:
        raise DeliveryError("delivery result must be success or failure")
    _validate_managed_cycle(cycle)
    final = _final_commit(cycle, final_commit)
    _assert_prompt_contract_identity(cycle, final)
    changed = _changed_paths(cycle, final)
    canonical = _canonical_allowlist_paths(
        cycle,
        result=result,
        changed_paths=changed,
    )
    if allowlist is None:
        allowed = canonical
    else:
        requested = _allowlist_paths(
            cycle,
            allowlist,
            result=result,
            changed_paths=changed,
        )
        if not requested.issubset(canonical):
            outside = sorted(requested - canonical)
            raise AllowlistError(
                "requested delivery allowlist contains unsupported paths: "
                + ", ".join(outside)
            )
        allowed = requested
    selected = tuple(path for path in changed if path in allowed)
    source_hashes = {path: _build_source_hash(cycle, path) for path in selected}
    destination_hashes = {
        path: _build_destination_hash(cycle, final, path)
        for path in selected
    }
    if any(destination_hashes[path] is None for path in selected):
        raise DeliveryError("deletion delivery is not supported")
    patch_text = ""
    if selected:
        result_patch = _git(
            cycle.worktree,
            "diff",
            "--binary",
            "--full-index",
            cycle.cycle_base_commit,
            final,
            "--",
            *selected,
        )
        if result_patch.returncode != 0:
            detail = result_patch.stderr.decode("utf-8", "replace").strip()
            raise DeliveryError(f"unable to build delivery patch: {detail}")
        patch_text = result_patch.stdout.decode("utf-8", "surrogateescape")
        # Let Git parse the generated patch and require the actual section
        # paths to be exactly the selected delivery paths.
        if _native_patch_paths(cycle.original_repo, patch_text) != selected:
            raise DeliveryError("generated patch paths do not match selected delivery paths")
    return DeliveryPatch(
        text=patch_text,
        paths=selected,
        source_hashes=source_hashes,
        destination_hashes=destination_hashes,
        cycle_base_commit=cycle.cycle_base_commit,
        final_worktree_commit=final,
        result=result,
        prompt_path=_discover_prompt_path(cycle),
        allowlist_paths=tuple(sorted(canonical)),
    )


def _native_patch_paths(root: Path, text: str) -> tuple[str, ...]:
    """Ask Git to parse a patch and report its concrete paths.

    Git remains responsible for the patch grammar.  ``--numstat -z`` returns
    one NUL-delimited record per section, preserving spaces and other valid
    path characters without a second, incomplete parser in this module.
    """

    if not isinstance(text, str):
        raise DeliveryError("delivery patch must be text")
    if not text:
        return ()
    result = _git(
        root,
        "apply",
        "--numstat",
        "-z",
        "--no-3way",
        "--whitespace=nowarn",
        "-",
        input_data=text,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DeliveryError(f"Git rejected the delivery patch: {detail or 'invalid patch'}")
    paths: list[str] = []
    for record in result.stdout.split(b"\x00"):
        if not record:
            continue
        fields = record.split(b"\t", 2)
        if len(fields) != 3 or fields[0] == b"-" or fields[1] == b"-":
            raise DeliveryError("binary delivery patches are not supported")
        try:
            raw_path = os.fsdecode(fields[2])
        except UnicodeError as error:
            raise DeliveryError("delivery patch path is not valid for this filesystem") from error
        paths.append(_normalize_relative(raw_path, label="patch path"))
    return tuple(paths)


def _current_file_bytes(root: Path, relative: str) -> bytes | None:
    current = root / Path(relative)
    cursor = root
    for part in Path(relative).parts:
        if cursor.is_symlink():
            raise DeliveryConflict(f"delivery target traverses a symbolic link: {relative}")
        cursor = cursor / part
    if current.is_symlink():
        raise DeliveryConflict(f"delivery target is a symbolic link: {relative}")
    if not current.exists():
        return None
    if not current.is_file():
        raise DeliveryConflict(f"delivery target is not a regular file: {relative}")
    try:
        return current.read_bytes()
    except OSError as error:
        raise DeliveryConflict(f"unable to read delivery target: {relative}") from error


def _current_hash(root: Path, relative: str) -> str | None:
    return _hash_bytes(_current_file_bytes(root, relative))


def _index_changed(root: Path, relative: str) -> bool:
    result = _git(root, "diff", "--cached", "--quiet", "--", relative, check=False)
    if result.returncode == 1:
        return True
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise DeliveryError(f"unable to inspect staged delivery target: {detail}")
    return False


def _canonical_delivery_patch(cycle: WorktreeCycle, *, result: str) -> DeliveryPatch:
    """Regenerate delivery data solely from trusted cycle/worktree state."""

    if result not in {"success", "failure"}:
        raise DeliveryError("delivery result must be success or failure")
    _assert_cycle_base(cycle)
    final = _current_worktree_head(cycle)
    _assert_worktree_clean(cycle, final)
    return build_delivery_patch(cycle, final_commit=final, result=result)


def _assert_supplied_patch_paths(
    cycle: WorktreeCycle,
    supplied: DeliveryPatch,
    canonical: DeliveryPatch,
) -> None:
    """Reject extra persisted paths while keeping canonical data authoritative.

    A persisted patch is useful as an internal transport object, but it is not
    trusted.  Only its requested result and path shape are checked; the
    canonical patch regenerated from the cycle is what the caller applies.
    Git parses the supplied text to catch extra sections without reproducing
    Git's patch grammar in Python.
    """

    if supplied.result != canonical.result:
        raise DeliveryError("patch result does not match canonical delivery metadata")
    if supplied.paths != canonical.paths:
        if any(path not in canonical.allowlist_paths for path in supplied.paths):
            raise AllowlistError("patch paths do not match the canonical delivery allowlist")
        raise DeliveryError("patch paths do not match the canonical cycle diff")
    actual_paths = _native_patch_paths(cycle.original_repo, supplied.text)
    if actual_paths != canonical.paths:
        raise DeliveryError("patch sections do not match canonical delivery paths")


def _check_sources(cycle: WorktreeCycle, patch: DeliveryPatch, expected: Mapping[str, object] | None) -> None:
    # ``patch.source_hashes`` is safe here only because callers first compare
    # the patch with a newly generated canonical patch.  An optional external
    # map may constrain the check, but it can never replace canonical hashes.
    expected_hashes: dict[str, object] = dict(patch.source_hashes)
    if expected is not None:
        normalized_expected: dict[str, object] = {}
        for raw_path, digest in expected.items():
            try:
                path = _relative_path(cycle.original_repo, raw_path, label="source hash path")
            except AllowlistError as error:
                raise DeliveryConflict("provided source hash path is unsafe") from error
            if path == "prompt":
                prompt = _discover_prompt_path(cycle)
                if prompt is None:
                    raise DeliveryConflict("source hash prompt path cannot be resolved")
                path = prompt
            normalized_expected[path] = digest
        if tuple(normalized_expected.items()) != tuple(expected_hashes.items()):
            raise DeliveryConflict("provided source hashes do not match canonical cycle content")
    for path in patch.paths:
        if path not in expected_hashes:
            raise DeliveryConflict(f"source hash is missing for {path}")
        expected_digest = expected_hashes[path]
        if expected_digest is not None and not isinstance(expected_digest, str):
            raise DeliveryConflict(f"source hash is invalid for {path}")
        actual = _current_hash(cycle.original_repo, path)
        if actual != expected_digest:
            raise DeliveryConflict(
                f"source hash conflict for {path}: expected {expected_digest}, got {actual}"
            )
        if _index_changed(cycle.original_repo, path):
            raise DeliveryConflict(f"target has staged changes: {path}")


def preflight_patch(
    cycle: WorktreeCycle,
    patch: DeliveryPatch | Path | str | None = None,
    *,
    expected_source_hashes: Mapping[str, object] | None = None,
) -> DeliveryPatch:
    """Validate allowlisted paths, source hashes, and ``git apply --check``."""

    if not isinstance(cycle, WorktreeCycle):
        raise TypeError("preflight_patch expects a WorktreeCycle")
    _validate_managed_cycle(cycle)
    if patch is None:
        canonical = _canonical_delivery_patch(cycle, result="success")
    else:
        if isinstance(patch, (str, Path)):
            raise DeliveryError("a patch file requires its patch manifest metadata")
        if not isinstance(patch, DeliveryPatch):
            raise TypeError("preflight_patch expects a DeliveryPatch")
        if not isinstance(patch.result, str) or patch.result not in {"success", "failure"}:
            raise DeliveryError("delivery result must be success or failure")
        # Regenerate from cycle state.  The persisted object is only checked
        # for its result and path shape; its text and hashes are never applied.
        canonical = _canonical_delivery_patch(cycle, result=patch.result)
        _assert_supplied_patch_paths(cycle, patch, canonical)
    # Source checks intentionally happen immediately before Git's check.  The
    # application path repeats them after this function and before apply.
    _check_sources(cycle, canonical, expected_source_hashes)
    if canonical.text:
        result = _git(
            cycle.original_repo,
            "apply",
            "--check",
            "--whitespace=nowarn",
            "--no-3way",
            "-",
            input_data=canonical.text,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DeliveryConflict(f"delivery patch conflict: {detail or 'git apply --check failed'}")
    return canonical


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: str
    existed: bool
    data: bytes | None
    mode: int | None
    parent_dirs: tuple[Path, ...] = field(default=())


def _snapshot_targets(root: Path, paths: Sequence[str]) -> tuple[_Snapshot, ...]:
    snapshots: list[_Snapshot] = []
    for relative in paths:
        target = root / Path(relative)
        parent_dirs: list[Path] = []
        parent = target.parent
        while parent != root and not parent.exists():
            parent_dirs.append(parent)
            parent = parent.parent
        data = _current_file_bytes(root, relative)
        mode = None
        if data is not None:
            try:
                mode = stat.S_IMODE(target.stat().st_mode)
            except OSError as error:
                raise DeliveryError(f"unable to snapshot delivery target: {relative}") from error
        snapshots.append(
            _Snapshot(
                path=relative,
                existed=data is not None,
                data=data,
                mode=mode,
                parent_dirs=tuple(parent_dirs),
            )
        )
    return tuple(snapshots)


def _restore_snapshots(root: Path, snapshots: Sequence[_Snapshot]) -> None:
    errors: list[str] = []
    for snapshot in snapshots:
        target = root / Path(snapshot.path)
        try:
            if snapshot.existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        raise OSError("target became a directory")
                    target.unlink()
                target.write_bytes(snapshot.data or b"")
                if snapshot.mode is not None:
                    os.chmod(target, snapshot.mode)
            else:
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        raise OSError("target became a directory")
                    target.unlink()
        except OSError as error:
            errors.append(f"{snapshot.path}: {error}")
    # Remove only empty parent directories that did not exist at snapshot time.
    # This is narrowly scoped scaffolding cleanup, never worktree/branch cleanup.
    seen: set[Path] = set()
    for snapshot in snapshots:
        for parent in snapshot.parent_dirs:
            if parent in seen:
                continue
            seen.add(parent)
            try:
                parent.rmdir()
            except OSError:
                pass
    if errors:
        raise DeliveryError("unable to restore delivery targets: " + "; ".join(errors))


def apply_delivery_patch(
    cycle: WorktreeCycle,
    patch: DeliveryPatch | None = None,
    *,
    expected_source_hashes: Mapping[str, object] | None = None,
) -> DeliveryPatch:
    """Preflight, apply without staging, verify, and rollback on any error."""

    if not isinstance(cycle, WorktreeCycle):
        raise TypeError("apply_delivery_patch expects a WorktreeCycle")
    _validate_managed_cycle(cycle)
    prepared = preflight_patch(
        cycle,
        patch,
        expected_source_hashes=expected_source_hashes,
    )
    # Preflight may return before a mutating user action.  Regenerate once more
    # immediately before application so the current cycle state, not a stale
    # transport object, supplies the bytes and hashes.
    latest = _canonical_delivery_patch(cycle, result=prepared.result)
    prepared = latest
    if not prepared.text:
        return prepared

    # Repeat source checks after preflight and immediately before snapshot/apply
    # so a concurrent user edit cannot slip between the checks.
    _check_sources(cycle, prepared, expected_source_hashes)
    snapshots = _snapshot_targets(cycle.original_repo, prepared.paths)
    try:
        # These checks are intentionally adjacent to the mutating Git command;
        # they also run again after application so a concurrent worktree edit
        # triggers rollback rather than being silently accepted.
        _check_sources(cycle, prepared, expected_source_hashes)
        _assert_cycle_base(cycle)
        _assert_prompt_contract_identity(cycle, prepared.final_worktree_commit)
        _assert_worktree_snapshot(cycle, prepared)
        # Keep the original HEAD and committed Prompt identity checks directly
        # next to Git's mutating command as the final stale-state guard.
        _assert_cycle_base(cycle)
        _assert_prompt_contract_identity(cycle, prepared.final_worktree_commit)
        result = _git(
            cycle.original_repo,
            "apply",
            "--whitespace=nowarn",
            "--no-3way",
            "-",
            input_data=prepared.text,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip()
            raise DeliveryConflict(f"delivery patch application failed: {detail or 'git apply failed'}")
        _assert_worktree_snapshot(cycle, prepared)
        for path, expected in prepared.destination_hashes.items():
            actual = _current_hash(cycle.original_repo, path)
            if actual != expected:
                raise DeliveryError(
                    f"destination hash mismatch for {path}: expected {expected}, got {actual}"
                )
        return prepared
    except BaseException as error:
        # Restore even if Git partially applied a patch or verification raised.
        # Chaining preserves the original failure while making rollback failure
        # visible to the caller.
        try:
            _restore_snapshots(cycle.original_repo, snapshots)
        except BaseException as restore_error:
            raise DeliveryError(f"delivery failed and rollback failed: {restore_error}") from error
        raise


def save_cycle_atomic(cycle: WorktreeCycle, path: Path) -> None:
    """Persist cycle state with a same-directory durable replacement."""

    destination = _validate_state_path(cycle, Path(path))
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise WorktreeError(f"unable to atomically save cycle state: {error}") from error
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


def save_cycle(cycle: WorktreeCycle, path: Path) -> None:
    """Compatibility alias for the atomic state writer."""

    save_cycle_atomic(cycle, path)


def load_cycle(path: Path, *, require_current: bool = False) -> WorktreeCycle:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as error:
        raise WorktreeError(f"cycle state is unreadable: {error}") from None
    if not isinstance(value, Mapping):
        raise WorktreeError("cycle state must contain an object")
    if require_current:
        ownership_fields = (
            "state_version",
            "branch_ref",
            "branch_origin",
            "branch_created_by_cycle",
        )
        if any(field_name not in value for field_name in ownership_fields):
            raise WorktreeError("current lifecycle state is required")
    cycle = WorktreeCycle.from_dict(value)
    _validate_state_path(cycle, Path(path))
    if require_current:
        _validate_current_cycle(cycle)
    return cycle


def save_patch(patch: DeliveryPatch, patch_path: Path, manifest_path: Path) -> None:
    patch_path = Path(patch_path)
    manifest_path = Path(manifest_path)
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text(patch.text, encoding="utf-8", errors="surrogateescape")
    manifest_path.write_text(
        json.dumps(patch.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_patch(patch_path: Path, manifest_path: Path) -> DeliveryPatch:
    try:
        text = Path(patch_path).read_text(encoding="utf-8", errors="surrogateescape")
        value = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception as error:
        raise DeliveryError(f"delivery patch artifacts are unreadable: {error}") from None
    if not isinstance(value, Mapping):
        raise DeliveryError("patch manifest must contain an object")
    return DeliveryPatch.from_dict(value, text=text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--repo", type=Path, required=True)
    create.add_argument("--prompt-id", required=True)
    create.add_argument("--state", type=Path, required=True)
    create.add_argument("--branch")

    verify_ignores = commands.add_parser("verify-ignores")
    verify_ignores.add_argument("--state", type=Path, required=True)

    build = commands.add_parser("build-patch")
    build.add_argument("--state", type=Path, required=True)
    build.add_argument("--out", type=Path, required=True)
    build.add_argument("--out-manifest", type=Path, required=True)
    build.add_argument("--result", choices=("success", "failure"), required=True)
    build.add_argument("--final-commit")

    apply = commands.add_parser("apply-patch")
    apply.add_argument("--state", type=Path, required=True)
    apply.add_argument("--patch", type=Path, required=True)
    apply.add_argument("--patch-manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            try:
                cycle = create_cycle(
                    args.repo,
                    args.prompt_id,
                    branch=args.branch,
                    state_path=args.state,
                )
            except Exception as error:
                retained = getattr(error, "cycle", None)
                payload: dict[str, object] = {
                    "status": "error",
                    "error": str(error),
                }
                if isinstance(retained, WorktreeCycle):
                    payload.update(
                        {
                            "state": str(args.state),
                            "worktree": str(retained.worktree),
                            "branch": retained.branch,
                        }
                    )
                print(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 2
            print(json.dumps(cycle.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "verify-ignores":
            cycle = load_cycle(args.state, require_current=True)
            verified_paths = verify_runtime_ignores(cycle)
            cycle = replace(cycle, runtime_ignores_verified=True)
            save_cycle_atomic(cycle, args.state)
            print(
                json.dumps(
                    {
                        "status": "verified",
                        "state": str(args.state),
                        "paths": list(verified_paths),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        cycle = load_cycle(args.state)
        if args.command == "build-patch":
            patch = build_delivery_patch(
                cycle,
                FAILURE_ALLOWLIST if args.result == "failure" else SUCCESS_ALLOWLIST,
                final_commit=args.final_commit,
                result=args.result,
            )
            save_patch(patch, args.out, args.out_manifest)
            print(json.dumps(patch.to_dict(), ensure_ascii=False, sort_keys=True))
            return 0
        patch = load_patch(args.patch, args.patch_manifest)
        apply_delivery_patch(cycle, patch)
        print(json.dumps({"status": "applied", "paths": list(patch.paths)}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2


__all__ = [
    "AllowlistError",
    "CleanupProgress",
    "DeliveryConflict",
    "DeliveryError",
    "DeliveryPatch",
    "ExcludeResult",
    "FAILURE_ALLOWLIST",
    "FinalizationState",
    "MANAGED_EXCLUDES",
    "Patch",
    "PatchError",
    "SUCCESS_ALLOWLIST",
    "WorktreeCycle",
    "WorktreeDeliveryError",
    "WorktreeError",
    "apply_delivery_patch",
    "build_delivery_patch",
    "create_cycle",
    "initialize_local_excludes",
    "load_cycle",
    "load_patch",
    "main",
    "preflight_patch",
    "save_cycle",
    "save_cycle_atomic",
    "save_patch",
    "verify_runtime_ignores",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
