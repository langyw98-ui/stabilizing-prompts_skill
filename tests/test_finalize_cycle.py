from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts import finalize_cycle as finalize_module
from scripts.evaluation_summary import (
    FORMAL_KINDS,
    FormalResult,
    SummaryEvidence,
    normalize_formal_result,
)
from scripts.manage_worktree import (
    DeliveryError,
    WorktreeError,
    WorktreeCycle,
    create_cycle,
    load_cycle,
    save_cycle_atomic,
)
from scripts.finalize_cycle import (
    FinalizationError,
    compact_history_entry,
    prepare_finalization,
    resolve_delivery_commit,
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
    git(path, "config", "user.email", "finalize-tests@example.invalid")
    git(path, "config", "user.name", "Finalization Tests")
    (path / ".git" / "info" / "exclude").write_text(".worktrees/\n", encoding="utf-8")
    (path / ".gitignore").write_text("", encoding="utf-8")
    prompt = path / "prompts" / "classify.md"
    prompt.parent.mkdir()
    prompt.write_text("original prompt\n", encoding="utf-8")
    prompt_id = "classify--abc123"
    eval_root = path / ".prompt-evals" / prompt_id
    eval_root.mkdir(parents=True)
    (eval_root / "prompt-contract.yaml").write_text(
        "prompt_path: prompts/classify.md\n"
        "schema: target_app.production:Decision\n"
        "renderer: target_app.production:assemble_call\n",
        encoding="utf-8",
    )
    git(path, "add", ".gitignore", "prompts", ".prompt-evals")
    git(path, "commit", "-m", "initial prompt assets")
    return path, prompt_id


@pytest.fixture
def cycle_state(tmp_path: Path) -> Path:
    repo, prompt_id = make_repo(tmp_path / "repo")
    state = tmp_path / "cycle-state.json"
    cycle = create_cycle(repo, prompt_id, state_path=state)
    eval_root = cycle.worktree / ".prompt-evals" / prompt_id
    # These are the canonical evaluation assets that a prepared commit may
    # contain.  Runtime and report artifacts are deliberately not deliverable.
    (eval_root / "eval-config.yaml").write_text("repeats: 5\n", encoding="utf-8")
    (eval_root / "dev-cases.yaml").write_text("cases: []\n", encoding="utf-8")
    (eval_root / "validation-cases.yaml").write_text("cases: []\n", encoding="utf-8")
    (eval_root / "acceptance-cases.yaml").write_text("cases: []\n", encoding="utf-8")
    (eval_root / "coverage-obligations.yaml").write_text(
        "version: 1\ncategories: []\nobligations: []\n", encoding="utf-8"
    )
    (eval_root / "adapter.py").write_text("def prepare_call(prompt, case):\n    return case\n", encoding="utf-8")
    runtime = eval_root / ".runtime"
    runtime.mkdir()
    (runtime / "candidate.md").write_text("candidate prompt\n", encoding="utf-8")
    (runtime / "summary-evidence.json").write_text("{}\n", encoding="utf-8")
    (eval_root / "reports").mkdir()
    (eval_root / "reports" / "raw.json").write_text("raw\n", encoding="utf-8")
    return state


def evidence_for(result: FormalResult, cycle_id: str = "cycle-1") -> SummaryEvidence:
    metric = {
        "pass": 2,
        "parse_error": 0,
        "schema_error": 0,
        "business_error": 0,
        "schema_valid_rate": "1",
        "run_accuracy": "1",
        "stable_case_rate": "1",
    }
    comparison = {
        "fixes": 0,
        "regressions": 0,
        "stability_regressions": 0,
        "unchanged": 2,
    }
    case_counts = {"dev": 2, "validation": 2}
    repeats = {"dev": 1, "validation": 1}
    planned = {"dev": 2, "validation": 2}
    completed = {"dev": 2, "validation": 2}
    metrics: dict[str, object] = {"dev": dict(metric), "validation": dict(metric)}
    comparisons: dict[str, object] = {
        "dev": dict(comparison),
        "validation": dict(comparison),
    }
    if result.acceptance_status == "not_run":
        comparisons["acceptance"] = {"status": "not_run", "reason": "not required"}
    else:
        status = result.acceptance_status
        case_counts["acceptance"] = 2
        repeats["acceptance"] = 1
        planned["acceptance"] = 2
        completed["acceptance"] = 2
        metrics["acceptance"] = dict(metric)
        comparisons["acceptance"] = {**comparison, "status": status}
    return SummaryEvidence(
        cycle_id=cycle_id,
        evidence_identity={"cycle_id": cycle_id, "prompt_id": "classify--abc123"},
        prompt_name="classify.md",
        prompt_path="prompts/classify.md",
        model_name="fixed-model",
        case_counts=case_counts,
        repeats=repeats,
        planned_calls=planned,
        completed_calls=completed,
        metrics=metrics,
        comparisons=comparisons,
        coverage={
            "categories": ["routing"],
            "boundaries": ["missing input"],
            "matrix_complete": True,
            "near_duplicate_review": "none",
            "exclusions": {"none": "all obligations covered"},
            "saturation": "sufficient",
        },
        smoke_passed=True,
        failure_summaries=(),
    )


def candidate_path(state: Path) -> Path:
    cycle = load_cycle(state, require_current=True)
    return cycle.worktree / ".prompt-evals" / cycle.prompt_id / ".runtime" / "candidate.md"


def test_compact_history_entry_is_deterministic() -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    evidence = evidence_for(result)
    assert compact_history_entry(result, evidence) == {
        "finished_at_utc": "2026-09-16T08:09:10Z",
        "result": "acceptance_failed",
        "stop_reason": "acceptance_failed",
        "failure_categories": [],
    }


def test_yaml_timestamp_duplicate_is_rejected_after_safe_load(cycle_state: Path) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    history_path = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "optimization-history.yaml"
    history_path.write_text(
        "cycles:\n"
        "  - finished_at_utc: 2026-09-16T08:09:10Z\n"
        "    result: acceptance_failed\n"
        "    stop_reason: acceptance_failed\n"
        "    failure_categories: []\n",
        encoding="utf-8",
    )
    before_head = git(cycle.worktree, "rev-parse", "HEAD")
    before_history = history_path.read_bytes()
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )

    with pytest.raises(FinalizationError, match="already contains"):
        prepare_finalization(cycle_state, result, evidence_for(result))

    assert git(cycle.worktree, "rev-parse", "HEAD") == before_head
    assert history_path.read_bytes() == before_history
    assert not (
        history_path.parent / "evaluation-summaries" / "2026-09-16-080910-acceptance_failed.md"
    ).exists()


