"""Test-only orchestration and transport boundary for offline integration tests.

The production scripts intentionally have no fake-transport configuration.  This
module injects a ChatOpenAI-shaped client through the public ``client`` argument
or a temporary monkeypatch around the runner CLI, and keeps all target-repository
files in disposable Git fixtures.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from contextlib import redirect_stdout
from dataclasses import dataclass, field, replace
import hashlib
from io import StringIO
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Literal

import yaml
from langchain_core.messages import AIMessage

from scripts import compare_runs as compare_module
from scripts import run_prompt_eval as runner_module
from scripts import validate_cases as cases_module
from scripts import validate_workspace as workspace_module
from scripts.local_model_client import MODEL_NAME, probe_model, safe_client_config
from scripts.manage_worktree import (
    FAILURE_ALLOWLIST,
    SUCCESS_ALLOWLIST,
    DeliveryConflict,
    DeliveryError,
    WorktreeCycle,
    apply_delivery_patch,
    build_delivery_patch,
    create_cycle,
)
from scripts.run_prompt_eval import (
    RunManifest,
    execute_run,
    load_adapter,
    load_manifest,
    new_manifest,
    persist_manifest,
    pending_slots,
)
from scripts.score_results import RunMetrics, score_run
from scripts.validate_cases import (
    CaseSetupError,
    CaseSuite,
    EvalCase,
    coverage_obligations_hash,
    dataset_hash,
    load_case_suite,
    load_coverage_obligations,
)
from scripts.validate_workspace import WorkspaceError, prompt_id_for_path, validate_workspace


FIXTURE_SOURCE = Path(__file__).parent / "fixtures" / "target_repo"
PROMPT_RELATIVE = "prompts/classify.md"
DEPENDENCY_RELATIVE = "target_app/production.py"

DEFAULT_EVIDENCE_CHECKED = (
    "target_app/production.py",
    "prompts/classify.md",
    "references/business-contract.md",
    "repository-tests",
)
DEFAULT_SATURATION_STATEMENT = (
    "Scanned the production Schema, renderer, business contract, and fixture "
    "history; no additional evidence-backed boundaries remain."
)
NEAR_DUPLICATE_REVIEW_NOT_REQUIRED = "not_required"
NEAR_DUPLICATE_REVIEW_CONFIRMED = "confirmed"
NEAR_DUPLICATE_REVIEW_REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ConfirmationRecord:
    """All user-confirmed evidence that gates the first model call."""

    coverage_obligations_hash: str
    case_suite_hash: str
    evidence_checked: tuple[str, ...]
    saturation_statement: str
    near_duplicate_review_status: str

    def __post_init__(self) -> None:
        if not isinstance(self.coverage_obligations_hash, str):
            raise TypeError("coverage_obligations_hash must be a string")
        if not isinstance(self.case_suite_hash, str):
            raise TypeError("case_suite_hash must be a string")
        if not isinstance(self.evidence_checked, tuple) or any(
            not isinstance(item, str) for item in self.evidence_checked
        ):
            raise TypeError("evidence_checked must be a tuple of strings")
        if not isinstance(self.saturation_statement, str):
            raise TypeError("saturation_statement must be a string")
        if not isinstance(self.near_duplicate_review_status, str):
            raise TypeError("near_duplicate_review_status must be a string")

    def to_dict(self) -> dict[str, object]:
        return {
            "coverage_obligations_hash": self.coverage_obligations_hash,
            "case_suite_hash": self.case_suite_hash,
            "evidence_checked": list(self.evidence_checked),
            "saturation_statement": self.saturation_statement,
            "near_duplicate_review_status": self.near_duplicate_review_status,
        }


# Keep a descriptive alias available to integration callers that refer to the
# cycle value as coverage confirmation rather than a generic record.
CoverageConfirmation = ConfirmationRecord


@dataclass(frozen=True, slots=True)
class _FixtureCaseSpec:
    """One business boundary in the deterministic routing catalog."""

    boundary: str
    message: str
    channel: str
    account_status: str
    risk_level: str
    amount_cents: int
    jurisdiction: str
    device_trust: str
    velocity: int
    consent: bool
    partner_status: str
    expected_action: Literal["accept", "reject"]
    expected_reason: str


_FIXTURE_CASE_CATALOG = (
    _FixtureCaseSpec(
        "known-web-low-risk",
        "Verified customer submits a low-risk web request within the standard limit.",
        "web",
        "verified",
        "low",
        2500,
        "us",
        "known",
        0,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "trusted-api-small",
        "Verified service account sends a trusted low-risk API request.",
        "api",
        "verified",
        "low",
        1200,
        "gb",
        "trusted",
        1,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "consented-mobile",
        "Verified customer uses a known mobile device for a consented request.",
        "mobile",
        "verified",
        "low",
        750,
        "ca",
        "known",
        0,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "approved-partner",
        "Verified partner traffic arrives through an approved integration.",
        "partner",
        "verified",
        "low",
        500,
        "fr",
        "known",
        0,
        True,
        "trusted",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "batch-at-limit",
        "Verified batch settlement reaches the published amount boundary.",
        "batch",
        "verified",
        "low",
        100000,
        "de",
        "trusted",
        2,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "zero-amount-web",
        "Verified web request carries the permitted zero-value amount.",
        "web",
        "verified",
        "low",
        0,
        "jp",
        "known",
        0,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "mobile-velocity-boundary",
        "Verified mobile customer remains at the hourly velocity boundary.",
        "mobile",
        "verified",
        "low",
        300,
        "au",
        "trusted",
        3,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "trusted-api-euro",
        "Verified European service sends a low-risk API request.",
        "api",
        "verified",
        "low",
        4200,
        "nl",
        "known",
        1,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "strong-partner-device",
        "Verified partner traffic uses a strongly trusted device signal.",
        "partner",
        "verified",
        "low",
        6400,
        "sg",
        "trusted",
        2,
        True,
        "trusted",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "web-upper-amount-boundary",
        "Verified web customer reaches the inclusive upper amount boundary.",
        "web",
        "verified",
        "low",
        100000,
        "us",
        "trusted",
        3,
        True,
        "not_applicable",
        "accept",
        "verified-low-risk",
    ),
    _FixtureCaseSpec(
        "suspended-account",
        "Suspended account attempts an otherwise low-risk web request.",
        "web",
        "suspended",
        "low",
        50,
        "us",
        "known",
        0,
        True,
        "not_applicable",
        "reject",
        "account-not-eligible",
    ),
    _FixtureCaseSpec(
        "closed-account",
        "Closed account submits a low-risk mobile request.",
        "mobile",
        "closed",
        "low",
        70,
        "ca",
        "trusted",
        0,
        True,
        "not_applicable",
        "reject",
        "account-not-eligible",
    ),
    _FixtureCaseSpec(
        "pending-account",
        "Pending account sends a low-risk API request before verification.",
        "api",
        "pending",
        "low",
        90,
        "gb",
        "trusted",
        1,
        True,
        "not_applicable",
        "reject",
        "account-not-eligible",
    ),
    _FixtureCaseSpec(
        "unverified-account",
        "Unverified account reaches the partner entry boundary.",
        "partner",
        "unverified",
        "low",
        110,
        "fr",
        "known",
        0,
        True,
        "trusted",
        "reject",
        "account-not-eligible",
    ),
    _FixtureCaseSpec(
        "sanctioned-jurisdiction",
        "Verified customer originates in a sanctioned jurisdiction.",
        "web",
        "verified",
        "low",
        150,
        "sanctioned",
        "known",
        0,
        True,
        "not_applicable",
        "reject",
        "jurisdiction-blocked",
    ),
    _FixtureCaseSpec(
        "restricted-jurisdiction",
        "Verified API request comes from a restricted jurisdiction.",
        "api",
        "verified",
        "low",
        250,
        "restricted",
        "trusted",
        1,
        True,
        "not_applicable",
        "reject",
        "jurisdiction-blocked",
    ),
    _FixtureCaseSpec(
        "high-risk-web",
        "Verified web customer is classified with elevated risk.",
        "web",
        "verified",
        "high",
        400,
        "us",
        "known",
        0,
        True,
        "not_applicable",
        "reject",
        "elevated-risk",
    ),
    _FixtureCaseSpec(
        "high-risk-api",
        "Verified API request crosses the elevated-risk boundary.",
        "api",
        "verified",
        "high",
        450,
        "gb",
        "trusted",
        1,
        True,
        "not_applicable",
        "reject",
        "elevated-risk",
    ),
    _FixtureCaseSpec(
        "unknown-device",
        "Verified mobile request lacks a recognized device signal.",
        "mobile",
        "verified",
        "low",
        600,
        "ca",
        "unknown",
        0,
        True,
        "not_applicable",
        "reject",
        "device-untrusted",
    ),
    _FixtureCaseSpec(
        "untrusted-device",
        "Verified web request carries an explicitly untrusted device signal.",
        "web",
        "verified",
        "low",
        650,
        "us",
        "untrusted",
        0,
        True,
        "not_applicable",
        "reject",
        "device-untrusted",
    ),
    _FixtureCaseSpec(
        "velocity-exceeded-web",
        "Verified web account exceeds the hourly request velocity.",
        "web",
        "verified",
        "low",
        700,
        "us",
        "known",
        4,
        True,
        "not_applicable",
        "reject",
        "velocity-limit",
    ),
    _FixtureCaseSpec(
        "velocity-exceeded-partner",
        "Verified partner integration exceeds the hourly request velocity.",
        "partner",
        "verified",
        "low",
        750,
        "fr",
        "trusted",
        5,
        True,
        "trusted",
        "reject",
        "velocity-limit",
    ),
    _FixtureCaseSpec(
        "amount-over-limit-web",
        "Verified web request exceeds the published amount limit by one cent.",
        "web",
        "verified",
        "low",
        100001,
        "us",
        "known",
        0,
        True,
        "not_applicable",
        "reject",
        "amount-limit",
    ),
    _FixtureCaseSpec(
        "amount-over-limit-batch",
        "Verified batch settlement substantially exceeds the amount limit.",
        "batch",
        "verified",
        "low",
        250000,
        "de",
        "trusted",
        2,
        True,
        "not_applicable",
        "reject",
        "amount-limit",
    ),
    _FixtureCaseSpec(
        "missing-consent-web",
        "Verified web customer has not supplied the required consent.",
        "web",
        "verified",
        "low",
        800,
        "us",
        "known",
        0,
        False,
        "not_applicable",
        "reject",
        "consent-required",
    ),
    _FixtureCaseSpec(
        "missing-consent-mobile",
        "Verified mobile customer withdraws consent before submission.",
        "mobile",
        "verified",
        "low",
        850,
        "ca",
        "trusted",
        1,
        False,
        "not_applicable",
        "reject",
        "consent-required",
    ),
    _FixtureCaseSpec(
        "untrusted-partner",
        "Verified partner integration is present but not trusted.",
        "partner",
        "verified",
        "low",
        900,
        "fr",
        "known",
        0,
        True,
        "blocked",
        "reject",
        "partner-untrusted",
    ),
    _FixtureCaseSpec(
        "pending-partner-review",
        "Verified partner integration is awaiting trust review.",
        "partner",
        "verified",
        "low",
        950,
        "fr",
        "trusted",
        1,
        True,
        "pending",
        "reject",
        "partner-untrusted",
    ),
    _FixtureCaseSpec(
        "high-risk-restricted",
        "Verified high-risk request also originates in a restricted jurisdiction.",
        "web",
        "verified",
        "high",
        275,
        "restricted",
        "known",
        0,
        True,
        "not_applicable",
        "reject",
        "jurisdiction-blocked",
    ),
    _FixtureCaseSpec(
        "unknown-device-high-risk",
        "Verified mobile request combines an unknown device with elevated risk.",
        "mobile",
        "verified",
        "high",
        500,
        "au",
        "unknown",
        1,
        True,
        "not_applicable",
        "reject",
        "elevated-risk",
    ),
)


# Each non-default primary obligation below is backed by a distinct catalog
# boundary.  The mapping keeps the integration asset honest: every required
# category has real primary cases instead of being declared merely to satisfy
# the fixed category list.
_FIXTURE_OBLIGATION_BY_INDEX = {
    0: "partition-output",
    4: "near-boundary",
    5: "field-boundary",
    6: "near-boundary",
    9: "near-boundary",
    10: "partition-output",
    11: "conditional-branch",
    14: "partition-output",
    15: "conditional-branch",
    16: "partition-output",
    18: "untrusted-device-adversarial",
    19: "untrusted-device-adversarial",
    20: "partition-output",
    22: "partition-output",
    23: "near-boundary",
    24: "field-boundary",
    25: "field-boundary",
    26: "partition-output",
    27: "conditional-branch",
    28: "precedence-conflict",
}


def _git(repo: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssertionError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def _git_commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _establish_managed_worktree_ignore(repo: Path) -> None:
    """Model the user's pre-run ignore setup without changing project files."""

    exclude = repo / ".git" / "info" / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".worktrees/" in existing.splitlines():
        return
    separator = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(existing + separator + ".worktrees/\n", encoding="utf-8")


