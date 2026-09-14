from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "SKILL.md"
CASE_SCHEMA = ROOT / "references" / "case-schema.md"
BUSINESS_CONTRACT = ROOT / "references" / "business-contract.md"


@pytest.fixture
def skill_text() -> str:
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture
def case_schema_text() -> str:
    return CASE_SCHEMA.read_text(encoding="utf-8")


@pytest.fixture
def business_contract_text() -> str:
    return BUSINESS_CONTRACT.read_text(encoding="utf-8")


def _section(skill_text: str, heading: str, next_heading: str) -> str:
    return skill_text.split(heading, 1)[1].split(next_heading, 1)[0]


def test_verify_forbids_acceptance_dataset(skill_text):
    verify = skill_text.split("## verify", 1)[1]
    assert "must not read or run `acceptance-cases.yaml`" in verify


def test_tune_contains_both_user_gates_and_stop_conditions(skill_text):
    assert "contract confirmation gate" in skill_text
    assert "delivery confirmation gate" in skill_text
    for reason in (
        "two consecutive rounds",
        "five candidate rounds",
        "contract conflict",
        "acceptance failure",
    ):
        assert reason in skill_text


def test_tune_states_are_explicit_and_ordered(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    states = (
        "preflight",
        "worktree",
        "contract/cases/adapter",
        "user confirmation",
        "model probe/smoke",
        "asset commit",
        "dev/validation baseline",
        "no-change exit or candidate loop",
        "candidate freeze",
        "single acceptance activity",
        "failure exit or delivery confirmation",
        "worktree commit",
        "allowlisted synchronization",
    )
    state_line = next(
        line for line in tune.splitlines() if line.startswith("`preflight")
    )
    positions = [state_line.index(state) for state in states]
    assert positions == sorted(positions)


def test_all_supporting_cli_contracts_are_documented(skill_text):
    contracts = (
        "validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode tune|verify --output WORKSPACE_JSON",
        "validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON",
        "run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|acceptance|external --repeats N --manifest PATH",
        "score_results.py --manifest PATH --report PATH",
        "compare_runs.py --baseline PATH --candidate PATH --phase development|validation|acceptance --report PATH",
        "manage_worktree.py create --repo PATH --prompt-id ID --state PATH",
        "manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure",
        "manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON",
    )
    for contract in contracts:
        assert contract in skill_text


def test_tune_documents_confirmation_before_any_model_call(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    confirmation = tune.index("contract confirmation gate")
    model_call = tune.index("model probe/smoke")
    assert confirmation < model_call
    assert "contract, cases, adapter" in tune
    assert "delivery confirmation gate" in tune


def test_tune_separates_mechanical_coverage_from_human_saturation(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    mechanical = tune.index("mechanical coverage")
    saturation = tune.index("saturation statement")
    confirmation = tune.index("user confirmation")
    probe = tune.index("model probe")
    assert mechanical < saturation < confirmation < probe
    assert "invalidate" in tune
    assert "coverage_obligations_hash" in tune
    assert "run manifest binds" not in tune


def test_case_schema_documents_required_coverage_metadata(case_schema_text):
    example = case_schema_text.split("```yaml", 1)[1].split("```", 1)[0]
    for field in (
        "coverage:",
        "primary_obligation:",
        "secondary_obligations:",
        "variant:",
        "condition_id:",
    ):
        assert field in example
    assert "每条案例必须" in case_schema_text


def test_coverage_asset_lifecycle_is_proposed_frozen_then_committed(
    skill_text, business_contract_text, case_schema_text
):
    tune = _section(skill_text, "## tune", "## verify")
    proposed = tune.index("proposed/editable `coverage-obligations.yaml`")
    frozen = tune.index("freezes the contract, coverage obligations")
    committed = tune.index("after the model probe/smoke succeeds, commit")
    assert proposed < frozen < committed

    for text in (business_contract_text, case_schema_text):
        assert "proposed/editable" in text
        assert "frozen by explicit user confirmation" in text
        assert "committed after the model probe/smoke" in text


def test_confirmation_binds_complete_coverage_audit_evidence(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    confirmation = _section(
        tune, "### 4. user confirmation (contract confirmation gate)",
        "### 5. model probe/smoke",
    )
    for field in (
        "coverage_obligations_hash",
        "case_suite_hash",
        "evidence_checked",
        "saturation_statement",
        "near-duplicate review confirmation/status",
    ):
        assert field in confirmation
    assert "remains cycle state" in confirmation
    assert "not a project asset or a CLI input" in confirmation


def test_coverage_obligations_are_mandatory_inputs(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    assert "coverage-obligations.yaml` when present" not in tune
    assert "mandatory proposed/editable `coverage-obligations.yaml`" in tune
    assert "confirmed `coverage-obligations.yaml`" in tune


def test_tune_documents_project_local_worktree_gate(skill_text):
    tune = skill_text.split("## verify", 1)[0]
    assert ".worktrees/stabilizing-prompts" in tune
    assert "primary workspace" in tune
    assert "git check-ignore" in tune
    assert "never" in tune and "fall back" in tune
    assert "--worktree" not in tune

    setup_steps = (
        "resolve primary checkout identity",
        "reject linked worktree or detached HEAD",
        "derive .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/",
        "verify that directory is ignored with git check-ignore",
        "create branch and worktree with git worktree add",
        "persist WorktreeCycle",
    )
    positions = [tune.index(step) for step in setup_steps]
    assert positions == sorted(positions)


def test_tune_requires_user_ignore_setup_and_never_delivers_gitignore(skill_text):
    tune = skill_text.split("## verify", 1)[0]
    worktree = _section(tune, "### 2. worktree", "### 3. contract/cases/adapter")
    asset_commit = _section(tune, "### 6. asset commit", "### 7. dev/validation baseline")

    assert "user" in worktree
    assert "before invoking `tune`" in worktree.casefold()
    assert "`.worktrees/`" in worktree
    assert "Do not edit, stage, or commit the target repository's `.gitignore`" in worktree
    assert "any required evaluation ignore rules" not in asset_commit
    assert "`.gitignore`" in asset_commit
    assert "edit, stage, or commit" in asset_commit


def test_tune_separates_delivery_stale_state_guard_from_setup_gate(skill_text):
    tune = skill_text.split("## verify", 1)[0]
    worktree = _section(tune, "### 2. worktree", "### 3. contract/cases/adapter")
    delivery = _section(tune, "### 13. allowlisted synchronization", "### CLI contracts")

    assert "before the first model call" in worktree
    assert "git worktree add" in worktree
    assert "state-persistence failure" in worktree
    assert "stale/invalid cycle" not in worktree
    assert "critical dependency change" not in worktree
    assert "Delivery-time stale-state guard" in delivery
    assert "after model phases" in delivery


def test_tune_documents_cycle_worktree_continuity_and_acceptance_ownership(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    assert "same cycle" in tune
    assert "same worktree" in tune
    assert "bootstrap" in tune
    assert "acceptance-cases.yaml" in tune
    assert "once per cycle" in tune
    assert "only `tune`" in tune


def test_tune_documents_isolated_phases_and_no_change_exit(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    assert "development" in tune
    assert "validation" in tune
    assert "acceptance" in tune
    assert "never run acceptance" in tune or "do not run acceptance" in tune
    assert "no_change_needed" in tune
    assert "does not generate a candidate" in tune


def test_tune_documents_success_and_failure_delivery_gates(skill_text):
    tune = _section(skill_text, "## tune", "## verify")
    assert "success" in tune and "failure" in tune
    assert "acceptance passes" in tune
    assert "acceptance fails" in tune
    assert "failure-asset-only" in tune
    assert "explicitly confirm" in tune
    assert "does not deliver the candidate" in tune


def test_verify_is_read_only_and_rejects_acceptance_before_loading(skill_text):
    verify = skill_text.split("## verify", 1)[1]
    guard = verify.split("### verify state machine", 1)[0]
    reject = guard.index("Reject")
    load = guard.index("loading")
    client = guard.index("client")
    assert reject < load < client
    assert "read-only" in verify
    assert "current workspace" in verify
    assert "does not modify" in verify
    assert "--dataset acceptance" in verify
