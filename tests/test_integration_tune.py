from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from tests.integration_support import (
    CountingTransport,
    TuneResult,
    assert_delivered_files_unstaged_or_untracked,
    build_target_repo,
    make_case,
    remove_last_case,
    run_tune_with_fake_transport,
    workspace_snapshot,
)


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    return build_target_repo(tmp_path / "target-repo")


def _coverage_root(target_repo: Path) -> Path:
    return next(target_repo.glob(".prompt-evals/*"))


def test_tune_stops_before_transport_when_mechanical_coverage_fails(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    validation_path = _coverage_root(target_repo) / "validation-cases.yaml"
    remove_last_case(validation_path)
    transport = CountingTransport()

    result = run_tune_with_fake_transport(target_repo, transport=transport)

    assert result.stop_reason == "setup_error"
    assert transport.calls == []
    assert result.baseline_dev is None

    cases = yaml.safe_load(validation_path.read_text(encoding="utf-8"))
    cases.append(make_case("validation", 29, variant="boundary"))
    validation_path.write_text(
        yaml.safe_dump(cases, sort_keys=False), encoding="utf-8"
    )
    renewed = run_tune_with_fake_transport(target_repo, transport=transport)
    assert renewed.stop_reason == "delivered"
    assert transport.calls


def test_actual_case_counts_drive_fixed_repeat_slots(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="no-change")

    assert result.baseline_dev is not None
    assert result.baseline_validation is not None
    assert len(result.baseline_dev.slots) == 30 * 5
    assert len(result.baseline_validation.slots) == 30 * 5
    assert result.slot_estimates == {
        "baseline_slots": 30 * 5 + 30 * 5,
        "one_full_promoted_candidate_round": 30 * 5 + 30 * 5 + 30 * 5,
        "affected_dev_pre_run_slots": 30 * 5,
        "paired_acceptance_slots": 30 * 10 + 30 * 10,
    }
    worktree = next((target_repo / ".worktrees" / "stabilizing-prompts").iterdir())
    runtime = next(worktree.glob(".prompt-evals/*")) / ".runtime"
    baseline_payload = yaml.safe_load(
        (runtime / "baseline-dev.json").read_text(encoding="utf-8")
    )
    validation_payload = yaml.safe_load(
        (runtime / "baseline-validation.json").read_text(encoding="utf-8")
    )
    assert len(baseline_payload["slots"]) == 30 * 5
    assert len(validation_payload["slots"]) == 30 * 5


def test_changed_coverage_hash_invalidates_confirmation_and_baseline(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    transport = CountingTransport(scenario="no-change")

    initial = run_tune_with_fake_transport(
        target_repo, scenario="no-change", transport=transport
    )
    assert initial.stop_reason == "no_change_needed"
    assert initial.coverage_obligations_hash is not None
    assert initial.case_suite_hash is not None
    calls_before_change = len(transport.calls)

    obligations_path = _coverage_root(target_repo) / "coverage-obligations.yaml"
    obligations_path.write_text(
        obligations_path.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    stale_transport = CountingTransport(scenario="no-change")
    stale = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=stale_transport,
        confirmation_hashes=(
            initial.coverage_obligations_hash,
            initial.case_suite_hash,
        ),
    )

    assert stale.stop_reason == "setup_error"
    assert stale.baseline_dev is None
    assert stale_transport.calls == []
    assert len(transport.calls) == calls_before_change

    renewed = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=CountingTransport(scenario="no-change"),
    )
    assert renewed.stop_reason == "no_change_needed"
    assert renewed.transport_calls > 0


def test_explained_near_duplicate_requires_confirmation_before_probe(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    dev_path = _coverage_root(target_repo) / "dev-cases.yaml"
    cases = yaml.safe_load(dev_path.read_text(encoding="utf-8"))
    left = cases[0]
    right = copy.deepcopy(cases[2])
    right["expect"] = copy.deepcopy(left["expect"])
    right["coverage"]["primary_obligation"] = left["coverage"][
        "primary_obligation"
    ]
    right["input"] = copy.deepcopy(left["input"])
    right["input"]["variables"]["message"] += "!"
    cases[0], cases[2] = left, right
    dev_path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")

    transport = CountingTransport(scenario="no-change")
    unexplained = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
    )
    assert unexplained.stop_reason == "setup_error"
    assert transport.calls == []

    cases = yaml.safe_load(dev_path.read_text(encoding="utf-8"))
    cases[0]["coverage"]["distinction"] = "different evidenced decision boundary"
    cases[2]["coverage"]["distinction"] = "different evidenced decision boundary"
    dev_path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")

    unreviewed = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
        confirm_near_duplicate_review=False,
    )

    assert unreviewed.stop_reason == "setup_error"
    assert transport.calls == []

    reviewed = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
        confirm_near_duplicate_review=True,
    )
    assert reviewed.stop_reason == "no_change_needed"
    assert transport.calls