def _purge_fixture_modules() -> None:
    """Prevent a prior temporary target package from leaking into a new test."""

    for name in tuple(sys.modules):
        if name == "target_app" or name.startswith("target_app."):
            sys.modules.pop(name, None)


def _initial_contract(repo: Path, prompt_id: str) -> Path:
    path = repo / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "prompt_path: prompts/classify.md\n"
        "schema: target_app.production:Decision\n"
        "renderer: target_app.production:assemble_call\n"
        "purpose: deterministic request classification\n",
        encoding="utf-8",
    )
    return path


def _fixture_input(spec: _FixtureCaseSpec, split: str) -> dict[str, dict[str, object]]:
    """Render one catalog boundary as meaningful production input fields."""

    split_history_offset = {
        "dev": 0,
        "validation": 7,
        "acceptance": 14,
        "external": 21,
    }.get(split, 28)
    return {
        "variables": {
            "split": split,
            "boundary": spec.boundary,
            "message": f"{split.title()} evidence: {spec.message}",
            "channel": spec.channel,
            "account_status": spec.account_status,
            "risk_level": spec.risk_level,
            "amount_cents": spec.amount_cents,
            "jurisdiction": spec.jurisdiction,
            "device_trust": spec.device_trust,
            "velocity": spec.velocity,
            "consent": spec.consent,
            "partner_status": spec.partner_status,
            # Customer history is a real routing signal.  The split offset
            # gives each partition a distinct, deterministic population while
            # the catalog still determines the business boundary.
            "customer_history_days": (
                30 + spec.amount_cents % 17 + split_history_offset
            ),
        },
        "context": {
            "split": split,
            "boundary": spec.boundary,
            "catalog": "routing-business-boundaries",
        },
    }


def _fixture_decision(case_input: object) -> tuple[str, str]:
    """Derive the fixture decision from business attributes, never the case ID."""

    if not isinstance(case_input, Mapping):
        raise AssertionError("fixture input must be a mapping")
    variables = case_input.get("variables")
    if not isinstance(variables, Mapping):
        raise AssertionError("fixture input variables must be a mapping")

    account_status = variables.get("account_status")
    jurisdiction = variables.get("jurisdiction")
    risk_level = variables.get("risk_level")
    device_trust = variables.get("device_trust")
    velocity = variables.get("velocity")
    amount_cents = variables.get("amount_cents")
    consent = variables.get("consent")
    channel = variables.get("channel")
    partner_status = variables.get("partner_status")

    if account_status != "verified":
        return "reject", "account-not-eligible"
    if jurisdiction in {"sanctioned", "restricted"}:
        return "reject", "jurisdiction-blocked"
    if risk_level == "high":
        return "reject", "elevated-risk"
    if device_trust not in {"known", "trusted"}:
        return "reject", "device-untrusted"
    if isinstance(velocity, bool) or not isinstance(velocity, int):
        raise AssertionError("fixture velocity must be an integer")
    if velocity > 3:
        return "reject", "velocity-limit"
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
        raise AssertionError("fixture amount_cents must be an integer")
    if amount_cents > 100000:
        return "reject", "amount-limit"
    if consent is not True:
        return "reject", "consent-required"
    if channel == "partner" and partner_status != "trusted":
        return "reject", "partner-untrusted"
    return "accept", "verified-low-risk"


def make_case(
    split: str,
    index: int,
    *,
    obligation: str = "classify-input",
    variant: str = "normal",
) -> dict[str, object]:
    """Build one deterministic case from the substantive routing catalog."""

    if not isinstance(split, str) or not split.strip():
        raise ValueError("split must be a non-empty string")
    if not isinstance(index, int) or index < 0:
        raise ValueError("index must be a non-negative integer")
    try:
        spec = _FIXTURE_CASE_CATALOG[index]
    except IndexError as error:
        raise ValueError(
            f"fixture catalog index must be below {len(_FIXTURE_CASE_CATALOG)}"
        ) from error

    case_id = f"{split}-{index:03d}"
    family = f"routing-{split}-{spec.boundary}"
    condition_id = f"{split}-{spec.boundary}"
    case_input = _fixture_input(spec, split)
    action, reason = _fixture_decision(case_input)
    expected = (spec.expected_action, spec.expected_reason)
    if (action, reason) != expected:
        raise AssertionError(
            f"catalog decision disagrees with business rule for {spec.boundary}: "
            f"{(action, reason)!r} != {expected!r}"
        )
    return {
        "id": case_id,
        "semantic_family": family,
        "source": ["target_app/production.py"],
        "input": case_input,
        "expect": {"output": {"action": action, "reason": reason}},
        "priority": "normal",
        "dimensions": ["routing", spec.channel, spec.expected_reason],
        "rationale": spec.message,
        "coverage": {
            "primary_obligation": obligation,
            "secondary_obligations": [],
            "variant": variant,
            "condition_id": condition_id,
            "distinction": None,
        },
    }


