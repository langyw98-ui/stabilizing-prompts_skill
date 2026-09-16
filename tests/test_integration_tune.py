from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from tests import integration_support as support_module
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
    renewed = run_tune_with_fake_transport(
        target_repo,
        transport=transport,
        confirm_delivery=True,
    )
    assert renewed.stop_reason == "delivered"
    assert transport.calls


def test_complete_assets_tune_success_delivers_after_external_fixture_conditional(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)

    result = run_tune_with_fake_transport(
        target_repo,
        scenario="happy",
        confirm_delivery=True,
    )

    assert result.stop_reason == "delivered"
    assert result.formal_result is not None
    assert result.formal_result.kind == "acceptance_passed"
    assert result.cleanup_status == "complete"
    assert "external-cases.yaml" not in result.delivered_paths
    assert not (target_repo / ".worktrees" / "stabilizing-prompts").exists()


def test_actual_case_counts_drive_fixed_repeat_slots(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(target_repo)

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
    acceptance_baseline_payload = yaml.safe_load(
        (runtime / "acceptance-baseline.json").read_text(encoding="utf-8")
    )
    acceptance_candidate_payload = yaml.safe_load(
        (runtime / "acceptance-candidate.json").read_text(encoding="utf-8")
    )
    assert len(baseline_payload["slots"]) == 30 * 5
    assert len(validation_payload["slots"]) == 30 * 5
    assert len(acceptance_baseline_payload["slots"]) == 30 * 10
    assert len(acceptance_candidate_payload["slots"]) == 30 * 10


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


def test_reused_confirmation_rejects_changed_saturation_before_probe(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    transport = CountingTransport(scenario="no-change")

    initial = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
        saturation_statement="saturation statement A",
    )
    assert initial.stop_reason == "no_change_needed"
    calls_before_change = len(transport.calls)

    stale = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=transport,
        saturation_statement="saturation statement B",
    )

    assert stale.stop_reason == "setup_error"
    assert stale.baseline_dev is None
    assert transport.calls[calls_before_change:] == []


def test_confirmation_binds_every_coverage_evidence_field_before_probe(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    initial = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        saturation_statement="saturation statement A",
    )
    confirmation = getattr(initial, "confirmation_record", None)
    assert confirmation is not None

    mutations = {
        "coverage_obligations_hash": confirmation.coverage_obligations_hash + "-changed",
        "case_suite_hash": confirmation.case_suite_hash + "-changed",
        "evidence_checked": confirmation.evidence_checked + ("new-boundary.md",),
        "saturation_statement": "saturation statement B",
        "near_duplicate_review_status": "confirmed",
    }
    for field, value in mutations.items():
        stale_transport = CountingTransport(scenario="no-change")
        stale = run_tune_with_fake_transport(
            target_repo,
            scenario="no-change",
            transport=stale_transport,
            saturation_statement="saturation statement A",
            confirmation=replace(confirmation, **{field: value}),
        )

        assert stale.stop_reason == "setup_error", field
        assert stale.baseline_dev is None, field
        assert stale_transport.calls == [], field


def test_changed_case_suite_hash_invalidates_confirmation_and_baseline(
    tmp_path: Path,
) -> None:
    target_repo = build_target_repo(tmp_path / "target-repo", complete_assets=True)
    initial = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        saturation_statement="saturation statement A",
    )
    confirmation = getattr(initial, "confirmation_record", None)
    assert confirmation is not None

    dev_path = _coverage_root(target_repo) / "dev-cases.yaml"
    cases = yaml.safe_load(dev_path.read_text(encoding="utf-8"))
    cases[0]["input"]["variables"]["message"] += " with an audited alias"
    dev_path.write_text(yaml.safe_dump(cases, sort_keys=False), encoding="utf-8")

    stale_transport = CountingTransport(scenario="no-change")
    stale = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        transport=stale_transport,
        saturation_statement="saturation statement A",
        confirmation=confirmation,
    )

    assert stale.stop_reason == "setup_error"
    assert stale.baseline_dev is None
    assert stale_transport.calls == []


