"""Prepare scored prompt-tuning results and resolve delivery confirmation.

Finalization deliberately owns only the small boundary between deterministic
evaluation evidence and the committed cycle result.  Git identity, cycle
state validation, and atomic state persistence remain in ``manage_worktree``;
this module asks that module for those operations rather than trusting paths
or commit identifiers supplied by callers.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import subprocess

try:
    import yaml
except ImportError:  # pragma: no cover - kds includes PyYAML
    yaml = None  # type: ignore[assignment]

from scripts import manage_worktree
from scripts.evaluation_summary import (
    FORMAL_KINDS,
    FormalResult,
    SummaryEvidence,
    SummaryError,
    normalize_formal_result,
    render_summary,
)
from scripts.manage_worktree import (
    DeliveryError,
    FinalizationState,
    WorktreeCycle,
    WorktreeError,
    load_cycle,
    save_cycle_atomic,
)


class FinalizationError(DeliveryError):
    """Raised when a scored result cannot be safely finalized or delivered."""


_EVAL_ASSET_NAMES = frozenset(
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
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CURRENT_HASH_RE = re.compile(r"^([ \t]*)current_prompt_hash[ \t]*:")


def _git(repo: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    """Use the cycle's Git helper while keeping finalization shell-free."""

    try:
        return manage_worktree._git(repo, *arguments, check=check)
    except WorktreeError as error:
        raise FinalizationError(str(error)) from error


def _git_text(repo: Path, *arguments: str) -> str:
    try:
        return manage_worktree.git_text(repo, *arguments)
    except WorktreeError as error:
        raise FinalizationError(str(error)) from error


def _load_current_cycle(state_path: Path) -> WorktreeCycle:
    try:
        cycle = load_cycle(Path(state_path), require_current=True)
        manage_worktree.validate_managed_cycle(cycle)
        _require_cycle_branch(cycle)
    except (WorktreeError, DeliveryError) as error:
        raise FinalizationError(str(error)) from error
    return cycle


def _require_cycle_branch(cycle: WorktreeCycle) -> None:
    actual = _git_text(cycle.worktree, "branch", "--show-current")
    if actual != cycle.branch:
        raise FinalizationError(
            f"cycle worktree branch changed: expected {cycle.branch}, got {actual}"
        )


def _evaluation_root(cycle: WorktreeCycle) -> Path:
    if not isinstance(cycle.prompt_id, str) or not cycle.prompt_id:
        raise FinalizationError("cycle is missing a prompt_id")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", cycle.prompt_id):
        raise FinalizationError("prompt_id is not a safe evaluation directory name")
    root = cycle.worktree / ".prompt-evals" / cycle.prompt_id
    _reject_links(root, cycle.worktree)
    if root.exists() and not root.is_dir():
        raise FinalizationError("cycle evaluation root is not a directory")
    return root


def _runtime_root(cycle: WorktreeCycle) -> Path:
    return _evaluation_root(cycle) / ".runtime"


def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        return path.resolve(strict=False).is_relative_to(parent.resolve(strict=False))
    except (OSError, ValueError):
        return False


def _reject_links(path: Path, root: Path) -> None:
    """Reject links in a caller-provided path before resolving it."""

    current = path
    while True:
        try:
            if current.is_symlink():
                raise FinalizationError(f"path contains a symlink: {current}")
            stat = current.lstat()
            if getattr(stat, "st_file_attributes", 0) & 0x400:
                raise FinalizationError(f"path contains a junction: {current}")
        except FileNotFoundError:
            pass
        except OSError as error:
            raise FinalizationError(f"unable to inspect path: {current}: {error}") from error
        if current == root:
            break
        if not _path_is_within(current, root):
            raise FinalizationError("path escapes cycle worktree")
        parent = current.parent
        if parent == current:
            raise FinalizationError("path escapes cycle worktree")
        current = parent