def _case_sets() -> dict[str, list[dict[str, object]]]:
    variants = {
        "dev": "normal",
        "validation": "boundary",
        "acceptance": "natural_variation",
    }
    return {
        split: [
            make_case(
                split,
                index,
                obligation=_FIXTURE_OBLIGATION_BY_INDEX.get(
                    index, "classify-input"
                ),
                variant=variant,
            )
            for index in range(30)
        ]
        for split, variant in variants.items()
    } | {"external": [make_case("external", 0)]}


def remove_last_case(path: Path) -> None:
    """Remove exactly one case while preserving the YAML asset contract."""

    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise AssertionError(f"case asset is not a non-empty list: {path}")
    path.write_text(yaml.safe_dump(cases[:-1], sort_keys=False), encoding="utf-8")


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _adapter_source() -> str:
    return (
        "from __future__ import annotations\n"
        "import sys\n"
        "from pathlib import Path\n"
        "_repo = Path(__file__).resolve().parents[2]\n"
        "if str(_repo) not in sys.path:\n"
        "    sys.path.insert(0, str(_repo))\n"
        "from target_app.production import assemble_call\n"
        "\n"
        "def prepare_call(prompt_path, case):\n"
        "    return assemble_call(prompt_path, case)\n"
    )


def _write_complete_assets(repo: Path, prompt_id: str) -> Path:
    eval_root = repo / ".prompt-evals" / prompt_id
    eval_root.mkdir(parents=True, exist_ok=True)
    _initial_contract(repo, prompt_id)
    _write_yaml(
        eval_root / "eval-config.yaml",
        {
            "repeats": {"development": 5, "validation": 5, "acceptance": 10},
            "thresholds": {"normal": {"development": 4, "acceptance": 9}},
        },
    )
    required_categories = {
        "normal_path",
        "output_partition",
        "near_boundary",
        "field_boundary",
        "conditional_branch",
        "conflict",
        "adversarial",
    }
    category_evidence = [
        "target_app/production.py",
        "repository-tests",
    ]
    _write_yaml(
        eval_root / "coverage-obligations.yaml",
        {
            "version": 1,
            "categories": [
                {
                    "category": category,
                    "applicability": (
                        "required"
                        if category in required_categories
                        else "not_applicable"
                    ),
                    "evidence_checked": category_evidence,
                    "rationale": (
                        None
                        if category in required_categories
                        else (
                            "The fixture has no evidenced ambiguity, irrelevant "
                            "input, explicit fallback, or confirmed historical "
                            "regression behavior."
                        )
                    ),
                }
                for category in (
                    "normal_path",
                    "output_partition",
                    "near_boundary",
                    "field_boundary",
                    "conditional_branch",
                    "conflict",
                    "ambiguity",
                    "irrelevant_input",
                    "fallback",
                    "historical_regression",
                    "adversarial",
                )
            ],
            "obligations": [
                {
                    "id": "classify-input",
                    "source": ["target_app/production.py"],
                    "category": "normal_path",
                    "risk": "normal",
                    "rule": "return the fixture routing decision",
                    "required_splits": {
                        "dev": ["normal"],
                        "validation": ["boundary"],
                        "acceptance": ["natural_variation"],
                    },
                    "variant_exclusions": {},
                },
                *[
                    {
                        "id": obligation_id,
                        "source": ["target_app/production.py"],
                        "category": category,
                        "risk": "normal",
                        "rule": f"exercise the evidenced {category} routing boundary",
                        "required_splits": {
                            "dev": ["normal"],
                            "validation": ["boundary"],
                            "acceptance": ["natural_variation"],
                        },
                        "variant_exclusions": {},
                    }
                    for category, obligation_id in (
                        ("output_partition", "partition-output"),
                        ("near_boundary", "near-boundary"),
                        ("field_boundary", "field-boundary"),
                        ("conditional_branch", "conditional-branch"),
                        ("conflict", "precedence-conflict"),
                        ("adversarial", "untrusted-device-adversarial"),
                    )
                ],
            ],
        },
    )
    for split, cases in _case_sets().items():
        filename = "external-cases.yaml" if split == "external" else f"{split}-cases.yaml"
        _write_yaml(eval_root / filename, cases)
    (eval_root / "adapter.py").write_text(_adapter_source(), encoding="utf-8")
    _write_yaml(eval_root / "optimization-history.yaml", {"cycles": []})
    return eval_root


_FIXTURE_ASSET_NAMES = (
    "prompt-contract.yaml",
    "eval-config.yaml",
    "dev-cases.yaml",
    "validation-cases.yaml",
    "acceptance-cases.yaml",
    "coverage-obligations.yaml",
    "adapter.py",
    "optimization-history.yaml",
)


def _prepare_fixture_assets(
    original_repo: Path, worktree: Path, prompt_id: str
) -> Path:
    """Copy an existing proposed asset or create the deterministic fixture.

    Copying the current original-workspace asset is intentional: integration
    tests use it to model a user-edited coverage asset and verify that stale
    confirmation state fails closed before any transport call.  A normal
    tune fixture starts without case assets, so it gets the same complete
    deterministic set directly in the isolated worktree.
    """

    source_root = Path(original_repo) / ".prompt-evals" / prompt_id
    destination_root = Path(worktree) / ".prompt-evals" / prompt_id
    required = (
        "dev-cases.yaml",
        "validation-cases.yaml",
        "acceptance-cases.yaml",
        "coverage-obligations.yaml",
    )
    if all((source_root / name).is_file() for name in required):
        destination_root.mkdir(parents=True, exist_ok=True)
        for name in _FIXTURE_ASSET_NAMES:
            source = source_root / name
            if source.is_file():
                shutil.copy2(source, destination_root / name)
        return destination_root
    return _write_complete_assets(worktree, prompt_id)


def _git_commit_if_changed(repo: Path, message: str) -> None:
    """Commit fixture assets only when this cycle actually changed them."""

    status = _git(repo, "status", "--porcelain", "--untracked-files=all").strip()
    if status:
        _git_commit(repo, message)


def build_target_repo(path: Path, *, complete_assets: bool = False) -> Path:
    """Copy and commit the minimal target fixture into a disposable repository."""

    target = Path(path).resolve()
    if target.exists():
        raise AssertionError(f"target fixture destination already exists: {target}")
    shutil.copytree(FIXTURE_SOURCE, target)
    prompt_id = prompt_id_for_path(PROMPT_RELATIVE)
    _initial_contract(target, prompt_id)
    _git(target, "init")
    _establish_managed_worktree_ignore(target)
    _git(target, "config", "user.email", "integration-tests@example.invalid")
    _git(target, "config", "user.name", "Offline Integration Tests")
    _git_commit(target, "fixture: create production prompt target")
    if complete_assets:
        _write_complete_assets(target, prompt_id)
        _git_commit(target, "fixture: add confirmed evaluation assets")
    return target


def _prompt_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_message(message: object) -> tuple[str, str, dict[str, object]]:
    content = getattr(message, "content", "")
    text = content if isinstance(content, str) else str(content)
    case_match = re.search(r"^case_id=([^\r\n]+)", text, re.MULTILINE)
    hash_match = re.search(r"^prompt_hash=([^\r\n]+)", text, re.MULTILINE)
    input_match = re.search(r"^input=([^\r\n]+)", text, re.MULTILINE)
    if case_match is None or hash_match is None or input_match is None:
        raise AssertionError(f"production renderer metadata is missing: {text!r}")
    try:
        case_input = json.loads(input_match.group(1))
    except json.JSONDecodeError as error:
        raise AssertionError(f"production renderer input is invalid: {text!r}") from error
    if not isinstance(case_input, dict):
        raise AssertionError("production renderer input must be a mapping")
    return case_match.group(1), hash_match.group(1), case_input


