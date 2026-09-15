"""Test-only orchestration and transport boundary for offline integration tests.

The production scripts intentionally have no fake-transport configuration.  This
module injects a ChatOpenAI-shaped client through the public ``client`` argument
or a temporary monkeypatch around the runner CLI, and keeps all target-repository
files in disposable Git fixtures.
"""

from __future__ import annotations

from collections import Counter
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


def make_case(
    split: str,
    index: int,
    *,
    obligation: str = "classify-input",
    variant: str = "normal",
) -> dict[str, object]:
    """Build one deterministic, substantively distinct integration case.

    The index is part of the semantic family, input, expected object, and
    coverage condition.  This keeps the fixture useful for duplicate and
    cross-split leakage checks rather than padding the split with wording-only
    copies.  Even indexes model the accepting partition and odd indexes model
    the rejecting partition used by ``CountingTransport``.
    """

    if not isinstance(split, str) or not split.strip():
        raise ValueError("split must be a non-empty string")
    if not isinstance(index, int) or index < 0:
        raise ValueError("index must be a non-negative integer")
    case_id = f"{split}-{index:03d}"
    family = f"routing-{split}-condition-{index:03d}"
    condition_id = f"{split}-condition-{index:03d}"
    action: Literal["accept", "reject"] = "accept" if index % 2 == 0 else "reject"
    return {
        "id": case_id,
        "semantic_family": family,
        "source": ["target_app/production.py"],
        "input": {
            "variables": {
                "split": split,
                "condition": condition_id,
                "message": f"{split} evidenced condition {index:03d}",
            },
            "context": {"index": index, "family": family},
        },
        "expect": {
            "output": {
                "action": action,
                "reason": f"fixture-{case_id}",
            }
        },
        "priority": "normal",
        "dimensions": ["routing", "deterministic-fixture", f"condition-{index:03d}"],
        "rationale": (
            f"the fixture contract fixes the {split} decision for "
            f"evidenced condition {index:03d}"
        ),
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
            make_case(split, index, variant=variant)
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
    _write_yaml(
        eval_root / "coverage-obligations.yaml",
        {
            "version": 1,
            "categories": [
                {
                    "category": category,
                    "applicability": "required",
                    "evidence_checked": [],
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
                }
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


def _parse_message(message: object) -> tuple[str, str]:
    content = getattr(message, "content", "")
    text = content if isinstance(content, str) else str(content)
    case_match = re.search(r"^case_id=([^\r\n]+)", text, re.MULTILINE)
    hash_match = re.search(r"^prompt_hash=([^\r\n]+)", text, re.MULTILINE)
    if case_match is None or hash_match is None:
        raise AssertionError(f"production renderer metadata is missing: {text!r}")
    return case_match.group(1), hash_match.group(1)


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
        self.model_name = MODEL_NAME
        self.expected_by_case: dict[str, tuple[str, str]] = {}
        # This identity is deliberately test-only.  It must not be confused
        # with evidence from the fixed production client (covered by Task 4
        # and the Task 10 behavior-forward checks).
        self.transport_identity = "test-only-counting-transport"

    def mark(self, event: str) -> None:
        """Record test-only lifecycle evidence without changing production APIs."""

        self.lifecycle_events.append(event)

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
    def _fallback_expected(case_id: str) -> tuple[str, str]:
        match = re.search(r"-(\d+)$", case_id)
        if match is not None:
            action = "accept" if int(match.group(1)) % 2 == 0 else "reject"
        else:
            action = "reject" if case_id.endswith("reject") else "accept"
        return action, f"fixture-{case_id}"

    def _expected(self, case_id: str) -> tuple[str, str]:
        return self.expected_by_case.get(case_id, self._fallback_expected(case_id))

    def _is_wrong(self, prompt_hash: str, case_id: str) -> bool:
        if self.scenario == "no-change":
            return False
        match = re.search(r"-(\d+)$", case_id)
        accepting_case = (
            int(match.group(1)) % 2 == 0 if match is not None else case_id.endswith("accept")
        )
        if (
            prompt_hash == self.original_hash
            and case_id.startswith(("dev-", "validation-"))
            and accepting_case
        ):
            return True
        if self.scenario == "regression":
            return prompt_hash == self.candidate_hash and case_id == "validation-000"
        if self.scenario == "acceptance-failure":
            return prompt_hash == self.candidate_hash and case_id == "acceptance-000"
        return False

    def invoke(self, messages: object) -> object:
        if self._schema is None:
            raise AssertionError("structured schema was not configured")
        if not isinstance(messages, (list, tuple)) or not messages:
            raise AssertionError("production call did not provide messages")
        case_id, prompt_hash = _parse_message(messages[0])
        index = self._per_case[(prompt_hash, case_id)]
        self._per_case[(prompt_hash, case_id)] += 1
        self.call_count += 1
        self.calls.append({"prompt_hash": prompt_hash, "case_id": case_id, "repeat": index})
        self.mark(f"call:{case_id}")
        if (
            self.scenario == "resume"
            and not self._resume_failed
            and prompt_hash == self.original_hash
            and case_id == "dev-001"
        ):
            self._resume_failed = True
            raise ConnectionError("offline fixture transport interruption")

        action, reason = self._expected(case_id)
        if self._is_wrong(prompt_hash, case_id):
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
    lifecycle_events: tuple[str, ...] = ()
    evidence_checked: tuple[str, ...] = ()
    saturation_statement: str | None = None
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
            "lifecycle_events": list(self.lifecycle_events),
            "evidence_checked": list(self.evidence_checked),
            "saturation_statement": self.saturation_statement,
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


def _report_evidence(transport: CountingTransport) -> object:
    """Keep harness results at the same redacted boundary as reports."""

    return runner_module._redacted(transport.raw_evidence)


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
    suite = load_case_suite(
        tuple(eval_root / f"{split}-cases.yaml" for split in ("dev", "validation", "acceptance")),
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
    return _load_fixture_assets(repo)


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
    saturation_statement: str = (
        "Scanned the production Schema, renderer, business contract, and fixture "
        "history; no additional evidence-backed boundaries remain."
    ),
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
    evidence_checked = (
        "target_app/production.py",
        "prompts/classify.md",
        "references/business-contract.md",
        "repository-tests",
    )

    def _result(stop_reason: str, **values: object) -> TuneResult:
        defaults: dict[str, object] = {
            "baseline_dev": baseline_dev,
            "baseline_validation": baseline_validation,
            "coverage_obligations_hash": obligations_hash,
            "case_suite_hash": suite_hash,
            "confirmation_hashes": confirmed_hashes,
            "lifecycle_events": tuple(fake.lifecycle_events),
            "evidence_checked": evidence_checked,
            "saturation_statement": saturation_statement,
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
    cycle = create_cycle(
        repo,
        prompt_id,
        prompt_path=snapshot.prompt_path,
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
    if not saturation_statement.strip():
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )

    current_hashes = (obligations_hash, suite_hash)
    supplied_hashes = confirmation_hashes
    if supplied_hashes is None:
        supplied_hashes = fake.confirmed_hashes
    if supplied_hashes is not None and tuple(supplied_hashes) != current_hashes:
        fake.mark("confirmation-invalidated")
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    if suite.coverage_audit.near_duplicates and not confirm_near_duplicate_review:
        fake.mark("near-duplicate-review-rejected")
        return _result(
            "setup_error",
            transport_calls=fake.call_count,
            raw_evidence=_report_evidence(fake),
        )
    fake.mark("confirmation")
    fake.confirmed_hashes = current_hashes
    confirmed_hashes = current_hashes
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