def test_fixture_catalog_uses_distinct_business_boundaries_not_index_variants() -> None:
    cases = [make_case("dev", index) for index in range(30)]
    variables = [case["input"]["variables"] for case in cases]

    assert len({case["semantic_family"] for case in cases}) == 30
    assert len({case["coverage"]["condition_id"] for case in cases}) == 30
    assert {
        "boundary",
        "channel",
        "account_status",
        "risk_level",
        "amount_cents",
        "jurisdiction",
        "device_trust",
        "velocity",
        "consent",
    } <= set(variables[0])
    assert len({tuple(sorted(item.items())) for item in variables}) == 30
    assert {case["expect"]["output"]["action"] for case in cases} == {
        "accept",
        "reject",
    }


def test_counting_transport_derives_decision_from_business_input() -> None:
    case = make_case("dev", 0)
    transport = CountingTransport(scenario="no-change")
    expected = case["expect"]["output"]

    assert transport._expected(case["input"]) == (
        expected["action"],
        expected["reason"],
    )

    blocked = copy.deepcopy(case["input"])
    blocked["variables"]["account_status"] = "suspended"
    assert transport._expected(blocked) == ("reject", "account-not-eligible")


def test_acceptance_case_loader_is_bootstrap_only(
    target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = CountingTransport(scenario="no-change")
    original_loader = support_module.load_case_suite
    observed: list[tuple[str | None, tuple[str, ...]]] = []
    file_access: list[tuple[str | None, Path]] = []

    original_read_text = Path.read_text

    def traced_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "acceptance-cases.yaml":
            file_access.append(
                (
                    transport.lifecycle_events[-1]
                    if transport.lifecycle_events
                    else None,
                    path,
                )
            )
        return original_read_text(path, *args, **kwargs)

    def traced_loader(paths: object, *args: object, **kwargs: object) -> object:
        path_names = tuple(Path(path).name for path in paths)  # type: ignore[arg-type]
        if "acceptance-cases.yaml" in path_names:
            observed.append(
                (
                    transport.lifecycle_events[-1]
                    if transport.lifecycle_events
                    else None,
                    path_names,
                )
            )
        return original_loader(paths, *args, **kwargs)

    monkeypatch.setattr(support_module, "load_case_suite", traced_loader)
    monkeypatch.setattr(Path, "read_text", traced_read_text)
    result = run_tune_with_fake_transport(
        target_repo, scenario="no-change", transport=transport
    )

    assert result.stop_reason == "no_change_needed"
    assert observed
    assert all(stage == "mechanical-validation" for stage, _ in observed)
    assert file_access
    assert all(stage == "mechanical-validation" for stage, _ in file_access)
    assert not any(
        stage.startswith("phase:")
        for stage, _ in observed
        if stage is not None
    )


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
    assert "probe" not in transport.lifecycle_events

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


def test_no_change_finalizes_assets_and_cleans_after_confirmation(
    target_repo: Path,
) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        confirm_delivery=True,
    )

    assert result.stop_reason == "no_change_needed"
    assert result.delivery_profile == "assets"
    assert result.prepared_commit == result.delivery_commit
    assert result.summary_path is not None
    assert (target_repo / result.summary_path).is_file()
    assert result.cleanup_status == "complete"
    assert not (target_repo / ".worktrees" / "stabilizing-prompts").exists()


def test_declined_formal_result_retains_prepared_cycle_without_workspace_delivery(
    target_repo: Path,
) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        confirm_delivery=False,
    )

    assert result.prepared_commit is not None
    assert result.delivery_commit is None
    assert result.cleanup_status == "retained"
    assert workspace_snapshot(target_repo) == before
    assert len(tuple((target_repo / ".worktrees" / "stabilizing-prompts").iterdir())) == 1


def test_ambiguous_delivery_confirmation_retains_prepared_cycle(
    target_repo: Path,
) -> None:
    before = workspace_snapshot(target_repo)
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="no-change",
        confirm_delivery=None,
    )

    assert result.formal_result is not None
    assert result.formal_result.kind == "no_change_needed"
    assert result.prepared_commit is not None
    assert result.delivery_commit is None
    assert result.cleanup_status == "retained"
    assert workspace_snapshot(target_repo) == before