class CountingTransport:
    """Deterministic ChatOpenAI-shaped fake injected only by tests."""

    def __init__(
        self,
        *,
        scenario: str = "happy",
        original_hash: str | None = None,
        candidate_hash: str | None = None,
        sentinel: str | None = None,
    ) -> None:
        self.scenario = scenario
        self.original_hash = original_hash
        self.candidate_hash = candidate_hash
        self.sentinel = sentinel
        self.call_count = 0
        self.calls: list[dict[str, str | int]] = []
        self.lifecycle_events: list[str] = []
        self.raw_evidence: list[AIMessage] = []
        self._schema: type[Any] | None = None
        self._per_case = Counter[tuple[str, str]]()
        self._resume_failed = False
        self.structured_output_kwargs: list[dict[str, object]] = []
        self.confirmed_hashes: tuple[str, str] | None = None
        self.confirmed_confirmation: ConfirmationRecord | None = None
        self.loader_events: list[tuple[str | None, tuple[str, ...]]] = []
        self.model_name = MODEL_NAME
        self.expected_by_case: dict[str, tuple[str, str]] = {}
        # This identity is deliberately test-only.  It must not be confused
        # with evidence from the fixed production client (covered by Task 4
        # and the Task 10 behavior-forward checks).
        self.transport_identity = "test-only-counting-transport"

    def mark(self, event: str) -> None:
        """Record test-only lifecycle evidence without changing production APIs."""

        self.lifecycle_events.append(event)

    def record_case_loader(self, paths: object) -> None:
        """Record the one complete-suite loader boundary during bootstrap."""

        if not isinstance(paths, (tuple, list)):
            raise AssertionError("case loader paths must be a sequence")
        names = tuple(Path(path).name for path in paths)
        stage = self.lifecycle_events[-1] if self.lifecycle_events else None
        self.loader_events.append((stage, names))

    def bind_prompt_hashes(self, original_hash: str, candidate_hash: str) -> None:
        self.original_hash = original_hash
        self.candidate_hash = candidate_hash

    def bind_expected_cases(self, suite: CaseSuite) -> None:
        """Use the production-validated expected object for each case."""

        self.expected_by_case = {
            validated.case.id: (
                str(validated.expected.action),
                str(validated.expected.reason),
            )
            for validated in suite.all_cases
        }

    def with_structured_output(self, schema: type[Any], **kwargs: object) -> "CountingTransport":
        self._schema = schema
        self.structured_output_kwargs.append(dict(kwargs))
        return self

    @staticmethod
    def _expected(case_input: object) -> tuple[str, str]:
        return _fixture_decision(case_input)

    def _is_wrong(
        self, prompt_hash: str, case_input: object, expected: tuple[str, str]
    ) -> bool:
        if self.scenario == "no-change":
            return False
        variables = case_input.get("variables") if isinstance(case_input, Mapping) else None
        if not isinstance(variables, Mapping):
            raise AssertionError("fixture input variables must be a mapping")
        split = variables.get("split")
        boundary = variables.get("boundary")
        if (
            prompt_hash == self.original_hash
            and split in {"dev", "validation"}
            and expected[0] == "accept"
        ):
            return True
        if self.scenario == "regression":
            return (
                prompt_hash == self.candidate_hash
                and split == "validation"
                and boundary == "known-web-low-risk"
            )
        if self.scenario == "acceptance-failure":
            return (
                prompt_hash == self.candidate_hash
                and split == "acceptance"
                and boundary == "known-web-low-risk"
            )
        return False

    def invoke(self, messages: object) -> object:
        if self._schema is None:
            raise AssertionError("structured schema was not configured")
        if not isinstance(messages, (list, tuple)) or not messages:
            raise AssertionError("production call did not provide messages")
        case_id, prompt_hash, case_input = _parse_message(messages[0])
        index = self._per_case[(prompt_hash, case_id)]
        self._per_case[(prompt_hash, case_id)] += 1
        self.call_count += 1
        self.calls.append({"prompt_hash": prompt_hash, "case_id": case_id, "repeat": index})
        self.mark(f"call:{case_id}")
        if (
            self.scenario == "resume"
            and not self._resume_failed
            and prompt_hash == self.original_hash
            and isinstance(case_input.get("variables"), Mapping)
            and case_input["variables"].get("boundary") == "closed-account"
        ):
            self._resume_failed = True
            raise ConnectionError("offline fixture transport interruption")

        action, reason = self._expected(case_input)
        declared = self.expected_by_case.get(case_id)
        if declared is not None and declared != (action, reason):
            raise AssertionError(
                f"fixture transport decision disagrees with case {case_id}: "
                f"{(action, reason)!r} != {declared!r}"
            )
        if self._is_wrong(prompt_hash, case_input, (action, reason)):
            action, reason = ("reject", "fixture-business-regression")
        payload = {"action": action, "reason": reason}
        raw_content = "fixture structured response"
        if self.sentinel:
            raw_content += f"\nAuthorization: Bearer {self.sentinel}"
        raw = AIMessage(
            content=raw_content,
            tool_calls=[
                {
                    "name": self._schema.__name__,
                    "args": payload,
                    "id": f"fixture-call-{self.call_count}",
                }
            ],
        )
        self.raw_evidence.append(raw)
        parsed = self._schema.model_validate(payload)
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


@dataclass(frozen=True)
class TuneResult:
    stop_reason: str
    original_prompt: str
    baseline_dev: RunManifest | None = None
    baseline_validation: RunManifest | None = None
    coverage_obligations_hash: str | None = None
    case_suite_hash: str | None = None
    confirmation_hashes: tuple[str, str] | None = None
    confirmation_record: ConfirmationRecord | None = None
    lifecycle_events: tuple[str, ...] = ()
    evidence_checked: tuple[str, ...] = ()
    saturation_statement: str | None = None
    near_duplicate_review_status: str | None = None
    requires_user_review: bool = False
    slot_estimates: dict[str, int] = field(default_factory=dict)
    candidate_prompt: str | None = None
    original_workspace_status: dict[str, str] | None = None
    acceptance_activities: int = 0
    acceptance_baseline_perfect: bool = False
    acceptance_candidate_perfect: bool = False
    frozen_candidate_hash: str | None = None
    delivered_prompt_hash: str | None = None
    delivered_paths: tuple[str, ...] = ()
    partially_delivered_paths: tuple[str, ...] = ()
    transport_calls: int = 0
    transport_retry_slots: tuple[str, ...] = ()
    resumed_slot_key: str | None = None
    completed_slot_keys: tuple[str, ...] = ()
    slot_call_counts: dict[str, int] = field(default_factory=dict)
    slot_attempts: dict[str, int] = field(default_factory=dict)
    raw_evidence: object = None

    def to_dict(self) -> dict[str, object]:
        return {
            "stop_reason": self.stop_reason,
            "original_prompt": self.original_prompt,
            "coverage_obligations_hash": self.coverage_obligations_hash,
            "case_suite_hash": self.case_suite_hash,
            "confirmation_hashes": list(self.confirmation_hashes or ()),
            "confirmation_record": (
                self.confirmation_record.to_dict()
                if self.confirmation_record is not None
                else None
            ),
            "lifecycle_events": list(self.lifecycle_events),
            "evidence_checked": list(self.evidence_checked),
            "saturation_statement": self.saturation_statement,
            "near_duplicate_review_status": self.near_duplicate_review_status,
            "requires_user_review": self.requires_user_review,
            "slot_estimates": dict(self.slot_estimates),
            "candidate_prompt": self.candidate_prompt,
            "original_workspace_status": self.original_workspace_status,
            "acceptance_activities": self.acceptance_activities,
            "acceptance_baseline_perfect": self.acceptance_baseline_perfect,
            "acceptance_candidate_perfect": self.acceptance_candidate_perfect,
            "frozen_candidate_hash": self.frozen_candidate_hash,
            "delivered_prompt_hash": self.delivered_prompt_hash,
            "delivered_paths": list(self.delivered_paths),
            "partially_delivered_paths": list(self.partially_delivered_paths),
            "transport_calls": self.transport_calls,
            "transport_retry_slots": list(self.transport_retry_slots),
            "resumed_slot_key": self.resumed_slot_key,
            "completed_slot_keys": list(self.completed_slot_keys),
            "slot_call_counts": dict(self.slot_call_counts),
            "slot_attempts": dict(self.slot_attempts),
            "raw_evidence": runner_module._redacted(self.raw_evidence),
        }

    @property
    def confirmation(self) -> ConfirmationRecord | None:
        """Compatibility alias for callers that call the record confirmation."""

        return self.confirmation_record


def _report_evidence(transport: CountingTransport) -> object:
    """Keep harness results at the same redacted boundary as reports."""

    return runner_module._redacted(transport.raw_evidence)


def _coerce_confirmation(value: object) -> ConfirmationRecord:
    """Accept the immutable record or its serialized mapping form."""

    if isinstance(value, ConfirmationRecord):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("confirmation must be a ConfirmationRecord or mapping")
    evidence = value.get("evidence_checked")
    if isinstance(evidence, list):
        evidence = tuple(evidence)
    return ConfirmationRecord(
        coverage_obligations_hash=value["coverage_obligations_hash"],  # type: ignore[arg-type]
        case_suite_hash=value["case_suite_hash"],  # type: ignore[arg-type]
        evidence_checked=evidence,  # type: ignore[arg-type]
        saturation_statement=value["saturation_statement"],  # type: ignore[arg-type]
        near_duplicate_review_status=value["near_duplicate_review_status"],  # type: ignore[arg-type]
    )