def test_yaml_timestamp_is_normalized_to_utc_string_before_dump(cycle_state: Path) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    history_path = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "optimization-history.yaml"
    history_path.write_text(
        "cycles:\n"
        "  - finished_at_utc: 2026-09-15T08:09:10Z\n"
        "    result: prior_result\n"
        "    stop_reason: prior_result\n"
        "    failure_categories: []\n",
        encoding="utf-8",
    )
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )

    prepare_finalization(cycle_state, result, evidence_for(result))

    history = yaml.safe_load(history_path.read_text(encoding="utf-8"))
    assert isinstance(history["cycles"][0]["finished_at_utc"], str)
    assert history["cycles"][0]["finished_at_utc"] == "2026-09-15T08:09:10Z"


def test_existing_duplicate_history_entries_fail_closed(cycle_state: Path) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    history_path = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "optimization-history.yaml"
    history_path.write_text(
        "cycles:\n"
        "  - finished_at_utc: 2026-09-15T08:09:10Z\n"
        "    result: prior_result\n"
        "    stop_reason: prior_result\n"
        "    failure_categories: []\n"
        "  - finished_at_utc: 2026-09-15T08:09:10Z\n"
        "    result: prior_result\n"
        "    stop_reason: prior_result\n"
        "    failure_categories: []\n",
        encoding="utf-8",
    )
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )

    with pytest.raises(FinalizationError, match="duplicate"):
        prepare_finalization(cycle_state, result, evidence_for(result))