def _runtime_file(cycle: WorktreeCycle, path: Path, *, label: str) -> tuple[Path, str]:
    """Validate one regular file below this cycle's exact ``.runtime``."""

    runtime = _runtime_root(cycle)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = candidate.absolute()
    candidate = Path(candidate)
    if not _path_is_within(candidate, runtime):
        raise FinalizationError(f"{label} must be under the exact cycle .runtime")
    _reject_links(candidate, cycle.worktree)
    resolved_runtime = runtime.resolve(strict=False)
    resolved_candidate = candidate.resolve(strict=False)
    if not _path_is_within(resolved_candidate, resolved_runtime):
        raise FinalizationError(f"{label} must be under the exact cycle .runtime")
    if not candidate.is_file() or candidate.is_symlink():
        raise FinalizationError(f"{label} must be a regular file")
    try:
        relative = candidate.relative_to(cycle.worktree).as_posix()
    except ValueError as error:
        raise FinalizationError(f"{label} is outside the cycle worktree") from error
    relative = manage_worktree.normalize_relative_path(relative, label=label)
    if ".runtime" not in relative.split("/"):
        raise FinalizationError(f"{label} must be under the exact cycle .runtime")
    return candidate, relative


def _validate_hash(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FinalizationError(f"{label} must be a SHA-256 hex digest")
    return value.lower()


def _candidate_identity(
    cycle: WorktreeCycle,
    candidate: Path | None,
    candidate_hash: str | None,
) -> tuple[str | None, str | None]:
    if cycle.finalization is not None:
        raise FinalizationError("cycle is already finalized")
    if candidate is None and candidate_hash is None:
        return None, None
    if candidate is None or candidate_hash is None:
        raise FinalizationError("candidate and candidate-hash must be supplied together")
    path, relative = _runtime_file(cycle, Path(candidate), label="frozen candidate")
    if not relative.casefold().endswith(".md"):
        raise FinalizationError("frozen candidate must be a Markdown file")
    expected = _validate_hash(candidate_hash, label="candidate hash")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise FinalizationError(
            f"frozen candidate hash mismatch: expected {expected}, got {actual}"
        )
    return relative, expected


def _canonical_prompt_path(cycle: WorktreeCycle) -> str:
    try:
        path = manage_worktree.canonical_prompt_path(cycle)
    except DeliveryError as error:
        raise FinalizationError(str(error)) from error
    if path is None:
        raise FinalizationError("canonical Prompt path cannot be resolved")
    return path


def _validate_current_contract(cycle: WorktreeCycle, prompt_path: str) -> Path:
    eval_root = _evaluation_root(cycle)
    contract = eval_root / "prompt-contract.yaml"
    if not contract.is_file() or contract.is_symlink():
        raise FinalizationError("canonical prompt-contract.yaml is missing")
    try:
        current = manage_worktree.parse_prompt_contract_path(cycle.original_repo, contract)
    except DeliveryError as error:
        raise FinalizationError(str(error)) from error
    if current != prompt_path:
        raise FinalizationError("prompt-contract.yaml redirected the canonical Prompt path")
    return contract


def _history_path(cycle: WorktreeCycle) -> Path:
    return _evaluation_root(cycle) / "optimization-history.yaml"


def _load_history(path: Path) -> dict[str, object]:
    if yaml is None:
        raise FinalizationError("PyYAML is required for optimization history")
    if not path.exists():
        return {"cycles": []}
    if path.is_symlink() or not path.is_file():
        raise FinalizationError("optimization-history.yaml must be a regular file")
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise FinalizationError(f"optimization-history.yaml is invalid: {error}") from error
    if not isinstance(value, Mapping):
        raise FinalizationError("optimization-history.yaml must contain a mapping")
    cycles = value.get("cycles", [])
    if not isinstance(cycles, list):
        raise FinalizationError("optimization-history.yaml cycles must be a list")
    if any(not isinstance(entry, Mapping) for entry in cycles):
        raise FinalizationError("optimization-history.yaml cycles must contain mappings")
    # Copy into a plain mapping so the dump is deterministic and does not
    # retain PyYAML's mutable loader objects.
    result: dict[str, object] = dict(value)
    result["cycles"] = [dict(entry) for entry in cycles]
    return result


def compact_history_entry(result: FormalResult, evidence: SummaryEvidence) -> dict[str, object]:
    """Return the one compact, deterministic history entry for a result."""

    if not isinstance(result, FormalResult):
        raise FinalizationError("result must be a FormalResult")
    if not isinstance(evidence, SummaryEvidence):
        raise FinalizationError("evidence must be SummaryEvidence")
    categories: set[str] = set()
    for index, item in enumerate(evidence.failure_summaries):
        if not isinstance(item, Mapping):
            raise FinalizationError(f"failure summary {index} must be a mapping")
        category = item.get("category")
        if category is not None:
            categories.add(str(category))
    return {
        "finished_at_utc": result.finished_at_utc,
        "result": result.kind,
        "stop_reason": result.stop_reason,
        "failure_categories": sorted(categories),
    }


def _append_history(path: Path, result: FormalResult, evidence: SummaryEvidence) -> None:
    history = _load_history(path)
    cycles = history["cycles"]
    assert isinstance(cycles, list)
    if any(
        isinstance(entry, Mapping)
        and entry.get("finished_at_utc") == result.finished_at_utc
        and (entry.get("result") or entry.get("stop_reason")) == result.kind
        for entry in cycles
    ):
        raise FinalizationError(
            "optimization history already contains this finished time/result"
        )
    cycles.append(compact_history_entry(result, evidence))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(  # type: ignore[union-attr]
                history,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
            newline="\n",
        )
    except OSError as error:
        raise FinalizationError(f"unable to write optimization history: {error}") from error


def _summary_destination(cycle: WorktreeCycle, result: FormalResult) -> Path:
    timestamp = result.finished_at_utc[:10] + "-" + result.finished_at_utc[11:19].replace(":", "")
    return _evaluation_root(cycle) / "evaluation-summaries" / f"{timestamp}-{result.kind}.md"


def _staged_paths(cycle: WorktreeCycle) -> tuple[str, ...]:
    result = _git(cycle.worktree, "diff", "--cached", "--name-only", "-z")
    fields = result.stdout.split(b"\x00")
    return tuple(
        manage_worktree.normalize_relative_path(field.decode("utf-8", "surrogateescape"))
        for field in fields
        if field
    )


def _assert_allowed_staged(cycle: WorktreeCycle, allowed: set[str]) -> None:
    staged = set(_staged_paths(cycle))
    if not staged <= allowed:
        unexpected = ", ".join(sorted(staged - allowed))
        raise FinalizationError(f"staged path is not a canonical deliverable asset: {unexpected}")


def _stage_prepared_assets(cycle: WorktreeCycle, summary: Path) -> set[str]:
    eval_root = _evaluation_root(cycle)
    prefix = f".prompt-evals/{cycle.prompt_id}/"
    allowed = {prefix + name for name in _EVAL_ASSET_NAMES}
    summary_relative = summary.relative_to(cycle.worktree).as_posix()
    allowed.add(summary_relative)
    _assert_allowed_staged(cycle, allowed)

    existing: list[str] = []
    for name in sorted(_EVAL_ASSET_NAMES | {summary.name}):
        path = eval_root / name if name != summary.name else summary
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise FinalizationError(f"deliverable asset is not a regular file: {path}")
            existing.append(path.relative_to(cycle.worktree).as_posix())
    if summary_relative not in existing:
        raise FinalizationError("evaluation summary was not written in the cycle")
    if existing:
        _git(cycle.worktree, "add", "--", *existing)
    _assert_allowed_staged(cycle, allowed)
    return allowed


def _require_cycle_head(cycle: WorktreeCycle, expected: str) -> None:
    actual = _git_text(cycle.worktree, "rev-parse", "HEAD")
    if actual != expected:
        raise FinalizationError(
            f"cycle worktree HEAD changed: expected {expected}, got {actual}"
        )


def _worktree_clean(cycle: WorktreeCycle) -> None:
    status = _git(
        cycle.worktree,
        "-c",
        "status.showUntrackedFiles=all",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    if status.stdout.strip():
        raise FinalizationError("cycle worktree contains uncommitted changes")


def _prepared_state(
    state_path: Path,
    cycle: WorktreeCycle,
    result: FormalResult,
    summary: Path,
    prepared_commit: str,
    candidate_path: str | None,
    candidate_hash: str | None,
) -> WorktreeCycle:
    finalization = FinalizationState(
        result_kind=result.kind,
        stop_reason=result.stop_reason,
        finished_at_utc=result.finished_at_utc,
        summary_path=summary.relative_to(cycle.worktree).as_posix(),
        delivery_profile=result.delivery_profile,
        prepared_commit=prepared_commit,
        frozen_candidate_path=candidate_path,
        frozen_candidate_hash=candidate_hash,
    )
    updated = replace(
        cycle,
        final_worktree_commit=prepared_commit,
        finalization=finalization,
    )
    try:
        save_cycle_atomic(updated, Path(state_path))
    except (WorktreeError, OSError) as error:
        raise FinalizationError(f"unable to persist prepared finalization: {error}") from error
    return updated


def _validate_finalization_matrix(finalization: FinalizationState) -> None:
    """Validate persisted result/profile fields before any approval mutation."""

    if finalization.result_kind not in FORMAL_KINDS:
        raise FinalizationError("finalization contains an unknown formal result")
    try:
        normalized = normalize_formal_result(
            finalization.result_kind,
            finished_at_utc=finalization.finished_at_utc,
            stop_reason=finalization.stop_reason,
        )
    except SummaryError as error:
        raise FinalizationError(str(error)) from error
    if finalization.delivery_profile != normalized.delivery_profile:
        raise FinalizationError("finalization delivery profile does not match result")
    if normalized.acceptance_status == "passed":
        if not finalization.frozen_candidate_path or not finalization.frozen_candidate_hash:
            raise FinalizationError("acceptance-pass finalization is missing frozen candidate identity")
    elif finalization.frozen_candidate_path is not None or finalization.frozen_candidate_hash is not None:
        raise FinalizationError("asset-only finalization contains a frozen candidate")


def prepare_finalization(
    state_path: Path,
    result: FormalResult,
    evidence: SummaryEvidence,
    *,
    frozen_candidate: Path | None = None,
    frozen_candidate_hash: str | None = None,
) -> WorktreeCycle:
    """Render, history-append, and commit one scored result exactly once."""

    if not isinstance(result, FormalResult):
        raise FinalizationError("result must be a FormalResult")
    if not isinstance(evidence, SummaryEvidence):
        raise FinalizationError("evidence must be SummaryEvidence")
    cycle = _load_current_cycle(Path(state_path))
    if cycle.finalization is not None:
        raise FinalizationError("cycle is already finalized")
    candidate_path, candidate_hash = _candidate_identity(
        cycle,
        frozen_candidate if result.acceptance_status == "passed" else None,
        frozen_candidate_hash if result.acceptance_status == "passed" else None,
    )
    if result.acceptance_status != "passed" and (
        frozen_candidate is not None or frozen_candidate_hash is not None
    ):
        raise FinalizationError("asset-only result cannot receive a frozen candidate")
    if result.acceptance_status == "passed" and (
        candidate_path is None or candidate_hash is None
    ):
        raise FinalizationError("acceptance-pass result requires a frozen candidate and hash")

    prompt_path = _canonical_prompt_path(cycle)
    if evidence.prompt_path != prompt_path:
        raise FinalizationError("summary evidence Prompt path does not match the cycle contract")
    identity_prompt_id = evidence.evidence_identity.get("prompt_id")
    if identity_prompt_id is not None and identity_prompt_id != cycle.prompt_id:
        raise FinalizationError("summary evidence prompt identity does not match the cycle")
    _validate_current_contract(cycle, prompt_path)
    try:
        summary_text = render_summary(result, evidence)
    except SummaryError as error:
        raise FinalizationError(str(error)) from error
    summary = _summary_destination(cycle, result)
    if summary.exists():
        raise FinalizationError(f"summary destination already exists: {summary}")

    history = _history_path(cycle)
    # Validate duplicate/history shape before touching either output file.
    _load_history(history)
    # Create the summary exclusively; the preflight above gives callers a
    # deterministic collision error before history is changed.
    try:
        summary.parent.mkdir(parents=True, exist_ok=True)
        with summary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(summary_text)
    except FileExistsError:
        raise FinalizationError(f"summary destination already exists: {summary}") from None
    except OSError as error:
        raise FinalizationError(f"unable to write evaluation summary: {error}") from error

    history_before = history.read_bytes() if history.exists() else None
    try:
        _append_history(history, result, evidence)
        _stage_prepared_assets(cycle, summary)
        _assert_allowed_staged(
            cycle,
            {
                f".prompt-evals/{cycle.prompt_id}/{name}" for name in _EVAL_ASSET_NAMES
            }
            | {summary.relative_to(cycle.worktree).as_posix()},
        )
        commit_result = _git(
            cycle.worktree,
            "commit",
            "-m",
            f"tune: prepare {result.kind} finalization",
            check=False,
        )
        if commit_result.returncode != 0:
            detail = (commit_result.stderr or commit_result.stdout).decode("utf-8", "replace").strip()
            raise FinalizationError(f"unable to create prepared commit: {detail}")
        prepared_commit = _git_text(cycle.worktree, "rev-parse", "HEAD")
    except BaseException:
        # The ignored runtime remains untouched.  Restore only the files this
        # operation owns if no commit was created; this keeps a failed prepare
        # inspectable without leaving a half-written summary/history pair.
        if history_before is None:
            try:
                history.unlink(missing_ok=True)
            except OSError:
                pass
        else:
            try:
                history.write_bytes(history_before)
            except OSError:
                pass
        try:
            summary.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return _prepared_state(
        Path(state_path),
        cycle,
        result,
        summary,
        prepared_commit,
        candidate_path,
        candidate_hash,
    )


def _validate_recorded_candidate(cycle: WorktreeCycle, finalization: FinalizationState) -> Path:
    if not finalization.frozen_candidate_path or not finalization.frozen_candidate_hash:
        raise FinalizationError("acceptance-pass finalization is missing frozen candidate identity")
    candidate, relative = _runtime_file(
        cycle,
        cycle.worktree / Path(finalization.frozen_candidate_path),
        label="frozen candidate",
    )
    if relative != finalization.frozen_candidate_path:
        raise FinalizationError("frozen candidate identity changed")
    expected = _validate_hash(finalization.frozen_candidate_hash, label="candidate hash")
    actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
    if actual != expected:
        raise FinalizationError(
            f"frozen candidate hash mismatch: expected {expected}, got {actual}"
        )
    return candidate


def _safe_worktree_file(cycle: WorktreeCycle, relative: str, *, label: str) -> Path:
    normalized = manage_worktree.normalize_relative_path(relative, label=label)
    path = cycle.worktree / Path(normalized)
    if not _path_is_within(path, cycle.worktree):
        raise FinalizationError(f"{label} escapes cycle worktree")
    _reject_links(path, cycle.worktree)
    if path.exists() and path.is_dir():
        raise FinalizationError(f"{label} must be a regular file")
    return path


def _update_contract_hash(contract: Path, candidate_hash: str) -> bytes:
    try:
        original = contract.read_bytes()
        text = original.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise FinalizationError(f"unable to read prompt contract: {error}") from error
    lines = text.splitlines(keepends=True)
    replaced = False
    for index, line in enumerate(lines):
        match = _CURRENT_HASH_RE.match(line)
        if match:
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = f"{match.group(1)}current_prompt_hash: {candidate_hash}{newline}"
            replaced = True
            break
    if not replaced:
        separator = "" if not text or text.endswith(("\n", "\r")) else "\n"
        lines.append(f"{separator}current_prompt_hash: {candidate_hash}\n")
    return "".join(lines).encode("utf-8")


def _acceptance_delivery(cycle: WorktreeCycle, finalization: FinalizationState) -> str:
    _require_cycle_head(cycle, finalization.prepared_commit)
    _worktree_clean(cycle)
    candidate = _validate_recorded_candidate(cycle, finalization)
    prompt_path = _canonical_prompt_path(cycle)
    try:
        manage_worktree.assert_prompt_contract_identity(cycle, finalization.prepared_commit)
    except DeliveryError as error:
        raise FinalizationError(str(error)) from error
    contract = _validate_current_contract(cycle, prompt_path)
    prompt = _safe_worktree_file(cycle, prompt_path, label="canonical Prompt path")
    candidate_bytes = candidate.read_bytes()
    contract_before = contract.read_bytes()
    prompt_before = prompt.read_bytes() if prompt.exists() else None
    try:
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_bytes(candidate_bytes)
        contract.write_bytes(_update_contract_hash(contract, finalization.frozen_candidate_hash or ""))
        _assert_allowed_staged(
            cycle,
            {
                f".prompt-evals/{cycle.prompt_id}/{name}" for name in _EVAL_ASSET_NAMES
            }
            | {prompt_path},
        )
        _git(cycle.worktree, "add", "--", prompt_path, contract.relative_to(cycle.worktree).as_posix())
        _assert_allowed_staged(
            cycle,
            {
                f".prompt-evals/{cycle.prompt_id}/{name}" for name in _EVAL_ASSET_NAMES
            }
            | {prompt_path},
        )
        commit_result = _git(
            cycle.worktree,
            "commit",
            "--allow-empty",
            "-m",
            "tune: deliver accepted prompt",
            check=False,
        )
        if commit_result.returncode != 0:
            detail = (commit_result.stderr or commit_result.stdout).decode("utf-8", "replace").strip()
            raise FinalizationError(f"unable to create delivery commit: {detail}")
    except BaseException:
        try:
            if prompt_before is None:
                prompt.unlink(missing_ok=True)
            else:
                prompt.write_bytes(prompt_before)
            contract.write_bytes(contract_before)
        except OSError:
            pass
        raise
    delivery_commit = _git_text(cycle.worktree, "rev-parse", "HEAD")
    if _git_text(cycle.worktree, "rev-parse", f"{delivery_commit}^") != finalization.prepared_commit:
        raise FinalizationError("delivery commit is not a child of prepared commit")
    return delivery_commit


def resolve_delivery_commit(state_path: Path, *, confirmed: bool) -> WorktreeCycle:
    """Resolve one explicit affirmative delivery confirmation.

    ``False`` (and any non-boolean answer passed defensively by a caller) is a
    no-op: it does not write state, create a commit, or mutate the worktree.
    """

    cycle = _load_current_cycle(Path(state_path))
    finalization = cycle.finalization
    if finalization is None:
        raise FinalizationError("cycle has no prepared finalization")
    _validate_finalization_matrix(finalization)
    if confirmed is not True:
        return cycle
    if finalization.delivery_confirmed and finalization.delivery_commit:
        return cycle
    if finalization.delivery_confirmed or finalization.cleanup.authorized:
        raise FinalizationError("cycle confirmation state is inconsistent")
    if finalization.delivery_commit is not None:
        raise FinalizationError("cycle delivery state contains an unconfirmed commit")
    if finalization.delivery_profile == "assets":
        _require_cycle_head(cycle, finalization.prepared_commit)
        _worktree_clean(cycle)
        delivery_commit = finalization.prepared_commit
    elif finalization.delivery_profile == "success":
        delivery_commit = _acceptance_delivery(cycle, finalization)
    else:
        raise FinalizationError("unknown delivery profile in prepared finalization")
    updated_finalization = replace(
        finalization,
        delivery_commit=delivery_commit,
        delivery_confirmed=True,
        cleanup=replace(finalization.cleanup, authorized=True),
    )
    updated = replace(
        cycle,
        final_worktree_commit=delivery_commit,
        finalization=updated_finalization,
    )
    try:
        save_cycle_atomic(updated, Path(state_path))
    except (WorktreeError, OSError) as error:
        raise FinalizationError(f"unable to persist delivery confirmation: {error}") from error
    return updated


def _runtime_path_for_cli(cycle: WorktreeCycle, path: Path, *, label: str) -> Path:
    candidate, _ = _runtime_file(cycle, path, label=label)
    return candidate


def _read_evidence(path: Path, cycle: WorktreeCycle) -> SummaryEvidence:
    evidence_path = _runtime_path_for_cli(cycle, path, label="summary evidence")
    if evidence_path.suffix.casefold() != ".json":
        raise FinalizationError("summary evidence must be a JSON file")
    try:
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FinalizationError(f"summary evidence is unreadable: {error}") from error
    try:
        return SummaryEvidence.from_mapping(value)
    except SummaryError as error:
        raise FinalizationError(str(error)) from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--state", type=Path, required=True)
    prepare.add_argument("--result", required=True)
    prepare.add_argument("--finished-at", required=True)
    prepare.add_argument("--evidence", type=Path, required=True)
    prepare.add_argument("--candidate", type=Path)
    prepare.add_argument("--candidate-hash")
    approve = commands.add_parser("approve")
    approve.add_argument("--state", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            cycle = _load_current_cycle(args.state)
            evidence = _read_evidence(args.evidence, cycle)
            result = normalize_formal_result(args.result, finished_at_utc=args.finished_at)
            prepared = prepare_finalization(
                args.state,
                result,
                evidence,
                frozen_candidate=args.candidate,
                frozen_candidate_hash=args.candidate_hash,
            )
            assert prepared.finalization is not None
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "state": str(args.state),
                        "summary_path": prepared.finalization.summary_path,
                        "prepared_commit": prepared.finalization.prepared_commit,
                        "delivery_profile": prepared.finalization.delivery_profile,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        delivered = resolve_delivery_commit(args.state, confirmed=True)
        assert delivered.finalization is not None
        print(
            json.dumps(
                {
                    "status": "delivered",
                    "state": str(args.state),
                    "delivery_commit": delivered.finalization.delivery_commit,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2


__all__ = [
    "FinalizationError",
    "compact_history_entry",
    "main",
    "prepare_finalization",
    "resolve_delivery_commit",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