def _eval_root(repo: Path) -> Path:
    return repo / ".prompt-evals" / prompt_id_for_path(PROMPT_RELATIVE)


def workspace_snapshot(repo: Path) -> tuple[dict[str, bytes], str]:
    """Capture key workspace bytes and Git status for delivery assertions.

    Runtime bytecode/cache directories are intentionally excluded because they
    are ignored implementation artifacts, not assets that a tune cycle may
    deliver.  Retained linked checkouts under the managed project-local
    ``.worktrees/stabilizing-prompts/`` subtree are excluded for the same reason.  The status
    component still catches staged, unstaged, and untracked changes to all
    other tracked/visible assets.
    """

    root = Path(repo).resolve()
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if (
            not path.is_file()
            or ".git" in relative_path.parts
            or "__pycache__" in relative_path.parts
            or relative_path.parts[:2] == (".worktrees", "stabilizing-prompts")
        ):
            continue
        relative = relative_path.as_posix()
        files[relative] = path.read_bytes()
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    return files, status


def _slot_evidence(
    manifest: RunManifest,
    transport: CountingTransport,
    *,
    call_start: int = 0,
) -> tuple[dict[str, int], dict[str, int]]:
    """Return observed calls and persisted attempts for each baseline slot."""

    call_counts: dict[str, int] = {}
    attempts: dict[str, int] = {}
    for slot in manifest.slots:
        result = manifest.results.get(slot.key)
        if result is not None:
            # The renderer intentionally does not receive a repeat index, so
            # the transport's per-case sequence cannot distinguish a retry of
            # slot ``repeat_index=0`` from the next planned repeat.  The
            # production runner's persisted ``attempts`` is the authoritative
            # per-slot count; cross-check its sum against all observed calls
            # for this prompt/case plan to retain transport-level evidence.
            call_counts[slot.key] = result.attempts
            attempts[slot.key] = result.attempts
    expected_calls = sum(
        call_counts.values()
    )
    observed_calls = sum(
        call.get("prompt_hash") == manifest.prompt_hash
        and call.get("case_id") in {slot.case_id for slot in manifest.slots}
        for call in transport.calls[call_start:]
    )
    if expected_calls != observed_calls:
        raise AssertionError(
            f"baseline slot attempt evidence disagrees with transport calls: "
            f"expected {expected_calls}, observed {observed_calls}"
        )
    return call_counts, attempts


def _load_fixture_assets(
    repo: Path,
    *,
    loader_observer: Any | None = None,
) -> tuple[Path, Any, CaseSuite, type[Any]]:
    eval_root = _eval_root(repo)
    _purge_fixture_modules()
    adapter = load_adapter(eval_root)
    raw = yaml.safe_load((eval_root / "dev-cases.yaml").read_text(encoding="utf-8"))
    first_case = EvalCase.model_validate(raw[0])
    call = adapter(repo / PROMPT_RELATIVE, first_case)
    schema = call["schema"]
    obligations = load_coverage_obligations(
        eval_root / "coverage-obligations.yaml", repo
    )
    case_paths = tuple(
        eval_root / f"{split}-cases.yaml"
        for split in ("dev", "validation", "acceptance")
    )
    if loader_observer is not None:
        loader_observer(case_paths)
    suite = load_case_suite(
        case_paths,
        schema,
        obligations=obligations,
    )
    return eval_root, adapter, suite, schema


def _validate_fixture_assets(
    repo: Path, transport: CountingTransport
) -> tuple[Path, Any, CaseSuite, type[Any]]:
    """Run the real mechanical CASE_SUITE_JSON boundary before transport."""

    eval_root = _eval_root(repo)
    output_path = eval_root / ".runtime" / "CASE_SUITE_JSON"
    output = StringIO()
    repo_text = str(Path(repo).resolve())
    _purge_fixture_modules()
    added_repo = repo_text not in sys.path
    if added_repo:
        sys.path.insert(0, repo_text)
    transport.mark("mechanical-validation")
    try:
        with redirect_stdout(output):
            code = cases_module.main(
                [
                    "--eval-root",
                    str(eval_root),
                    "--schema",
                    "target_app.production:Decision",
                    "--output",
                    str(output_path),
                ]
            )
    finally:
        if added_repo:
            try:
                sys.path.remove(repo_text)
            except ValueError:
                pass
    if code != 0:
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CaseSetupError(
                f"mechanical coverage validation failed with exit code {code}"
            ) from error
        detail = payload.get("error") if isinstance(payload, dict) else None
        raise CaseSetupError(str(detail or "mechanical coverage validation failed"))
    return _load_fixture_assets(repo, loader_observer=transport.record_case_loader)


def _run_phase(
    *,
    eval_root: Path,
    adapter: Any,
    suite: CaseSuite,
    schema: type[Any],
    split: Literal["dev", "validation", "acceptance"],
    prompt_path: Path,
    canonical_prompt: Path,
    cycle: WorktreeCycle,
    transport: CountingTransport,
    label: str,
    resume: bool = False,
    prepare_cache: dict[tuple[str, str], Any] | None = None,
) -> tuple[RunManifest, RunMetrics, str | None, tuple[str, ...]]:
    repeats = 10 if split == "acceptance" else 5
    cases = suite[split]
    transport.mark(f"phase:{label}")
    prompt_hash = _prompt_hash(prompt_path)
    manifest_path = eval_root / ".runtime" / f"{label}.json"
    call_cache = prepare_cache if prepare_cache is not None else {}

    def cached_prepare_call(path: Path, case: object) -> Any:
        case_id = getattr(case, "id", None)
        if not isinstance(case_id, str):
            raise AssertionError("fixture case is missing its string id")
        key = (str(Path(path).resolve()), case_id)
        if key not in call_cache:
            call_cache[key] = adapter(path, case)
        return call_cache[key]

    # Plan and execute in memory so the 30-case fixture does not rewrite a
    # large JSON manifest after every slot.  Persist the final manifest once,
    # retaining the production lifecycle artifact without the I/O blowup.
    manifest = new_manifest(
        cases,
        repeats,
        prompt_hash,
        prompt_path=prompt_path,
        schema=schema,
        manifest_path=None,
        dataset=split,
        mode="tune",
        cycle_id=cycle.branch,
        client_config=safe_client_config(),
    )
    result = execute_run(
        manifest,
        prompt_path=prompt_path,
        prepare_call=cached_prepare_call,
        client=transport,
        manifest_path=None,
    )
    resumed_slot: str | None = None
    retry_slots: tuple[str, ...] = ()
    if resume and pending_slots(result):
        resumed_slot = pending_slots(result)[0].key
        retry_slots = (resumed_slot,)
        result = execute_run(
            result,
            prompt_path=prompt_path,
            prepare_call=cached_prepare_call,
            client=transport,
            manifest_path=None,
        )
    if result.status != "complete" or result.metrics is None:
        raise AssertionError(f"fixture phase did not complete: {result.status}")
    # Candidate files are runtime-only, but comparisons use the canonical
    # production Prompt path.  Keep the content hash as the only Prompt
    # identity difference, matching the manifest contract.
    if result.prompt_path != str(canonical_prompt.resolve()):
        result = replace(result, prompt_path=str(canonical_prompt.resolve()))
    result = replace(result, manifest_path=manifest_path)
    persist_manifest(result, manifest_path)
    metrics = score_run(result, schema)
    return result, metrics, resumed_slot, retry_slots


def _all_pass(metrics: RunMetrics) -> bool:
    return (
        metrics.schema_valid_rate == 1
        and metrics.run_accuracy == 1
        and metrics.stable_case_rate == 1
        and all(case.pass_count == case.total_responses for case in metrics.case_scores)
    )


def _status_for_path(repo: Path, relative: str) -> str:
    return _git(repo, "status", "--short", "--", relative).rstrip()


def assert_delivered_files_unstaged_or_untracked(repo: Path, paths: tuple[str, ...]) -> None:
    """Ruling 2: inspect each delivered file, never Git's compact dir label."""

    for relative in paths:
        tracked = bool(
            _git(repo, "ls-files", "--error-unmatch", "--", relative, check=False).strip()
        )
        status = _status_for_path(repo, relative)
        assert status, f"delivered path has no Git status: {relative}"
        if tracked:
            assert status.startswith(" M"), f"tracked delivery was staged: {relative}: {status!r}"
        else:
            assert status.startswith("??"), f"new delivery is not untracked: {relative}: {status!r}"


def _finish_success_delivery(
    cycle: WorktreeCycle,
    prompt_id: str,
    candidate_prompt: str,
    candidate_hash: str,
) -> tuple[tuple[str, ...], str]:
    worktree_prompt = cycle.worktree / PROMPT_RELATIVE
    worktree_prompt.write_text(candidate_prompt, encoding="utf-8")
    contract = cycle.worktree / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
    contract.write_text(
        contract.read_text(encoding="utf-8")
        + f"current_prompt_hash: {candidate_hash}\n",
        encoding="utf-8",
    )
    _git_commit(cycle.worktree, "tune: commit accepted candidate")
    patch = build_delivery_patch(cycle, SUCCESS_ALLOWLIST, result="success")
    apply_delivery_patch(cycle, patch)
    delivered_hash = _prompt_hash(cycle.original_repo / PROMPT_RELATIVE)
    return patch.paths, delivered_hash