def test_omitted_delivery_confirmation_never_implicitly_delivers_success(
    target_repo: Path,
) -> None:
    before = workspace_snapshot(target_repo)

    result = run_tune_with_fake_transport(target_repo, scenario="happy")

    assert result.stop_reason == "acceptance_passed"
    assert result.formal_result is not None
    assert result.formal_result.kind == "acceptance_passed"
    assert result.prepared_commit is not None
    assert result.delivery_commit is None
    assert result.cleanup_status == "retained"
    assert result.delivered_paths == ()
    assert workspace_snapshot(target_repo) == before
    assert (target_repo / ".worktrees" / "stabilizing-prompts").is_dir()


@pytest.mark.parametrize(
    ("scenario", "kind", "acceptance_count", "profile", "prompt_changes"),
    [
        ("no-change", "no_change_needed", 0, "assets", False),
        ("no-strict-improvement", "no_strict_improvement", 0, "assets", False),
        ("regression", "validation_failed", 0, "assets", False),
        ("no-improvement-limit", "no_improvement_limit", 0, "assets", False),
        ("round-limit", "round_limit", 0, "assets", False),
        ("acceptance-failure", "acceptance_failed", 1, "assets", False),
        ("happy", "acceptance_passed", 1, "success", True),
    ],
)
def test_every_scored_terminal_result_finalizes_and_delivers_exact_profile(
    target_repo: Path,
    scenario: str,
    kind: str,
    acceptance_count: int,
    profile: str,
    prompt_changes: bool,
) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        scenario=scenario,
        confirm_delivery=True,
    )

    assert result.formal_result is not None
    assert result.formal_result.kind == kind
    assert result.summary_path is not None
    assert result.prepared_commit is not None
    assert result.delivery_commit is not None
    assert result.delivery_profile == profile
    assert result.acceptance_activities == acceptance_count
    assert result.cleanup_status == "complete"
    assert (target_repo / result.summary_path).is_file()
    if profile == "assets":
        assert result.delivery_commit == result.prepared_commit
    else:
        assert result.delivery_commit != result.prepared_commit
        assert support_module._git(
            target_repo,
            "rev-parse",
            f"{result.delivery_commit}^",
        ).strip() == result.prepared_commit
    assert support_module._git(
        target_repo,
        "show",
        f"{result.prepared_commit}:{result.summary_path}",
    )

    history = yaml.safe_load(
        (target_repo / ".prompt-evals" / next(target_repo.glob(".prompt-evals/*")).name / "optimization-history.yaml").read_text(
            encoding="utf-8"
        )
    )
    entries = history.get("cycles", []) if isinstance(history, dict) else []
    assert sum(
        isinstance(entry, dict) and entry.get("result") == kind
        for entry in entries
    ) == 1
    assert all(
        ".runtime/" not in path
        and "/reports/" not in path
        and "raw" not in path
        for path in result.delivered_paths
    )
    prompt = (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8")
    assert prompt == (result.candidate_prompt if prompt_changes else result.original_prompt)
    assert not (target_repo / ".worktrees" / "stabilizing-prompts").exists()


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
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="equal-perfect",
        confirm_delivery=True,
    )

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
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="conflict",
        confirm_delivery=True,
    )

    assert result.stop_reason == "delivery_conflict"
    assert result.delivered_prompt_hash is None
    assert result.delivered_paths == ()
    assert (target_repo / "prompts" / "classify.md").read_text(encoding="utf-8") == "user edit wins\n"
    expected = dict(before[0])
    expected["prompts/classify.md"] = (target_repo / "prompts" / "classify.md").read_bytes()
    assert workspace_snapshot(target_repo) == (expected, " M prompts/classify.md\n")


def test_interrupted_slot_resumes_same_identity_without_extra_slot(target_repo: Path) -> None:
    result = run_tune_with_fake_transport(
        target_repo,
        scenario="resume",
        confirm_delivery=True,
    )

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
        confirm_delivery=True,
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
