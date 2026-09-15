from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.local_model_client import safe_client_config
from scripts.manage_worktree import WorktreeError
from scripts.validate_workspace import prompt_id_for_path
from scripts.run_prompt_eval import _redacted, _safe_serialize
from tests.integration_support import (
    CountingTransport,
    build_target_repo,
    run_cli_chain,
    run_tune_with_fake_transport,
)


def test_cli_chain_uses_production_renderer_and_schema_without_duplicate_contract(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)

    evidence = run_cli_chain(target_repo)

    assert evidence.workspace_status == "valid"
    assert evidence.case_suite_status == "valid"
    assert evidence.run_status == "complete"
    assert evidence.score_status == "complete"
    assert evidence.comparison_status == "passed"
    assert evidence.schema_name == "Decision"
    assert evidence.renderer_marker == "production-renderer"


def test_delivery_rollback_restores_exact_targets_after_post_apply_failure(tmp_path: Path) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    result = run_tune_with_fake_transport(target_repo, scenario="rollback")

    assert result.stop_reason == "delivery_rollback"
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt
    assert not result.partially_delivered_paths


def test_tune_rejects_unignored_project_local_worktree_before_model_call(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo")
    (target_repo / ".git" / "info" / "exclude").write_text("", encoding="utf-8")
    (target_repo / ".gitignore").write_text(
        ".prompt-evals/**/reports/\n"
        ".prompt-evals/**/.runtime/\n"
        "__pycache__/\n"
        "*.pyc\n",
        encoding="utf-8",
    )
    transport = CountingTransport()

    with pytest.raises(WorktreeError, match="not ignored"):
        run_tune_with_fake_transport(target_repo, transport=transport)

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