def test_acceptance_remains_unread_before_candidate_freeze_with_coverage(
    target_repo: Path,
) -> None:
    result = run_tune_with_fake_transport(target_repo)

    events = result.lifecycle_events
    freeze_index = events.index("candidate-freeze")
    acceptance_index = events.index("phase:acceptance-baseline")
    assert freeze_index < acceptance_index
    assert not any(
        event.startswith("call:acceptance-")
        for event in events[:freeze_index]
    )


def test_tune_initializes_and_delivers_only_after_acceptance_and_confirmation(
    target_repo: Path,
) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        confirm_contract=True,
        confirm_delivery=True,
    )

    assert isinstance(result, TuneResult)
    assert result.acceptance_activities == 1
    assert result.delivered_prompt_hash == result.frozen_candidate_hash
    assert result.confirmation_hashes == (
        result.coverage_obligations_hash,
        result.case_suite_hash,
    )
    assert result.lifecycle_events.index("confirmation") < result.lifecycle_events.index("probe")
    assert result.transport_calls == len(result.raw_evidence)
    assert_delivered_files_unstaged_or_untracked(target_repo, result.delivered_paths)
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.candidate_prompt


def test_workspace_snapshot_ignores_managed_worktree_but_captures_other_nested_worktree(
    target_repo: Path,
) -> None:
    managed_asset = (
        target_repo
        / ".worktrees"
        / "stabilizing-prompts"
        / "retained-cycle"
        / "prompt.txt"
    )
    managed_asset.parent.mkdir(parents=True)
    managed_asset.write_text("retained checkout\n", encoding="utf-8")

    nested_asset = target_repo / "fixtures" / ".worktrees" / "meaningful.txt"
    nested_asset.parent.mkdir(parents=True)
    nested_asset.write_bytes(b"meaningful workspace asset\n")

    sibling_asset = target_repo / ".worktrees" / "other" / "meaningful.txt"
    sibling_asset.parent.mkdir(parents=True)
    sibling_asset.write_bytes(b"sibling workspace asset\n")

    files, _ = workspace_snapshot(target_repo)

    assert ".worktrees/stabilizing-prompts/retained-cycle/prompt.txt" not in files
    assert files["fixtures/.worktrees/meaningful.txt"] == b"meaningful workspace asset\n"
    assert files[".worktrees/other/meaningful.txt"] == b"sibling workspace asset\n"


def test_tune_does_not_run_model_before_contract_confirmation(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        confirm_contract=False,
        confirm_delivery=True,
    )

    assert result.stop_reason == "contract_not_confirmed"
    assert result.transport_calls == 0
    assert result.acceptance_activities == 0
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt


def test_tune_no_change_exit_skips_candidate_and_acceptance(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="no-change")

    assert result.stop_reason == "no_change_needed"
    assert result.acceptance_activities == 0
    assert result.candidate_prompt is None
    assert result.transport_calls > 0
    assert not result.delivered_paths
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt


def test_validation_regression_rejects_candidate_before_acceptance(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="regression")

    assert result.stop_reason == "validation_failed"
    assert result.acceptance_activities == 0
    assert result.delivered_prompt_hash is None
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt


def test_equal_perfect_acceptance_is_allowed_and_delivers(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="equal-perfect")

    assert result.stop_reason == "delivered"
    assert result.acceptance_activities == 1
    assert result.acceptance_baseline_perfect is True
    assert result.acceptance_candidate_perfect is True
    assert result.delivered_prompt_hash == result.frozen_candidate_hash