@pytest.mark.parametrize("kind", sorted(FORMAL_KINDS))
def test_every_formal_result_gets_one_summary_and_prepared_commit(
    cycle_state: Path, kind: str
) -> None:
    result = normalize_formal_result(kind, finished_at_utc="2026-09-16T08:09:10Z")
    kwargs: dict[str, object] = {}
    if kind == "acceptance_passed":
        candidate = candidate_path(cycle_state)
        kwargs = {
            "frozen_candidate": candidate,
            "frozen_candidate_hash": hashlib.sha256(candidate.read_bytes()).hexdigest(),
        }
    prepared = prepare_finalization(cycle_state, result, evidence_for(result), **kwargs)

    assert prepared.finalization is not None
    assert prepared.finalization.result_kind == kind
    assert prepared.finalization.delivery_profile == result.delivery_profile
    assert prepared.finalization.prepared_commit == git(prepared.worktree, "rev-parse", "HEAD")
    summary = prepared.worktree / ".prompt-evals" / prepared.prompt_id / "evaluation-summaries"
    files = list(summary.glob(f"2026-09-16-080910-{kind}.md"))
    assert len(files) == 1
    history = yaml.safe_load(
        (prepared.worktree / ".prompt-evals" / prepared.prompt_id / "optimization-history.yaml").read_text(
            encoding="utf-8"
        )
    )
    entries = [entry for entry in history["cycles"] if entry["result"] == kind]
    assert len(entries) == 1


def test_asset_only_prepares_history_summary_and_reuses_commit(
    cycle_state: Path,
) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    before = git(prepared.worktree, "rev-parse", "HEAD")
    final = resolve_delivery_commit(cycle_state, confirmed=True)

    assert prepared.finalization is not None
    assert final.finalization is not None
    assert final.finalization.delivery_commit == prepared.finalization.prepared_commit
    assert git(final.worktree, "rev-parse", "HEAD") == before
    assert final.finalization.delivery_confirmed is True
    assert final.finalization.cleanup.authorized is True


def test_acceptance_pass_creates_one_child_without_duplicate_history(
    cycle_state: Path,
) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    candidate_hash = hashlib.sha256(candidate.read_bytes()).hexdigest()
    prepared = prepare_finalization(
        cycle_state,
        result,
        evidence_for(result),
        frozen_candidate=candidate,
        frozen_candidate_hash=candidate_hash,
    )
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)

    assert delivered.finalization is not None
    assert delivered.finalization.delivery_commit != prepared.finalization.prepared_commit
    assert git(delivered.worktree, "rev-parse", f"{delivered.finalization.delivery_commit}^") == prepared.finalization.prepared_commit
    history_text = git(
        delivered.worktree,
        "show",
        f"{delivered.finalization.delivery_commit}:.prompt-evals/classify--abc123/optimization-history.yaml",
    )
    assert history_text.count("result: acceptance_passed") == 1
    assert (delivered.worktree / "prompts" / "classify.md").read_text(encoding="utf-8") == "candidate prompt\n"