def _finish_failure_delivery(
    cycle: WorktreeCycle,
    prompt_id: str,
) -> tuple[str, ...]:
    history = cycle.worktree / ".prompt-evals" / prompt_id / "optimization-history.yaml"
    history.write_text(
        "cycles:\n"
        "  - stop_reason: acceptance_failed\n"
        "    case: acceptance-000\n",
        encoding="utf-8",
    )
    _git_commit(cycle.worktree, "tune: commit failure history and confirmed assets")
    patch = build_delivery_patch(cycle, FAILURE_ALLOWLIST, result="failure")
    apply_delivery_patch(cycle, patch)
    return patch.paths


def run_tune_with_fake_transport(
    target_repo: Path,
    *,
    scenario: str = "happy",
    confirm_contract: bool = True,
    confirm_delivery: bool = True,
    confirm_failure_delivery: bool = False,
    confirm_near_duplicate_review: bool = True,
    confirmation_hashes: tuple[str, str] | None = None,
    confirmation: ConfirmationRecord | Mapping[str, object] | None = None,
    confirmation_record: ConfirmationRecord | Mapping[str, object] | None = None,
    evidence_checked: tuple[str, ...] | list[str] = DEFAULT_EVIDENCE_CHECKED,
    saturation_statement: str = DEFAULT_SATURATION_STATEMENT,
    near_duplicate_review_status: str | None = None,
    transport: CountingTransport | None = None,
    project_transport_setting: str | None = None,
    sentinel: str | None = None,
) -> TuneResult:
    """Exercise the tune state machine with only test-injected model calls."""

    repo = Path(target_repo).resolve()
    original_prompt_path = repo / PROMPT_RELATIVE
    original_prompt = original_prompt_path.read_text(encoding="utf-8")
    prompt_id = prompt_id_for_path(PROMPT_RELATIVE)
    fake = transport or CountingTransport(scenario=scenario, sentinel=sentinel)
    prepare_cache: dict[tuple[str, str], Any] = {}
    baseline_dev: RunManifest | None = None
    baseline_validation: RunManifest | None = None
    obligations_hash: str | None = None
    suite_hash: str | None = None
    suite: CaseSuite | None = None
    slot_estimates: dict[str, int] = {}
    confirmed_hashes: tuple[str, str] | None = None
    confirmed_confirmation: ConfirmationRecord | None = None
    current_evidence_checked = (
        tuple(evidence_checked) if isinstance(evidence_checked, (tuple, list)) else ()
    )
    current_saturation_statement = (
        saturation_statement if isinstance(saturation_statement, str) else ""
    )
    current_near_duplicate_review_status = near_duplicate_review_status

    def _result(stop_reason: str, **values: object) -> TuneResult:
        defaults: dict[str, object] = {
            "baseline_dev": baseline_dev,
            "baseline_validation": baseline_validation,
            "coverage_obligations_hash": obligations_hash,
            "case_suite_hash": suite_hash,
            "confirmation_hashes": confirmed_hashes,
            "confirmation_record": confirmed_confirmation,
            "lifecycle_events": tuple(fake.lifecycle_events),
            "evidence_checked": current_evidence_checked,
            "saturation_statement": current_saturation_statement,
            "near_duplicate_review_status": current_near_duplicate_review_status,
            "requires_user_review": (
                bool(suite.coverage_audit.near_duplicates) if suite is not None else False
            ),
            "slot_estimates": slot_estimates,
            "original_prompt": original_prompt,
        }
        defaults.update(values)
        return TuneResult(stop_reason=stop_reason, **defaults)

    fake.mark("preflight")

    try:
        snapshot = validate_workspace(repo, original_prompt_path, (repo / DEPENDENCY_RELATIVE,))
    except WorkspaceError as error:
        reason = "dependency_dirty" if "dependency" in str(error).casefold() else "preflight_failed"
        return _result(
            reason,
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    if not confirm_contract:
        fake.mark("confirmation-rejected")
        return _result(
            "contract_not_confirmed",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )

    fake.mark("worktree")
    state_path = repo.parent / f".{repo.name}-{prompt_id}.cycle.json"
    cycle = create_cycle(
        repo,
        prompt_id,
        prompt_path=snapshot.prompt_path,
        state_path=state_path,
    )
    eval_root = _prepare_fixture_assets(repo, cycle.worktree, prompt_id)
    if project_transport_setting is not None:
        # Persist the selector in the disposable project's real config.  The
        # production runner never interprets this field; only the explicit
        # test ``client`` argument below can install CountingTransport.
        config_path = eval_root / "eval-config.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise AssertionError("fixture eval-config.yaml must contain a mapping")
        config["transport"] = project_transport_setting
        _write_yaml(config_path, config)

    try:
        eval_root, adapter, suite, schema = _validate_fixture_assets(
            cycle.worktree, fake
        )
    except (CaseSetupError, OSError, ValueError, TypeError):
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    fake.bind_expected_cases(suite)
    obligations_hash = coverage_obligations_hash(
        eval_root / "coverage-obligations.yaml"
    )
    suite_hash = dataset_hash(suite)
    dev_count = len(suite.dev)
    validation_count = len(suite.validation)
    acceptance_count = len(suite.acceptance)
    slot_estimates = {
        "baseline_slots": 5 * dev_count + 5 * validation_count,
        "one_full_promoted_candidate_round": (
            5 * dev_count + 5 * validation_count + 5 * dev_count
        ),
        "affected_dev_pre_run_slots": 5 * dev_count,
        "paired_acceptance_slots": 10 * acceptance_count + 10 * acceptance_count,
    }
    fake.mark("slot-estimates")
    fake.mark("evidence-checked")
    fake.mark("saturation-statement")
    if not current_evidence_checked or any(
        not item.strip() for item in current_evidence_checked
    ):
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    if not current_saturation_statement.strip():
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )

    has_near_duplicates = bool(suite.coverage_audit.near_duplicates)
    derived_review_status = (
        NEAR_DUPLICATE_REVIEW_CONFIRMED
        if has_near_duplicates and confirm_near_duplicate_review
        else NEAR_DUPLICATE_REVIEW_REJECTED
        if has_near_duplicates
        else NEAR_DUPLICATE_REVIEW_NOT_REQUIRED
    )
    if near_duplicate_review_status is None:
        current_near_duplicate_review_status = derived_review_status
    else:
        current_near_duplicate_review_status = near_duplicate_review_status
    if current_near_duplicate_review_status not in {
        NEAR_DUPLICATE_REVIEW_NOT_REQUIRED,
        NEAR_DUPLICATE_REVIEW_CONFIRMED,
        NEAR_DUPLICATE_REVIEW_REJECTED,
    }:
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    if (
        has_near_duplicates
        and (
            not confirm_near_duplicate_review
            or current_near_duplicate_review_status != NEAR_DUPLICATE_REVIEW_CONFIRMED
        )
    ):
        fake.mark("near-duplicate-review-rejected")
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    if not has_near_duplicates and (
        current_near_duplicate_review_status != NEAR_DUPLICATE_REVIEW_NOT_REQUIRED
    ):
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )

    current_hashes = (obligations_hash, suite_hash)
    current_confirmation = ConfirmationRecord(
        coverage_obligations_hash=obligations_hash,
        case_suite_hash=suite_hash,
        evidence_checked=current_evidence_checked,
        saturation_statement=current_saturation_statement,
        near_duplicate_review_status=current_near_duplicate_review_status,
    )
    supplied_confirmation: ConfirmationRecord | None = None
    try:
        if confirmation is not None and confirmation_record is not None:
            raise TypeError("pass only one confirmation object")
        if confirmation is not None:
            supplied_confirmation = _coerce_confirmation(confirmation)
        elif confirmation_record is not None:
            supplied_confirmation = _coerce_confirmation(confirmation_record)
        elif confirmation_hashes is not None:
            hashes = tuple(confirmation_hashes)
            if len(hashes) != 2:
                raise TypeError("confirmation_hashes must contain two hashes")
            previous = fake.confirmed_confirmation or ConfirmationRecord(
                coverage_obligations_hash=hashes[0],
                case_suite_hash=hashes[1],
                evidence_checked=DEFAULT_EVIDENCE_CHECKED,
                saturation_statement=DEFAULT_SATURATION_STATEMENT,
                near_duplicate_review_status=NEAR_DUPLICATE_REVIEW_NOT_REQUIRED,
            )
            supplied_confirmation = replace(
                previous,
                coverage_obligations_hash=hashes[0],
                case_suite_hash=hashes[1],
            )
        elif fake.confirmed_confirmation is not None:
            supplied_confirmation = fake.confirmed_confirmation
    except (KeyError, TypeError, ValueError):
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )

    if supplied_confirmation is not None and supplied_confirmation != current_confirmation:
        fake.mark("confirmation-invalidated")
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    fake.mark("confirmation")
    fake.confirmed_hashes = current_hashes
    confirmed_hashes = current_hashes
    fake.confirmed_confirmation = current_confirmation
    confirmed_confirmation = current_confirmation
    try:
        fake.mark("probe")
        probe_model(fake)
    except Exception:
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    _git_commit_if_changed(cycle.worktree, "tune: commit confirmed evaluation assets")
    fake.mark("asset-commit")

    candidate_prompt = (
        "Classify the request as accept or reject.\n"
        "Return the production decision with an explicit reason.\n"
    )
    candidate_path = eval_root / ".runtime" / "candidate.md"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(candidate_prompt, encoding="utf-8")
    candidate_hash = _prompt_hash(candidate_path)
    # Git may normalize the fixture's line endings while checking out the
    # isolated cycle worktree.  The transport keys baseline behavior to the
    # bytes actually rendered by that production checkout, not to the
    # pre-checkout source bytes in the original fixture repository.
    fake.bind_prompt_hashes(_prompt_hash(cycle.worktree / PROMPT_RELATIVE), candidate_hash)

    baseline_call_start = len(fake.calls)
    baseline_dev, baseline_dev_metrics, resumed_slot, retry_slots = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="dev",
        prompt_path=cycle.worktree / PROMPT_RELATIVE,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="baseline-dev",
        resume=scenario == "resume",
        prepare_cache=prepare_cache,
    )
    baseline_validation, baseline_validation_metrics, _, _ = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="validation",
        prompt_path=cycle.worktree / PROMPT_RELATIVE,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="baseline-validation",
        prepare_cache=prepare_cache,
    )
    baseline_slot_call_counts, baseline_slot_attempts = _slot_evidence(
        baseline_dev, fake, call_start=baseline_call_start
    )

    if scenario == "no-change" and _all_pass(baseline_dev_metrics) and _all_pass(baseline_validation_metrics):
        return _result(
            "no_change_needed",
            transport_calls=fake.call_count,
            resumed_slot_key=resumed_slot,
            completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
            slot_call_counts=baseline_slot_call_counts,
            slot_attempts=baseline_slot_attempts,
            transport_retry_slots=retry_slots,
            raw_evidence=_report_evidence(fake),
        )

    candidate_dev, candidate_dev_metrics, _, _ = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="dev",
        prompt_path=candidate_path,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="candidate-dev",
        prepare_cache=prepare_cache,
    )
    dev_comparison = compare_module.compare_runs(
        baseline_dev,
        candidate_dev,
        "development",
        schema=schema,
    )
    if not compare_module.evaluate_gate(dev_comparison, "development").passed:
        return _result(
            "development_failed",
            candidate_prompt=candidate_prompt,
            frozen_candidate_hash=candidate_hash,
            transport_calls=fake.call_count,
            resumed_slot_key=resumed_slot,
            completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
            slot_call_counts=baseline_slot_call_counts,
            slot_attempts=baseline_slot_attempts,
            transport_retry_slots=retry_slots,
            raw_evidence=_report_evidence(fake),
        )

    candidate_validation, candidate_validation_metrics, _, _ = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="validation",
        prompt_path=candidate_path,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="candidate-validation",
        prepare_cache=prepare_cache,
    )
    del candidate_dev_metrics, candidate_validation_metrics
    validation_comparison = compare_module.compare_runs(
        baseline_validation,
        candidate_validation,
        "validation",
        schema=schema,
    )
    validation_gate = compare_module.evaluate_gate(validation_comparison, "validation")
    if not validation_gate.passed:
        return _result(
            "validation_failed",
            candidate_prompt=candidate_prompt,
            frozen_candidate_hash=candidate_hash,
            transport_calls=fake.call_count,
            resumed_slot_key=resumed_slot,
            completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
            slot_call_counts=baseline_slot_call_counts,
            slot_attempts=baseline_slot_attempts,
            transport_retry_slots=retry_slots,
            raw_evidence=_report_evidence(fake),
        )

    fake.mark("candidate-freeze")
    acceptance_activities = 1
    acceptance_baseline, acceptance_baseline_metrics, _, _ = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="acceptance",
        prompt_path=cycle.worktree / PROMPT_RELATIVE,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="acceptance-baseline",
        prepare_cache=prepare_cache,
    )
    acceptance_candidate, acceptance_candidate_metrics, _, _ = _run_phase(
        eval_root=eval_root,
        adapter=adapter,
        suite=suite,
        schema=schema,
        split="acceptance",
        prompt_path=candidate_path,
        canonical_prompt=cycle.worktree / PROMPT_RELATIVE,
        cycle=cycle,
        transport=fake,
        label="acceptance-candidate",
        prepare_cache=prepare_cache,
    )
    acceptance_comparison = compare_module.compare_runs(
        acceptance_baseline,
        acceptance_candidate,
        "acceptance",
        schema=schema,
    )
    acceptance_gate = compare_module.evaluate_gate(acceptance_comparison, "acceptance")
    if not acceptance_gate.passed:
        delivered_paths: tuple[str, ...] = ()
        if confirm_failure_delivery:
            delivered_paths = _finish_failure_delivery(cycle, prompt_id)
        return _result(
            "acceptance_failed",
            candidate_prompt=candidate_prompt,
            frozen_candidate_hash=candidate_hash,
            acceptance_activities=acceptance_activities,
            acceptance_baseline_perfect=_all_pass(acceptance_baseline_metrics),
            acceptance_candidate_perfect=_all_pass(acceptance_candidate_metrics),
            delivered_paths=delivered_paths,
            delivered_prompt_hash=None,
            transport_calls=fake.call_count,
            resumed_slot_key=resumed_slot,
            completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
            slot_call_counts=baseline_slot_call_counts,
            slot_attempts=baseline_slot_attempts,
            transport_retry_slots=retry_slots,
            raw_evidence=_report_evidence(fake),
        )

    if not confirm_delivery:
        return _result(
            "delivery_not_confirmed",
            candidate_prompt=candidate_prompt,
            frozen_candidate_hash=candidate_hash,
            acceptance_activities=acceptance_activities,
            acceptance_baseline_perfect=_all_pass(acceptance_baseline_metrics),
            acceptance_candidate_perfect=_all_pass(acceptance_candidate_metrics),
            transport_calls=fake.call_count,
            resumed_slot_key=resumed_slot,
            completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
            slot_call_counts=baseline_slot_call_counts,
            slot_attempts=baseline_slot_attempts,
            transport_retry_slots=retry_slots,
            raw_evidence=_report_evidence(fake),
        )

    if scenario == "conflict":
        original_prompt_path.write_text("user edit wins\n", encoding="utf-8")
        try:
            _finish_success_delivery(cycle, prompt_id, candidate_prompt, candidate_hash)
        except DeliveryConflict:
            return _result(
                "delivery_conflict",
                candidate_prompt=candidate_prompt,
                frozen_candidate_hash=candidate_hash,
                acceptance_activities=acceptance_activities,
                acceptance_baseline_perfect=_all_pass(acceptance_baseline_metrics),
                acceptance_candidate_perfect=_all_pass(acceptance_candidate_metrics),
                transport_calls=fake.call_count,
                resumed_slot_key=resumed_slot,
                completed_slot_keys=tuple(
                    slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results
                ),
                slot_call_counts=baseline_slot_call_counts,
                slot_attempts=baseline_slot_attempts,
                transport_retry_slots=retry_slots,
                raw_evidence=_report_evidence(fake),
            )
        raise AssertionError("conflicting delivery unexpectedly succeeded")

    if scenario == "rollback":
        worktree_prompt = cycle.worktree / PROMPT_RELATIVE
        worktree_prompt.write_text(candidate_prompt, encoding="utf-8")
        contract = cycle.worktree / ".prompt-evals" / prompt_id / "prompt-contract.yaml"
        contract.write_text(
            contract.read_text(encoding="utf-8")
            + f"current_prompt_hash: {candidate_hash}\n",
            encoding="utf-8",
        )
        _git_commit(cycle.worktree, "tune: commit accepted candidate for rollback")
        patch = build_delivery_patch(cycle, SUCCESS_ALLOWLIST, result="success")
        original_assert = __import__("scripts.manage_worktree", fromlist=["_assert_worktree_snapshot"])._assert_worktree_snapshot
        calls = 0

        def fail_after_apply(current_cycle: WorktreeCycle, current_patch: object) -> None:
            nonlocal calls
            calls += 1
            original_assert(current_cycle, current_patch)
            if calls == 2:
                raise DeliveryError("post-apply verification failure")

        manage_module = __import__("scripts.manage_worktree", fromlist=["_assert_worktree_snapshot"])
        manage_module._assert_worktree_snapshot = fail_after_apply
        try:
            apply_delivery_patch(cycle, patch)
        except DeliveryError:
            return _result(
                "delivery_rollback",
                candidate_prompt=candidate_prompt,
                frozen_candidate_hash=candidate_hash,
                acceptance_activities=acceptance_activities,
                acceptance_baseline_perfect=_all_pass(acceptance_baseline_metrics),
                acceptance_candidate_perfect=_all_pass(acceptance_candidate_metrics),
                delivered_paths=patch.paths,
                partially_delivered_paths=tuple(
                    path for path in patch.paths if _status_for_path(repo, path)
                ),
                transport_calls=fake.call_count,
                resumed_slot_key=resumed_slot,
                completed_slot_keys=tuple(
                    slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results
                ),
                slot_call_counts=baseline_slot_call_counts,
                slot_attempts=baseline_slot_attempts,
                transport_retry_slots=retry_slots,
                raw_evidence=_report_evidence(fake),
            )
        finally:
            manage_module._assert_worktree_snapshot = original_assert
        raise AssertionError("rollback injection unexpectedly succeeded")

    try:
        delivered_paths, delivered_hash = _finish_success_delivery(
            cycle,
            prompt_id,
            candidate_prompt,
            candidate_hash,
        )
    except DeliveryConflict as error:
        raise AssertionError(f"unexpected delivery conflict: {error}") from error
    return _result(
        "delivered",
        candidate_prompt=candidate_prompt,
        acceptance_activities=acceptance_activities,
        acceptance_baseline_perfect=_all_pass(acceptance_baseline_metrics),
        acceptance_candidate_perfect=_all_pass(acceptance_candidate_metrics),
        frozen_candidate_hash=candidate_hash,
        delivered_prompt_hash=delivered_hash,
        delivered_paths=delivered_paths,
        transport_calls=fake.call_count,
        resumed_slot_key=resumed_slot,
        completed_slot_keys=tuple(slot.key for slot in baseline_dev.slots if slot.key in baseline_dev.results),
        slot_call_counts=baseline_slot_call_counts,
        slot_attempts=baseline_slot_attempts,
        transport_retry_slots=retry_slots,
        raw_evidence=_report_evidence(fake),
    )