def test_acceptance_failure_never_delivers_candidate(target_repo: Path) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(target_repo, scenario="acceptance-failure")

    assert result.stop_reason == "acceptance_failed"
    assert result.acceptance_activities == 1
    assert result.delivered_prompt_hash is None
    assert result.delivered_paths == ()
    assert result.candidate_prompt is not None
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt
    assert workspace_snapshot(target_repo) == before


def test_acceptance_failure_can_sync_only_confirmed_assets(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="acceptance-failure",
        confirm_failure_delivery=True,
    )

    assert result.stop_reason == "acceptance_failed"
    assert result.delivered_prompt_hash is None
    assert result.delivered_paths
    assert all(path != "prompts/classify.md" for path in result.delivered_paths)
    assert any(path.startswith(".prompt-evals/") for path in result.delivered_paths)
    assert_delivered_files_unstaged_or_untracked(target_repo, result.delivered_paths)


def test_delivery_confirmation_decline_preserves_original_workspace(target_repo: Path) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(target_repo, confirm_delivery=False)

    assert result.stop_reason == "delivery_not_confirmed"
    assert result.acceptance_activities == 1
    assert result.delivered_prompt_hash is None
    assert result.delivered_paths == ()
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == result.original_prompt
    assert workspace_snapshot(target_repo) == before


def test_original_workspace_conflict_stops_without_overwriting_user_edit(
    target_repo: Path,
) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(target_repo, scenario="conflict")

    assert result.stop_reason == "delivery_conflict"
    assert result.delivered_prompt_hash is None
    assert result.delivered_paths == ()
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == "user edit wins\n"
    expected = dict(before[0])
    expected["prompts/classify.md"] = (target_repo / "prompts" / "classify.md").read_bytes()
    assert workspace_snapshot(target_repo) == (expected, " M prompts/classify.md\n")


def test_interrupted_slot_resumes_same_identity_without_extra_slot(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo, scenario="resume")

    assert result.stop_reason == "delivered"
    assert result.resumed_slot_key is not None
    assert result.resumed_slot_key in result.completed_slot_keys
    assert result.transport_retry_slots == (result.resumed_slot_key,)
    assert result.acceptance_activities == 1
    assert result.slot_call_counts[result.resumed_slot_key] == 2
    assert result.slot_attempts[result.resumed_slot_key] == 2
    complete_slots = set(result.completed_slot_keys) - {result.resumed_slot_key}
    assert complete_slots
    assert all(result.slot_call_counts[key] == 1 for key in complete_slots)
    assert all(result.slot_attempts[key] == 1 for key in complete_slots)


def test_dirty_unrelated_file_is_preserved_while_critical_dependency_is_rejected(
    target_repo: Path,
) -> None:
    unrelated = target_repo / "notes.txt"
    unrelated.write_text("unrelated user edit\n", encoding="utf-8")

    allowed = run_tune_with_fake_transport(target_repo, scenario="no-change")
    assert allowed.stop_reason == "no_change_needed"
    assert unrelated.read_text(encoding="utf-8") == "unrelated user edit\n"

    dependency = target_repo / "target_app" / "production.py"
    dependency.write_text(dependency.read_text(encoding="utf-8") + "# dirty\n", encoding="utf-8")
    rejected = run_tune_with_fake_transport(target_repo, scenario="no-change")
    assert rejected.stop_reason == "dependency_dirty"
    assert rejected.transport_calls == 0
    assert unrelated.read_text(encoding="utf-8") == "unrelated user edit\n"


def test_fake_transport_is_test_injected_and_not_project_selectable(target_repo: Path) -> None:
    transport = CountingTransport()
    result = run_tune_with_fake_transport(
        target_repo,
        transport=transport,
        project_transport_setting="fake",
    )

    assert result.stop_reason == "delivered"
    assert transport.call_count == result.transport_calls
    eval_config = next(target_repo.glob(".prompt-evals/*/eval-config.yaml"))
    assert "transport: fake" in eval_config.read_text(encoding="utf-8")
    assert transport.transport_identity == "test-only-counting-transport"
    assert transport.structured_output_kwargs
    assert all(
        kwargs == {"method": "function_calling", "include_raw": True}
        for kwargs in transport.structured_output_kwargs
    )