@pytest.mark.parametrize("confirmed", [False])
def test_declined_confirmation_preserves_head_and_state(
    cycle_state: Path, confirmed: bool
) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    before_head = git(prepared.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()

    retained = resolve_delivery_commit(cycle_state, confirmed=confirmed)

    assert git(retained.worktree, "rev-parse", "HEAD") == before_head
    assert cycle_state.read_bytes() == before_state
    assert retained.finalization is not None
    assert retained.finalization.delivery_commit is None


def test_acceptance_candidate_must_be_under_exact_runtime(cycle_state: Path, tmp_path: Path) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    external = tmp_path / "candidate.md"
    external.write_text("candidate prompt\n", encoding="utf-8")
    with pytest.raises(FinalizationError, match="runtime"):
        prepare_finalization(
            cycle_state,
            result,
            evidence_for(result),
            frozen_candidate=external,
            frozen_candidate_hash=hashlib.sha256(external.read_bytes()).hexdigest(),
        )


def test_acceptance_candidate_hash_is_rechecked_on_approval(cycle_state: Path) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    prepared = prepare_finalization(
        cycle_state,
        result,
        evidence_for(result),
        frozen_candidate=candidate,
        frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    candidate.write_text("tampered candidate\n", encoding="utf-8")
    before_head = git(prepared.worktree, "rev-parse", "HEAD")

    with pytest.raises(FinalizationError, match="hash"):
        resolve_delivery_commit(cycle_state, confirmed=True)

    assert git(prepared.worktree, "rev-parse", "HEAD") == before_head


def test_acceptance_approval_uses_the_single_verified_candidate_read(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    verified_bytes = candidate.read_bytes()
    prepare_finalization(
        cycle_state,
        result,
        evidence_for(result),
        frozen_candidate=candidate,
        frozen_candidate_hash=hashlib.sha256(verified_bytes).hexdigest(),
    )

    original_read_bytes = Path.read_bytes
    candidate_reads = 0

    def replace_after_verified_read(path: Path) -> bytes:
        nonlocal candidate_reads
        if path.resolve(strict=False) == candidate.resolve(strict=False):
            candidate_reads += 1
            data = original_read_bytes(path)
            if candidate_reads == 1:
                candidate.write_bytes(b"replacement after verification\n")
                return data
            return data
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", replace_after_verified_read)
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)

    assert candidate_reads == 1
    assert delivered.finalization is not None
    assert (delivered.worktree / "prompts" / "classify.md").read_bytes() == verified_bytes


def test_prepare_state_save_failure_rolls_back_and_can_retry(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    staged_asset = f".prompt-evals/{cycle.prompt_id}/eval-config.yaml"
    git(cycle.worktree, "add", "--", staged_asset)
    before_index = git(cycle.worktree, "diff", "--cached", "--name-only")
    before_head = git(cycle.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()
    history_path = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "optimization-history.yaml"
    summary_path = (
        history_path.parent
        / "evaluation-summaries"
        / "2026-09-16-080910-acceptance_failed.md"
    )
    real_save = finalize_module.save_cycle_atomic

    def fail_state_save(*args: object, **kwargs: object) -> None:
        raise WorktreeError("injected state save failure")

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", fail_state_save)
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    with pytest.raises(FinalizationError, match="injected state save failure"):
        prepare_finalization(cycle_state, result, evidence_for(result))

    assert git(cycle.worktree, "rev-parse", "HEAD") == before_head
    assert git(cycle.worktree, "diff", "--cached", "--name-only") == before_index
    assert cycle_state.read_bytes() == before_state
    assert not history_path.exists()
    assert not summary_path.exists()

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", real_save)
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    assert prepared.finalization is not None


def test_prepare_post_commit_resolution_failure_rolls_back_and_can_retry(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    before_head = git(cycle.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()
    history_path = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "optimization-history.yaml"
    summary_path = (
        history_path.parent
        / "evaluation-summaries"
        / "2026-09-16-080910-acceptance_failed.md"
    )
    original_git_text = finalize_module._git_text

    def fail_head_resolution(repo: Path, *args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            raise FinalizationError("injected post-commit resolution failure")
        return original_git_text(repo, *args)

    monkeypatch.setattr(finalize_module, "_git_text", fail_head_resolution)
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    with pytest.raises(FinalizationError, match="post-commit resolution failure"):
        prepare_finalization(cycle_state, result, evidence_for(result))

    assert git(cycle.worktree, "rev-parse", "HEAD") == before_head
    assert git(cycle.worktree, "diff", "--cached", "--name-only") == ""
    assert cycle_state.read_bytes() == before_state
    assert not history_path.exists()
    assert not summary_path.exists()

    monkeypatch.setattr(finalize_module, "_git_text", original_git_text)
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    assert prepared.finalization is not None


def test_asset_only_state_save_failure_does_not_authorize_delivery(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    assert prepared.finalization is not None
    before_head = git(prepared.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()
    real_save = finalize_module.save_cycle_atomic

    def fail_state_save(*args: object, **kwargs: object) -> None:
        raise WorktreeError("injected delivery state save failure")

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", fail_state_save)
    with pytest.raises(FinalizationError, match="injected delivery state save failure"):
        resolve_delivery_commit(cycle_state, confirmed=True)

    retained = load_cycle(cycle_state, require_current=True)
    assert git(retained.worktree, "rev-parse", "HEAD") == before_head
    assert cycle_state.read_bytes() == before_state
    assert retained.finalization is not None
    assert retained.finalization.delivery_confirmed is False
    assert retained.finalization.cleanup.authorized is False

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", real_save)
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)
    assert delivered.finalization is not None
    assert delivered.finalization.delivery_confirmed is True


def test_acceptance_delivery_state_save_failure_rolls_back_and_can_retry(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    prepared = prepare_finalization(
        cycle_state,
        result,
        evidence_for(result),
        frozen_candidate=candidate,
        frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    assert prepared.finalization is not None
    before_head = git(prepared.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()
    prompt = prepared.worktree / "prompts" / "classify.md"
    contract = prepared.worktree / ".prompt-evals" / prepared.prompt_id / "prompt-contract.yaml"
    before_prompt = prompt.read_bytes()
    before_contract = contract.read_bytes()
    real_save = finalize_module.save_cycle_atomic

    def fail_state_save(*args: object, **kwargs: object) -> None:
        raise WorktreeError("injected delivery state save failure")

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", fail_state_save)
    with pytest.raises(FinalizationError, match="injected delivery state save failure"):
        resolve_delivery_commit(cycle_state, confirmed=True)

    assert git(prepared.worktree, "rev-parse", "HEAD") == before_head
    assert git(prepared.worktree, "diff", "--cached", "--name-only") == ""
    assert cycle_state.read_bytes() == before_state
    assert prompt.read_bytes() == before_prompt
    assert contract.read_bytes() == before_contract

    monkeypatch.setattr(finalize_module, "save_cycle_atomic", real_save)
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)
    assert delivered.finalization is not None
    assert delivered.finalization.delivery_commit != before_head


def test_acceptance_post_commit_resolution_failure_rolls_back_and_can_retry(
    cycle_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    prepared = prepare_finalization(
        cycle_state,
        result,
        evidence_for(result),
        frozen_candidate=candidate,
        frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
    )
    assert prepared.finalization is not None
    before_head = git(prepared.worktree, "rev-parse", "HEAD")
    before_state = cycle_state.read_bytes()
    prompt = prepared.worktree / "prompts" / "classify.md"
    contract = prepared.worktree / ".prompt-evals" / prepared.prompt_id / "prompt-contract.yaml"
    before_prompt = prompt.read_bytes()
    before_contract = contract.read_bytes()
    original_git_text = finalize_module._git_text
    head_calls = 0

    def fail_second_head_resolution(repo: Path, *args: str) -> str:
        nonlocal head_calls
        if args == ("rev-parse", "HEAD"):
            head_calls += 1
            if head_calls == 2:
                raise FinalizationError("injected delivery post-commit resolution failure")
        return original_git_text(repo, *args)

    monkeypatch.setattr(finalize_module, "_git_text", fail_second_head_resolution)
    with pytest.raises(FinalizationError, match="delivery post-commit resolution failure"):
        resolve_delivery_commit(cycle_state, confirmed=True)

    assert head_calls == 2
    assert git(prepared.worktree, "rev-parse", "HEAD") == before_head
    assert git(prepared.worktree, "diff", "--cached", "--name-only") == ""
    assert cycle_state.read_bytes() == before_state
    assert prompt.read_bytes() == before_prompt
    assert contract.read_bytes() == before_contract

    monkeypatch.setattr(finalize_module, "_git_text", original_git_text)
    delivered = resolve_delivery_commit(cycle_state, confirmed=True)
    assert delivered.finalization is not None
    assert delivered.finalization.delivery_commit != before_head


def test_asset_only_result_rejects_candidate_input(cycle_state: Path) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    with pytest.raises(FinalizationError, match="candidate"):
        prepare_finalization(
            cycle_state,
            result,
            evidence_for(result),
            frozen_candidate=candidate,
            frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )


def test_prompt_contract_path_redirect_is_rejected(cycle_state: Path) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    contract = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "prompt-contract.yaml"
    contract.write_text("prompt_path: prompts/other.md\n", encoding="utf-8")
    result = normalize_formal_result(
        "acceptance_passed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    candidate = candidate_path(cycle_state)
    with pytest.raises((FinalizationError, DeliveryError), match="Prompt path|contract"):
        prepare_finalization(
            cycle_state,
            result,
            evidence_for(result),
            frozen_candidate=candidate,
            frozen_candidate_hash=hashlib.sha256(candidate.read_bytes()).hexdigest(),
        )


def test_existing_summary_collision_stops_without_commit(cycle_state: Path) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    summary = cycle.worktree / ".prompt-evals" / cycle.prompt_id / "evaluation-summaries"
    summary.mkdir()
    destination = summary / "2026-09-16-080910-acceptance_failed.md"
    destination.write_text("existing\n", encoding="utf-8")
    before_head = git(cycle.worktree, "rev-parse", "HEAD")
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )

    with pytest.raises(FinalizationError, match="collision|exists"):
        prepare_finalization(cycle_state, result, evidence_for(result))

    assert git(cycle.worktree, "rev-parse", "HEAD") == before_head
    assert destination.read_text(encoding="utf-8") == "existing\n"


def test_summary_parent_symlink_is_rejected_before_external_write(
    cycle_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cycle = load_cycle(cycle_state, require_current=True)
    eval_root = cycle.worktree / ".prompt-evals" / cycle.prompt_id
    summary_parent = eval_root / "evaluation-summaries"
    outside = tmp_path / "outside-summary-root"
    outside.mkdir()
    try:
        summary_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this platform")

    outside_opens: list[Path] = []
    outside_resolved = outside.resolve()
    original_open = Path.open

    def record_external_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if path.resolve(strict=False).is_relative_to(outside_resolved):
            outside_opens.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", record_external_open)
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    before_head = git(cycle.worktree, "rev-parse", "HEAD")
    with pytest.raises(FinalizationError, match="symlink|link|summary"):
        prepare_finalization(cycle_state, result, evidence_for(result))

    assert outside_opens == []
    assert not list(outside.iterdir())
    assert git(cycle.worktree, "rev-parse", "HEAD") == before_head


def test_second_finalization_attempt_is_rejected_without_duplicate_history(
    cycle_state: Path,
) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    before_head = git(prepared.worktree, "rev-parse", "HEAD")
    with pytest.raises(FinalizationError, match="already finalized|finalization"):
        prepare_finalization(cycle_state, result, evidence_for(result))
    assert git(prepared.worktree, "rev-parse", "HEAD") == before_head
    history = yaml.safe_load(
        (prepared.worktree / ".prompt-evals" / prepared.prompt_id / "optimization-history.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert [entry["result"] for entry in history["cycles"]].count("acceptance_failed") == 1


def test_prepare_cli_requires_evidence_file_in_exact_runtime(
    cycle_state: Path, tmp_path: Path
) -> None:
    from scripts.finalize_cycle import main

    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    external = tmp_path / "evidence.json"
    external.write_text("{}\n", encoding="utf-8")
    assert (
        main(
            [
                "prepare",
                "--state",
                str(cycle_state),
                "--result",
                result.kind,
                "--finished-at",
                result.finished_at_utc,
                "--evidence",
                str(external),
            ]
        )
        == 2
    )


def test_prepared_commit_stages_no_runtime_or_reports(cycle_state: Path) -> None:
    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepared = prepare_finalization(cycle_state, result, evidence_for(result))
    changed = git(
        prepared.worktree,
        "diff",
        "--name-only",
        f"{prepared.cycle_base_commit}",
        prepared.finalization.prepared_commit,
    ).splitlines()
    assert changed
    assert all(".runtime/" not in path and "/reports/" not in path for path in changed)
    assert all("raw" not in path for path in changed)


def test_approve_cli_sets_combined_confirmation(cycle_state: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from scripts.finalize_cycle import main

    result = normalize_formal_result(
        "acceptance_failed", finished_at_utc="2026-09-16T08:09:10Z"
    )
    prepare_finalization(cycle_state, result, evidence_for(result))
    assert main(["approve", "--state", str(cycle_state)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "delivered"
    cycle = load_cycle(cycle_state, require_current=True)
    assert cycle.finalization is not None
    assert cycle.finalization.delivery_confirmed is True
    assert cycle.finalization.cleanup.authorized is True