@dataclass(frozen=True)
class CliChainEvidence:
    workspace_status: str
    case_suite_status: str
    run_status: str
    score_status: str
    comparison_status: str
    schema_name: str
    renderer_marker: str


def _set_cycle_id(manifest_path: Path) -> None:
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["cycle_id"] = "offline-cli-cycle"
    manifest_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_cli_chain(target_repo: Path) -> CliChainEvidence:
    """Call each documented CLI entry point with a test-injected client."""

    repo = Path(target_repo).resolve()
    prompt = repo / PROMPT_RELATIVE
    prompt_id = prompt_id_for_path(PROMPT_RELATIVE)
    eval_root = repo / ".prompt-evals" / prompt_id
    runtime = eval_root / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    _purge_fixture_modules()
    sys.path.insert(0, str(repo))
    try:
        workspace_json = runtime / "cli-workspace.json"
        workspace_code = workspace_module.main(
            [
                "--repo",
                str(repo),
                "--prompt",
                PROMPT_RELATIVE,
                "--mode",
                "verify",
                "--output",
                str(workspace_json),
            ]
        )
        case_json = runtime / "cli-cases.json"
        case_code = cases_module.main(
            [
                "--eval-root",
                str(eval_root),
                "--schema",
                "target_app.production:Decision",
                "--output",
                str(case_json),
            ]
        )
        transport = CountingTransport(scenario="no-change")
        old_build_client = runner_module.build_client
        runner_module.build_client = lambda _path=None: transport
        try:
            baseline_manifest = runtime / "cli-baseline.json"
            baseline_code = runner_module.main(
                [
                    "--mode",
                    "verify",
                    "--eval-root",
                    str(eval_root),
                    "--prompt",
                    str(prompt),
                    "--dataset",
                    "dev",
                    "--repeats",
                    "5",
                    "--manifest",
                    str(baseline_manifest),
                ]
            )
            _set_cycle_id(baseline_manifest)
            candidate_manifest = runtime / "cli-candidate.json"
            candidate_code = runner_module.main(
                [
                    "--mode",
                    "verify",
                    "--eval-root",
                    str(eval_root),
                    "--prompt",
                    str(prompt),
                    "--dataset",
                    "dev",
                    "--repeats",
                    "5",
                    "--manifest",
                    str(candidate_manifest),
                ]
            )
            _set_cycle_id(candidate_manifest)
        finally:
            runner_module.build_client = old_build_client
        baseline_report = runtime / "cli-baseline-report.json"
        candidate_report = runtime / "cli-candidate-report.json"
        baseline_score_code = __import__("scripts.score_results", fromlist=["main"]).main(
            ["--manifest", str(baseline_manifest), "--report", str(baseline_report)]
        )
        candidate_score_code = __import__("scripts.score_results", fromlist=["main"]).main(
            ["--manifest", str(candidate_manifest), "--report", str(candidate_report)]
        )
        comparison_report = runtime / "cli-comparison.json"
        comparison_code = compare_module.main(
            [
                "--baseline",
                str(baseline_manifest),
                "--candidate",
                str(candidate_manifest),
                "--phase",
                "development",
                "--report",
                str(comparison_report),
            ]
        )
    finally:
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass
    workspace_payload = json.loads(workspace_json.read_text(encoding="utf-8"))
    case_payload = json.loads(case_json.read_text(encoding="utf-8"))
    baseline_payload = json.loads(baseline_manifest.read_text(encoding="utf-8"))
    comparison_payload = json.loads(comparison_report.read_text(encoding="utf-8"))
    return CliChainEvidence(
        workspace_status="valid" if workspace_code == 0 and workspace_payload["status"] == "valid" else "error",
        case_suite_status="valid" if case_code == 0 and case_payload["status"] == "valid" else "error",
        run_status="complete" if baseline_code == 0 and candidate_code == 0 and baseline_payload["status"] == "complete" else "error",
        score_status="complete" if baseline_score_code == 0 and candidate_score_code == 0 else "error",
        comparison_status="passed" if comparison_code == 0 and comparison_payload["status"] == "passed" else "failed",
        schema_name=str(baseline_payload.get("schema_import", "")).rsplit(":", 1)[-1],
        renderer_marker="production-renderer" if any(
            call.get("case_id") == "dev-000" for call in transport.calls
        ) else "missing",
    )


