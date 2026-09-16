from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml

from scripts.evaluation_summary import (
    FORMAL_KINDS,
    FormalResult,
    SummaryEvidence,
    normalize_formal_result,
)
from scripts.manage_worktree import (
    DeliveryError,
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
