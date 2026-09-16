from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import manage_worktree
from scripts.local_model_client import safe_client_config
from scripts.manage_worktree import (
    DeliveryError,
    WorktreeError,
    create_cycle,
    initialize_local_excludes,
    load_cycle,
)
from scripts.validate_workspace import prompt_id_for_path
from scripts.run_prompt_eval import _redacted, _safe_serialize
from tests import integration_support as support_module
from tests.integration_support import (
    CountingTransport,
    build_target_repo,
    run_cli_chain,
    run_tune_with_fake_transport,
    workspace_snapshot,
)


def test_cli_chain_uses_production_renderer_and_schema_without_duplicate_contract(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    initialize_local_excludes(
        target_repo,
        prompt_id_for_path("prompts/classify.md"),
        target_repo / ".worktrees" / "stabilizing-prompts" / "cli-probe",
    )

    evidence = run_cli_chain(target_repo)

    assert evidence.workspace_status == "valid"
    assert evidence.case_suite_status == "valid"
    assert evidence.run_status == "complete"
    assert evidence.score_status == "complete"
    assert evidence.comparison_status == "passed"
    assert evidence.schema_name == "Decision"
    assert evidence.renderer_marker == "production-renderer"


def test_delivery_artifacts_require_the_exact_cycle_runtime(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    prompt_id = prompt_id_for_path("prompts/classify.md")
    cycle = create_cycle(target_repo, prompt_id)
    runtime = cycle.worktree / ".prompt-evals" / prompt_id / ".runtime"
    runtime.mkdir(parents=True)

    with pytest.raises(DeliveryError, match=r"exact cycle \.runtime"):
        manage_worktree._require_runtime_output(
            cycle,
            tmp_path / "outside.patch",
            "patch output",
        )


def test_delivery_rollback_restores_exact_targets_after_post_apply_failure(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    result = run_tune_with_fake_transport(target_repo, scenario="rollback")

    assert result.stop_reason == "delivery_rollback"
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt
    assert not result.partially_delivered_paths


def test_post_confirmation_ignore_drift_invalidates_confirmation_without_finalization(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    transport = CountingTransport(scenario="ignore-drift")
    before = workspace_snapshot(target_repo)

    result = run_tune_with_fake_transport(
        target_repo,
        scenario="ignore-drift",
        transport=transport,
        confirm_delivery=True,
    )

    assert result.stop_reason == "setup_error"
    assert result.formal_result is None
    assert result.summary_path is None
    assert transport.call_count == 0
    assert result.raw_evidence == []
    assert "confirmation-invalidated" in result.lifecycle_events
    assert workspace_snapshot(target_repo) == before
    worktree_root = target_repo / ".worktrees" / "stabilizing-prompts"
    assert worktree_root.is_dir()
    worktree = next(worktree_root.iterdir())
    assert not tuple((worktree / ".prompt-evals").glob("*/evaluation-summaries/*.md"))


def test_delivery_state_failure_rolls_back_workspace_and_retains_cycle(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    before = workspace_snapshot(target_repo)

    result = run_tune_with_fake_transport(
        target_repo,
        scenario="delivery-state-failure",
        confirm_delivery=True,
    )

    assert result.stop_reason == "delivery_state_failed"
    assert result.formal_result is not None
    assert result.formal_result.kind == "acceptance_passed"
    assert result.prepared_commit is not None
    assert result.delivery_commit is not None
    assert result.cleanup_status == "retained"
    assert workspace_snapshot(target_repo) == before
    prompt_id = prompt_id_for_path("prompts/classify.md")
    state_path = target_repo.parent / f".{target_repo.name}-{prompt_id}.cycle.json"
    cycle = load_cycle(state_path, require_current=True)
    assert cycle.finalization is not None
    assert cycle.finalization.delivery_confirmed is True
    assert cycle.finalization.delivery_applied is False
    assert cycle.finalization.delivery_verified is False
    assert cycle.finalization.cleanup.authorized is True
    assert (target_repo / ".worktrees" / "stabilizing-prompts").is_dir()


def test_tune_initializes_repository_local_excludes_before_model_call(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    exclude = target_repo / ".git" / "info" / "exclude"
    exclude.write_text("", encoding="utf-8")
    (target_repo / ".gitignore").write_text(
        ".prompt-evals/**/reports/\n"
        ".prompt-evals/**/.runtime/\n"
        "__pycache__/\n"
        "*.pyc\n",
        encoding="utf-8",
    )
    transport = CountingTransport(scenario="no-change")

    result = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
    )

    assert result.stop_reason == "no_change_needed"
    assert transport.call_count > 0
    assert "/.worktrees/stabilizing-prompts/" in exclude.read_text(
        encoding="utf-8"
    )


def test_tune_retains_runtime_gate_failure_state_outside_managed_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    transport = CountingTransport()
    observed: dict[str, object] = {}
    real_create_cycle = support_module.create_cycle

    def observe_create_cycle(*args: object, **kwargs: object) -> object:
        observed["state_path"] = kwargs.get("state_path")
        return real_create_cycle(*args, **kwargs)

    monkeypatch.setattr(support_module, "create_cycle", observe_create_cycle)

    def fail_runtime(_cycle: object) -> tuple[str, str]:
        raise WorktreeError("runtime path is not ignored")

    monkeypatch.setattr(manage_worktree, "verify_runtime_ignores", fail_runtime)

    with pytest.raises(WorktreeError, match="runtime path is not ignored"):
        run_tune_with_fake_transport(
            target_repo,
            scenario="no-change",
            transport=transport,
        )

    state_value = observed.get("state_path")
    assert isinstance(state_value, Path)
    managed_root = target_repo / ".worktrees" / "stabilizing-prompts"
    assert not state_value.resolve().is_relative_to(managed_root.resolve())
    retained = load_cycle(state_value, require_current=True)
    assert retained.worktree.is_dir()
    assert retained.runtime_ignores_verified is False
    assert transport.call_count == 0


def test_tune_creates_project_local_worktree_before_model_call(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    worktree_root = target_repo / ".worktrees" / "stabilizing-prompts"

    class GateObservingTransport(CountingTransport):
        def __init__(self) -> None:
            super().__init__(scenario="no-change")
            self.worktree_ready_at_call: list[bool] = []

        def invoke(self, messages: object) -> object:
            self.worktree_ready_at_call.append(worktree_root.is_dir())
            return super().invoke(messages)

    transport = GateObservingTransport()

    result = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
    )

    assert result.stop_reason == "no_change_needed"
    assert transport.worktree_ready_at_call
    assert all(transport.worktree_ready_at_call)
    worktrees = tuple(worktree_root.iterdir())
    assert len(worktrees) == 1
    assert worktrees[0].is_dir()


def test_token_redaction_survives_fixture_transport_and_reports(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="token-redaction",
        sentinel="integration-sentinel-token",
    )

    serialized = json.dumps(
        {
            "result": result.to_dict(),
            "config": safe_client_config(),
            "runtime": _safe_serialize(result.raw_evidence),
            "redacted": _redacted(result.raw_evidence),
        },
        ensure_ascii=False,
    )
    assert "integration-sentinel-token" not in serialized
    assert "Authorization: Bearer integration-sentinel-token" not in serialized


def test_failure_assets_never_include_candidate_text_or_token(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="acceptance-failure",
        confirm_failure_delivery=True,
        sentinel="integration-sentinel-token",
    )

    delivered_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in target_repo.rglob("*")
        if (
            path.is_file()
            and ".git" not in path.parts
            and ".runtime" not in path.parts
            and path.relative_to(target_repo).parts[:2]
            != (".worktrees", "stabilizing-prompts")
        )
    )
    assert result.candidate_prompt not in delivered_text
    assert "integration-sentinel-token" not in delivered_text


@pytest.mark.parametrize("result_kind", ["success", "failure"])
def test_delivery_contains_coverage_obligations_for_both_results(
    tmp_path: Path, result_kind: str
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    if result_kind == "success":
        result = run_tune_with_fake_transport(target_repo)
    else:
        result = run_tune_with_fake_transport(
            target_repo,
            scenario="acceptance-failure",
            confirm_failure_delivery=True,
        )

    prompt_id = prompt_id_for_path("prompts/classify.md")
    coverage_path = f".prompt-evals/{prompt_id}/coverage-obligations.yaml"
    assert coverage_path in result.delivered_paths
    if result_kind == "failure":
        assert "prompts/classify.md" not in result.delivered_paths