def run_verify(
    target_repo: Path,
    *,
    dataset: Literal["dev", "validation", "external", "acceptance"],
    transport: CountingTransport,
) -> RunManifest:
    """Run the real verify CLI, with acceptance rejected before file access."""

    repo = Path(target_repo).resolve()
    prompt_id = prompt_id_for_path(PROMPT_RELATIVE)
    eval_root = repo / ".prompt-evals" / prompt_id
    manifest_path = eval_root / ".runtime" / f"verify-{dataset}.json"
    if dataset == "acceptance":
        # Invoke the production CLI entry point.  Its guard must run before
        # touching this file, importing the adapter, or constructing a client.
        code = runner_module.main(
            [
                "--mode",
                "verify",
                "--eval-root",
                str(repo / ".prompt-evals" / "missing-eval-root"),
                "--prompt",
                str(repo / PROMPT_RELATIVE),
                "--dataset",
                "acceptance",
                "--repeats",
                "10",
                "--manifest",
                str(manifest_path),
            ]
        )
        if code == 2:
            raise runner_module.UsageError(
                "verify mode cannot use the acceptance dataset"
            )
        raise AssertionError(f"verify acceptance guard returned {code}")

    _purge_fixture_modules()
    sys.path.insert(0, str(repo))
    old_build_client = runner_module.build_client
    runner_module.build_client = lambda _path=None: transport
    try:
        code = runner_module.main(
            [
                "--mode",
                "verify",
                "--eval-root",
                str(eval_root),
                "--prompt",
                str(repo / PROMPT_RELATIVE),
                "--dataset",
                dataset,
                "--repeats",
                "5",
                "--manifest",
                str(manifest_path),
            ]
        )
    finally:
        runner_module.build_client = old_build_client
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass
    if code != 0:
        raise AssertionError(f"verify CLI failed with exit code {code}")
    return load_manifest(manifest_path)


__all__ = [
    "CliChainEvidence",
    "ConfirmationRecord",
    "CoverageConfirmation",
    "CountingTransport",
    "TuneResult",
    "assert_delivered_files_unstaged_or_untracked",
    "build_target_repo",
    "make_case",
    "remove_last_case",
    "run_cli_chain",
    "run_tune_with_fake_transport",
    "run_verify",
]
