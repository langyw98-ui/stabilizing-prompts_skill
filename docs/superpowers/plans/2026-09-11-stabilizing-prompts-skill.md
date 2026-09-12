# Stabilizing Prompts Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a personal Codex Skill that evaluates and minimally tunes one repository-backed Markdown prompt against deterministic Pydantic expectations using a fixed local model.

**Architecture:** The repository root is the installable Skill root. Small Python scripts own workspace validation, case validation, the fixed model client, resumable execution, scoring, comparison, and worktree delivery; `SKILL.md` orchestrates those scripts and enforces the user gates. Project-specific behavior stays behind `.prompt-evals/<prompt-id>/adapter.py`.

**Tech Stack:** Codex Skills, Python 3.14.6, Pydantic 2.13.4, `langchain-openai`, `langchain-core`, OpenAI SDK, PyYAML, pytest, Git worktrees, PowerShell/RTK.

**Spec:** `docs/superpowers/specs/2026-09-11-stabilizing-prompts-skill-design.md`

## Global Constraints

- Run every Python command through `conda run -n kds python`; do not discover or switch to `.venv`, uv, Poetry, or another environment.
- The fixed model is `dbirks/Qwen3.8-27B-W4A16-AutoRound` at `http://192.168.168.230:8000/v1`.
- Use `temperature=0.0`, `max_retries=2`, a 30-second timeout, and `extra_body={"enable_thinking": False, "enable_reasoning": False, "enable_search": False}`; do not send other sampling parameters.
- Never emit the Authorization Token or Authorization header in commands, exceptions, logs, manifests, reports, fixtures, or target repositories.
- Store the only real credential, when locally configured by the user, at the Skill-root path `.local/model-credentials.json`; the root `.gitignore` must exclude exactly that file, and installation must not copy it implicitly.
- The credential JSON must contain exactly one non-empty string field, `authorization_token`. Missing, unreadable, malformed, empty, or extra-field credentials are non-scoring `setup_error` conditions.
- Endpoint, model, timeout, retries, and every generation setting are fixed in tracked client code and cannot be overridden by project configuration; tests use temporary sentinel credential paths only.
- Tune exactly one repository-relative `.md` prompt and reuse the production renderer, message assembly, and Pydantic Schema.
- Only `tune` may read and run `acceptance-cases.yaml`, once per cycle after the candidate hash is frozen; `verify` must never read it.
- Only validation selection requires a strict metric improvement. Acceptance requires thresholds and no regression, not strict improvement.
- Candidate prompt files remain under `.runtime/`; replace the production prompt only after acceptance passes and the user confirms delivery.
- `cycle_base_commit` is the original workspace `HEAD` when the worktree is created. Every delivery patch starts there and is filtered through the delivery allowlist.
- Task 7 uses the approved local single-user, non-adversarial threat model: protect accidental edits, concurrent/stale cycle state, path mistakes, unexpected files, apply failures, and hash mismatches; do not add repository locks, cryptographic trust, or a custom complete patch parser to resist a local actor who can rewrite tracked code, Git state, manifests, patches, or hashes. Delivery regenerates the patch from trusted cycle state at delivery time.
- Use TDD for every Python behavior and make one focused commit after each task.

## File Map

- `SKILL.md`: trigger conditions, `tune`/`verify` workflow, user gates, stop conditions, and resource routing.
- `agents/openai.yaml`: Skill display metadata and default prompt.
- `scripts/local_model_client.py`: fixed client construction, connection probe, safe configuration report, and secret redaction.
- `scripts/validate_workspace.py`: Git boundary checks, `kds` validation, prompt identity, dirty dependency checks, and prompt ID generation.
- `scripts/validate_cases.py`: YAML models, uniqueness/leakage checks, production Schema validation, and dataset hashing.
- `scripts/run_prompt_eval.py`: adapter loading, immutable manifests, fixed slots, retries/resume, invocation, classification, and raw-result persistence.
- `scripts/score_results.py`: exact canonical declared-field comparison, field diffs, and per-run metrics.
- `scripts/compare_runs.py`: manifest compatibility, regression counts, and phase-specific gates.
- `scripts/manage_worktree.py`: isolated cycle creation, allowlisted patch generation, preflight, application, rollback, and hash verification.
- `references/business-contract.md`: required contract fields and evidence rules.
- `references/case-schema.md`: dataset schema and partition rules.
- `references/adapter-contract.md`: exact project adapter interface.
- `references/evaluation-method.md`: slot, classification, metric, and gate definitions.
- `references/worktree-lifecycle.md`: cycle and delivery state machine.
- `tests/`: unit and integration coverage; fake transport is reachable only from tests.

---

### Task 1: Installable Skill Skeleton and Contract Tests

**Files:**
- Create: `SKILL.md`
- Create: `agents/openai.yaml`
- Create: `references/business-contract.md`
- Create: `references/case-schema.md`
- Create: `references/adapter-contract.md`
- Create: `references/evaluation-method.md`
- Create: `references/worktree-lifecycle.md`
- Create: `tests/test_skill_structure.py`

**Interfaces:**
- Consumes: the approved design spec.
- Produces: a valid `stabilizing-prompts` Skill root and stable reference filenames used by later tasks.

- [ ] **Step 1: Write the failing structure test**

```python
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_required_skill_files_exist():
    expected = {
        "SKILL.md",
        "agents/openai.yaml",
        "references/business-contract.md",
        "references/case-schema.md",
        "references/adapter-contract.md",
        "references/evaluation-method.md",
        "references/worktree-lifecycle.md",
    }
    assert {str(path.relative_to(ROOT)).replace("\\", "/") for path in ROOT.rglob("*") if path.is_file()} >= expected


def test_skill_frontmatter_and_modes():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: stabilizing-prompts\n")
    assert "## tune" in text
    assert "## verify" in text
    assert "acceptance-cases.yaml" in text
```

- [ ] **Step 2: Run the test and confirm the missing files fail**

Run: `rtk conda run -n kds python -m pytest tests/test_skill_structure.py -v`

Expected: FAIL because `SKILL.md` and supporting resources do not exist.

- [ ] **Step 3: Create the Skill shell and reference contracts**

Start `SKILL.md` with:

```markdown
---
name: stabilizing-prompts
description: Use when a Python repository-backed Markdown prompt with Pydantic structured output is inconsistent across repeated model calls or needs a deterministic local-model regression suite.
---

# Stabilizing Prompts

Operate on one repository-relative `.md` prompt. Use only `conda run -n kds python` for Python execution. Stop when the target is outside the supported scope.

## tune

Create one isolated cycle, confirm the business contract and all cases before any model call, build a baseline, generate minimal candidates, run acceptance once, and deliver only after user confirmation.

## verify

Run development, validation, or separately supplied non-acceptance cases without changing canonical assets. Never read or run `acceptance-cases.yaml`.
```

Populate each reference with the exact corresponding rules from spec sections 7–15. Create `agents/openai.yaml` with display name `Stabilizing Prompts`, a concise description, and a default prompt that requires a repository-relative Markdown prompt path.

- [ ] **Step 4: Validate structure**

Run: `rtk conda run -n kds python -m pytest tests/test_skill_structure.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the skeleton**

Run:

```powershell
rtk git add -- SKILL.md agents references tests/test_skill_structure.py
rtk git commit -m "feat: scaffold stabilizing prompts skill"
```

### Task 2: Workspace Validation and Stable Prompt Identity

**Files:**
- Create: `scripts/validate_workspace.py`
- Create: `tests/test_validate_workspace.py`

**Interfaces:**
- Consumes: repository root and one prompt path.
- Produces: `WorkspaceSnapshot`, `prompt_id_for_path(path: str) -> str`, and `validate_workspace(repo_root: Path, prompt_path: Path, dependency_paths: Sequence[Path]) -> WorkspaceSnapshot`.

- [ ] **Step 1: Write failing tests for identity and safety checks**

```python
def test_prompt_id_uses_canonical_path_and_hash():
    value = prompt_id_for_path("src/agent-a/prompts/classify.md")
    assert value.startswith("src--agent-a--prompts--classify--")
    assert len(value.rsplit("--", 1)[1]) == 12


def test_rejects_prompt_outside_repo(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    outside = tmp_path / "outside.md"
    outside.write_text("prompt", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="outside repository"):
        validate_workspace(repo, outside, ())


def test_rejects_dirty_prompt_but_allows_unrelated_dirty_file(repo_with_prompt):
    repo, prompt = repo_with_prompt
    (repo / "notes.txt").write_text("dirty", encoding="utf-8")
    assert validate_workspace(repo, prompt, ()).prompt_path == "prompts/classify.md"
    prompt.write_text("dirty prompt", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="must match HEAD"):
        validate_workspace(repo, prompt, ())
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_validate_workspace.py -v`

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement the workspace model and validation**

```python
@dataclass(frozen=True)
class WorkspaceSnapshot:
    repo_root: Path
    head_commit: str
    prompt_path: str
    prompt_hash: str
    prompt_id: str
    python_command: tuple[str, ...] = ("conda", "run", "-n", "kds", "python")
    python_version: str = sys.version.split()[0]


def prompt_id_for_path(path: str) -> str:
    canonical = PurePosixPath(path).as_posix()
    slug = re.sub(r"[^a-zA-Z0-9]+", "--", canonical.removesuffix(".md")).strip("-").lower()
    suffix = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"{slug}--{suffix}"
```

Use `git rev-parse`, `git ls-files`, and `git diff --quiet -- <paths>` without modifying the repository. Reject a non-`.md` target, multiple targets, missing Git tracking, prompt/dependency dirtiness, a recorded prompt-contract path mismatch, or an environment other than `kds`.

- [ ] **Step 4: Run workspace tests**

Run: `rtk conda run -n kds python -m pytest tests/test_validate_workspace.py -v`

Expected: PASS.

- [ ] **Step 5: Commit workspace validation**

```powershell
rtk git add -- scripts/validate_workspace.py tests/test_validate_workspace.py
rtk git commit -m "feat: validate prompt evaluation workspaces"
```

### Task 3: Case Files and Production Schema Validation

**Files:**
- Create: `scripts/validate_cases.py`
- Create: `tests/test_validate_cases.py`

**Interfaces:**
- Consumes: three YAML paths and a production `type[pydantic.BaseModel]`.
- Produces: `EvalCase`, `CaseSuite`, `load_case_suite(paths, schema) -> CaseSuite`, and `dataset_hash(suite) -> str`.

- [ ] **Step 1: Write failing validation tests**

```python
class Decision(BaseModel):
    action: Literal["accept", "reject"]
    reason: str


def test_requires_complete_schema_object(tmp_path):
    paths = write_case_sets(tmp_path, dev_expect={"action": "accept"})
    with pytest.raises(CaseSetupError, match="reason"):
        load_case_suite(paths, Decision)


def test_rejects_duplicate_ids_and_cross_split_family_leakage(tmp_path):
    paths = write_duplicate_and_leaking_case_sets(tmp_path)
    with pytest.raises(CaseSetupError):
        load_case_suite(paths, Decision)
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_validate_cases.py -v`

Expected: FAIL because case validation is missing.

- [ ] **Step 3: Implement strict YAML models and hashing**

```python
class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    semantic_family: str
    source: list[str]
    input: dict[str, dict[str, object]]
    expect: dict[str, dict[str, object]]
    priority: Literal["normal", "critical"] = "normal"
    dimensions: list[str]
    rationale: str


@dataclass(frozen=True)
class ValidatedCase:
    case: EvalCase
    expected: BaseModel
```

Validate every `expect.output` with `schema.model_validate`, require every declared Schema field in the YAML object, enforce global case-ID uniqueness, and reject repeated or trivially normalized semantic families across splits. Hash canonical JSON with sorted keys.

- [ ] **Step 4: Run case tests**

Run: `rtk conda run -n kds python -m pytest tests/test_validate_cases.py -v`

Expected: PASS.

- [ ] **Step 5: Commit case validation**

```powershell
rtk git add -- scripts/validate_cases.py tests/test_validate_cases.py
rtk git commit -m "feat: validate prompt evaluation cases"
```

### Task 4: Fixed Local Model Client and Secret Safety

**Files:**
- Create: `scripts/local_model_client.py`
- Create: `tests/test_local_model_client.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: a user-created Skill-root `.local/model-credentials.json` file, or a temporary test credential path; the real credential is never tracked.
- Produces: `build_client() -> ChatOpenAI`, `probe_model(client) -> ModelProbe`, `safe_client_config() -> dict[str, object]`, and `redact_secret(value: object) -> object`.

- [ ] **Step 1: Write failing configuration and redaction tests**

```python
def test_fixed_client_configuration(monkeypatch):
    captured = {}
    monkeypatch.setattr(module, "ChatOpenAI", lambda **kwargs: captured.update(kwargs) or object())
    module.build_client(credentials_path=temporary_credentials_path)
    assert captured["base_url"] == "http://192.168.168.230:8000/v1"
    assert captured["model"] == "dbirks/Qwen3.8-27B-W4A16-AutoRound"
    assert captured["temperature"] == 0.0
    assert captured["timeout"] == 30
    assert captured["max_retries"] == 2
    assert captured["extra_body"] == {"enable_thinking": False, "enable_reasoning": False, "enable_search": False}
    assert "top_p" not in captured and "seed" not in captured


def test_safe_config_and_exception_text_never_contain_token():
    sentinel = "unit-test-secret"
    assert sentinel not in json.dumps(module.safe_client_config())
    assert sentinel not in module.safe_error(RuntimeError(f"Authorization: Bearer {sentinel}"))
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_local_model_client.py -v`

Expected: FAIL because the client module does not exist.

- [ ] **Step 3: Implement the immutable client configuration**

```python
BASE_URL = "http://192.168.168.230:8000/v1"
MODEL_NAME = "dbirks/Qwen3.8-27B-W4A16-AutoRound"
EXTRA_BODY = {"enable_thinking": False, "enable_reasoning": False, "enable_search": False}


def build_client(credentials_path: Path | None = None) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=_authorization_token(credentials_path),
        base_url=BASE_URL,
        model=MODEL_NAME,
        temperature=0.0,
        timeout=30,
        max_retries=2,
        extra_body=EXTRA_BODY,
    )
```

`_authorization_token()` reads only the Skill-root `.local/model-credentials.json`
by default (tests pass a temporary path). The JSON object must contain exactly
one non-empty string field, `authorization_token`; missing, unreadable,
malformed, empty, or extra-field files raise non-scoring `setup_error` without
including file contents. Add the exact `.local/model-credentials.json` entry to
the root `.gitignore`. Keep the real token out of fixtures, tests, reports,
manifests, diffs, and installation packages; installation never copies the
credential file. `probe_model` must require a non-empty returned model identity
equal to `MODEL_NAME`.

- [ ] **Step 4: Run client tests and scan tracked output for the test sentinel**

Run:

```powershell
rtk conda run -n kds python -m pytest tests/test_local_model_client.py -v
rtk rg -n "unit-test-secret" .
```

Expected: tests PASS; inspect every match and confirm it is a literal in this plan or the test, never generated output.

- [ ] **Step 5: Commit the client without printing the credential**

```powershell
rtk git add -- .gitignore scripts/local_model_client.py tests/test_local_model_client.py docs/superpowers/specs/2026-09-11-stabilizing-prompts-skill-design.md docs/superpowers/plans/2026-09-11-stabilizing-prompts-skill.md
rtk git commit -m "feat: add fixed local model client"
```

### Task 5: Resumable Evaluation Runner and Error Classification

**Files:**
- Create: `scripts/run_prompt_eval.py`
- Create: `tests/test_run_prompt_eval.py`

**Interfaces:**
- Consumes: `prepare_call(prompt_path: Path, case: EvalCase) -> dict`, validated cases, prompt hash, fixed client, and manifest path.
- Produces: `RunManifest`, `CallSlot`, `SlotResult`, `record_slot_result(...) -> RunManifest`, `execute_run(...) -> RunManifest`, and resumable JSON/YAML artifacts under `.runtime/` and `reports/`. Persist parsed values as dictionaries plus the Schema import reference; never serialize live Pydantic objects into the manifest.

- [ ] **Step 1: Write failing slot and classification tests**

```python
def test_slots_are_stable_and_resume_only_incomplete(tmp_path):
    manifest = new_manifest(cases=[case("a"), case("b")], repeats=2, prompt_hash="abc")
    assert [slot.key for slot in manifest.slots] == ["a:0:abc", "a:1:abc", "b:0:abc", "b:1:abc"]
    manifest = record_slot_result(manifest, "a:0:abc", passing_result())
    assert [slot.key for slot in pending_slots(manifest)] == ["a:1:abc", "b:0:abc", "b:1:abc"]


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_retryable_status_becomes_incomplete(status):
    assert classify_exception(fake_status_error(status)).kind == "transport_error"


def test_non_retryable_sdk_exception_is_setup_error():
    assert classify_exception(fake_status_error(401)).kind == "setup_error"


def test_invalid_include_raw_shape_is_protocol_error():
    assert classify_response(None).kind == "protocol_error"
    assert classify_response({"raw": object()}).kind == "protocol_error"
```

- [ ] **Step 2: Run runner tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_run_prompt_eval.py -v`

Expected: FAIL because the runner is missing.

- [ ] **Step 3: Implement adapter loading, immutable slots, atomic persistence, and classification**

```python
@dataclass(frozen=True)
class CallSlot:
    case_id: str
    repeat_index: int
    prompt_hash: str

    @property
    def key(self) -> str:
        return f"{self.case_id}:{self.repeat_index}:{self.prompt_hash}"


def classify_response(value: object) -> ClassifiedResult:
    if not isinstance(value, dict) or set(("raw", "parsed", "parsing_error")) - value.keys():
        return ClassifiedResult("protocol_error", "invalid_include_raw_contract")
    if not isinstance(value["raw"], AIMessage):
        return ClassifiedResult("protocol_error", "missing_raw_message")
    if value["parsed"] is not None:
        return ClassifiedResult("parsed", value["parsed"])
    return classify_parsing_failure(value["raw"], value["parsing_error"])
```

Write each slot result atomically via a same-directory temporary file and `Path.replace`. Preserve raw `AIMessage` content but pass every serialization and exception through credential redaction. Never calculate final metrics while any slot is incomplete.

- [ ] **Step 4: Run runner tests**

Run: `rtk conda run -n kds python -m pytest tests/test_run_prompt_eval.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the runner**

```powershell
rtk git add -- scripts/run_prompt_eval.py tests/test_run_prompt_eval.py
rtk git commit -m "feat: run resumable prompt evaluations"
```

### Task 6: Exact Scoring, Comparison, and Phase Gates

**Files:**
- Create: `scripts/score_results.py`
- Create: `scripts/compare_runs.py`
- Create: `tests/test_score_results.py`
- Create: `tests/test_compare_runs.py`

**Interfaces:**
- Consumes: completed `RunManifest` objects, their recorded Schema import reference, and the production Pydantic Schema.
- Produces: `RunMetrics`, `CaseScore`, `Comparison`, `score_run(manifest, schema)`, `compare_runs(baseline, candidate, phase)`, and `evaluate_gate(comparison, phase)`.

Exact equality and field diffs cover only production Pydantic Schema declared
fields represented by canonical serialization with field names. `PrivateAttr`,
caches, and other runtime-only state are not persisted, scored, or included in
diffs; online runner classification and persisted scoring must share this
semantics.

- [ ] **Step 1: Write failing scoring tests**

```python
def test_metrics_distinguish_schema_validity_accuracy_and_stability():
    run = completed_run(["pass", "pass", "business_error", "schema_error"])
    metrics = score_run(run)
    assert metrics.schema_valid_rate == Decimal("0.75")
    assert metrics.run_accuracy == Decimal("0.50")
    assert metrics.stable_case_rate == Decimal("0.00")


def test_field_diff_uses_complete_objects():
    expected = Decision(action="accept", reason="matched")
    actual = Decision(action="reject", reason="matched")
    assert field_diff(expected, actual) == [{"path": "action", "expected": "accept", "actual": "reject"}]
```

- [ ] **Step 2: Write failing phase-gate tests**

```python
def test_validation_requires_strict_improvement():
    comparison = comparison_with_equal_metrics_and_no_regressions()
    assert evaluate_gate(comparison, "validation").passed is False


def test_acceptance_allows_equal_perfect_metrics():
    comparison = perfect_equal_comparison()
    assert evaluate_gate(comparison, "acceptance").passed is True


def test_any_case_or_stability_regression_fails_every_candidate_gate():
    assert evaluate_gate(comparison_with_regression(), "development").passed is False
    assert evaluate_gate(comparison_with_regression(), "validation").passed is False
    assert evaluate_gate(comparison_with_regression(), "acceptance").passed is False
```

- [ ] **Step 3: Run tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py -v`

Expected: FAIL because scoring modules are missing.

- [ ] **Step 4: Implement exact metrics and phase gates**

```python
def evaluate_gate(comparison: Comparison, phase: Literal["development", "validation", "acceptance"]) -> GateResult:
    repeats_required = 9 if phase == "acceptance" else 4
    common = (
        comparison.candidate.schema_valid_rate == Decimal("1")
        and comparison.critical_failures == 0
        and comparison.normal_cases_meeting(repeats_required)
        and comparison.regression_count == 0
        and comparison.stability_regression_count == 0
    )
    if not common:
        return GateResult(False, comparison.failure_reasons())
    nondecreasing = comparison.run_accuracy_delta >= 0 and comparison.stable_case_rate_delta >= 0
    if phase == "validation":
        return GateResult(nondecreasing and comparison.has_strict_core_improvement(), ())
    if phase == "acceptance":
        return GateResult(nondecreasing, ())
    return GateResult(True, ())
```

Reject comparisons unless every manifest field required by the spec is compatible except prompt hash and run timestamps.

- [ ] **Step 5: Run scoring and comparison tests**

Run: `rtk conda run -n kds python -m pytest tests/test_score_results.py tests/test_compare_runs.py -v`

Expected: PASS.

- [ ] **Step 6: Commit scoring and gates**

```powershell
rtk git add -- scripts/score_results.py scripts/compare_runs.py tests/test_score_results.py tests/test_compare_runs.py
rtk git commit -m "feat: score and compare prompt runs"
```

### Task 7: Worktree Cycle and Safe Delivery

**Files:**
- Create: `scripts/manage_worktree.py`
- Create: `tests/test_manage_worktree.py`

**Interfaces:**
- Consumes: original repository, prompt ID, delivery allowlist, expected source hashes, and final worktree commit.
- Produces: `WorktreeCycle`, `create_cycle(...)`, `build_delivery_patch(...)`, `preflight_patch(...)`, and `apply_delivery_patch(...)`.

- [ ] **Step 1: Write failing lifecycle and rollback tests**

```python
def test_cycle_base_is_original_head(repo):
    cycle = create_cycle(repo, "classify--abc123")
    assert cycle.cycle_base_commit == git(repo, "rev-parse", "HEAD")


def test_patch_excludes_runtime_reports_and_unlisted_files(completed_cycle):
    patch = build_delivery_patch(completed_cycle, SUCCESS_ALLOWLIST)
    assert ".runtime" not in patch.paths
    assert "reports" not in patch.paths
    assert "unrelated.txt" not in patch.paths


def test_apply_conflict_leaves_original_workspace_unchanged(completed_cycle):
    before = snapshot_workspace(completed_cycle.original_repo)
    modify_target_prompt(completed_cycle.original_repo)
    with pytest.raises(DeliveryConflict):
        apply_delivery_patch(completed_cycle)
    assert snapshot_workspace(completed_cycle.original_repo) == before.with_expected_user_edit()
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -v`

Expected: FAIL because worktree management is missing.

- [ ] **Step 3: Implement explicit cycle state and allowlists**

```python
SUCCESS_ALLOWLIST = {
    "prompt",
    "prompt-contract.yaml",
    "eval-config.yaml",
    "dev-cases.yaml",
    "validation-cases.yaml",
    "acceptance-cases.yaml",
    "adapter.py",
    "optimization-history.yaml",
    ".gitignore",
}
FAILURE_ALLOWLIST = SUCCESS_ALLOWLIST - {"prompt"}


@dataclass(frozen=True)
class WorktreeCycle:
    original_repo: Path
    worktree: Path
    branch: str
    cycle_base_commit: str
```

At delivery time, regenerate the patch from the immutable `cycle_base_commit`, the current cycle worktree `HEAD`, and committed worktree content. Obtain Git's actual changed paths with `--name-status -z`/`--name-only -z` (or an equivalent native command), derive the result-specific allowlist, and require the selected path set to equal the paths represented by the generated patch; any extra path or patch section is rejected. Immediately before applying, re-check the original workspace `HEAD == cycle_base_commit` and that the committed `prompt-contract.yaml` still names the cycle's canonical Prompt path (only current Prompt hash and other non-path fields may change). Run `git apply --check`, snapshot only exact target paths, apply without staging, verify actual result paths and destination hashes, and restore exact snapshots after any application or verification error. Persisted patch text, manifest fields, and hashes may be retained as transport artifacts but are not trust anchors. Never delete the worktree or branch automatically.

Do not add a custom complete Git patch parser or adversarial tamper-proofing. Low-cost correctness checks still cover the generated patch's path set, deletion rejection, failure-result Prompt exclusion, worktree boundary, original HEAD/cycle identity, committed Prompt-path identity, clean apply, post-apply path/hash verification, rollback, and unstaged/uncommitted delivery.

- [ ] **Step 4: Run worktree tests**

Run: `rtk conda run -n kds python -m pytest tests/test_manage_worktree.py -v`

Expected: PASS.

- [ ] **Step 5: Commit safe worktree delivery**

```powershell
rtk git add -- scripts/manage_worktree.py tests/test_manage_worktree.py
rtk git commit -m "feat: manage isolated prompt tuning cycles"
```

### Task 8: Complete the Skill Workflow and CLI Contracts

**Files:**
- Modify: `SKILL.md`
- Modify: `references/business-contract.md`
- Modify: `references/case-schema.md`
- Modify: `references/adapter-contract.md`
- Modify: `references/evaluation-method.md`
- Modify: `references/worktree-lifecycle.md`
- Modify: `tests/test_skill_structure.py`
- Create: `tests/test_skill_instructions.py`

**Interfaces:**
- Consumes: all script interfaces from Tasks 2–7.
- Produces: complete human-gated `tune` and read-only `verify` orchestration instructions.

- [ ] **Step 1: Write failing instruction-policy tests**

```python
def test_verify_forbids_acceptance_dataset(skill_text):
    verify = skill_text.split("## verify", 1)[1]
    assert "must not read or run `acceptance-cases.yaml`" in verify


def test_tune_contains_both_user_gates_and_stop_conditions(skill_text):
    assert "contract confirmation gate" in skill_text
    assert "delivery confirmation gate" in skill_text
    for reason in ("two consecutive rounds", "five candidate rounds", "contract conflict", "acceptance failure"):
        assert reason in skill_text
```

- [ ] **Step 2: Run instruction tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_skill_structure.py tests/test_skill_instructions.py -v`

Expected: FAIL because the initial shell does not yet encode the complete state machine.

- [ ] **Step 3: Expand `SKILL.md` into an executable state machine**

Encode these exact `tune` states: preflight → worktree → contract/cases/adapter → user confirmation → model probe/smoke → asset commit → dev/validation baseline → no-change exit or candidate loop → candidate freeze → single acceptance activity → failure exit or delivery confirmation → worktree commit → allowlisted synchronization. Encode `verify` as current-workspace execution over development, validation, or separately supplied non-acceptance cases only.

For every state, name the supporting script command, required input files, generated output, next state, and stop behavior. Use these CLI contracts consistently:

```text
validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode tune|verify --output WORKSPACE_JSON
validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON
run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|acceptance|external --repeats N --manifest PATH [--mode tune|verify]
score_results.py --manifest PATH --report PATH
compare_runs.py --baseline PATH --candidate PATH --phase development|validation|acceptance --report PATH
manage_worktree.py create --repo PATH --prompt-id ID --state PATH
manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
```

`verify` must reject `--dataset acceptance` before loading the file or constructing the client. Route detailed formats to the five references instead of duplicating them in `SKILL.md`.

- [ ] **Step 4: Run Skill instruction and structure validation**

Run:

```powershell
rtk conda run -n kds python -m pytest tests/test_skill_structure.py tests/test_skill_instructions.py -v
rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
```

Expected: all pytest tests PASS and `quick_validate.py` reports a valid Skill.

- [ ] **Step 5: Commit the completed workflow**

```powershell
rtk git add -- SKILL.md references tests/test_skill_structure.py tests/test_skill_instructions.py
rtk git commit -m "feat: define stabilizing prompts workflow"
```

### Task 9: Offline Integration and Behavioral Regression Suite

**Files:**
- Create: `tests/fixtures/target_repo/`
- Create: `tests/test_integration_tune.py`
- Create: `tests/test_integration_verify.py`
- Create: `tests/test_behavior_contract.py`

**Interfaces:**
- Consumes: the production leaf scripts/modules with a fixture-only, test-only
  client injection boundary.
- Produces: isolated evidence for renderer/schema wiring, baseline and
  candidate leaf execution, acceptance ownership, failure delivery, and
  workspace safety; it does not implement the `SKILL.md` tune/verify state
  machine.

- [ ] **Step 1: Build a minimal committed target-repository fixture**

Create a fixture containing `prompts/classify.md`, a production renderer, a Pydantic `Decision` Schema, a production call assembly function, and Git history. Its fake transport must return deterministic tool calls keyed by prompt hash and case ID; injection occurs through a test-only function argument that project config cannot select.

- [ ] **Step 2: Write failing end-to-end tests**

```python
def test_tune_initializes_and_delivers_only_after_acceptance_and_confirmation(target_repo):
    result = run_tune_with_fake_transport(target_repo, confirm_contract=True, confirm_delivery=True)
    assert result.acceptance_activities == 1
    assert result.original_workspace_status == {"prompts/classify.md": "unstaged", ".prompt-evals": "untracked"}
    assert result.delivered_prompt_hash == result.frozen_candidate_hash


def test_acceptance_failure_never_delivers_candidate(target_repo):
    result = run_tune_with_acceptance_failure(target_repo)
    assert result.stop_reason == "acceptance_failed"
    assert read_prompt(target_repo) == result.original_prompt


def test_verify_rejects_acceptance_cases_before_transport_call(target_repo):
    transport = CountingTransport()
    with pytest.raises(UsageError, match="acceptance"):
        run_verify(target_repo, dataset="acceptance", transport=transport)
    assert transport.call_count == 0
```

- [ ] **Step 3: Run integration tests and confirm failure**

Run: `rtk conda run -n kds python -m pytest tests/test_integration_tune.py tests/test_integration_verify.py tests/test_behavior_contract.py -v`

Expected: FAIL until fixture orchestration and all cross-module paths are connected.

- [ ] **Step 4: Add only the fixture helpers and CLI wiring required for the tests**

Wire the existing production leaf modules/CLIs without duplicating production
Schema, parsing, comparison, or worktree logic. Fixture helpers may sequence
those leaf calls for offline evidence, but must not add a production Python
`tune`/`verify` orchestrator. Cover no-change exit, regression rejection,
equal-perfect acceptance, interrupted-slot resume, dirty unrelated files, dirty
critical dependencies, patch collision, rollback, failure-asset
synchronization, and token redaction. Task 10 behavior-forward tests remain the
coverage for the actual `SKILL.md` orchestration.

- [ ] **Step 5: Run the complete offline suite**

Run: `rtk conda run -n kds python -m pytest tests -v`

Expected: PASS with no network access and no skipped tests.

- [ ] **Step 6: Commit integration coverage**

```powershell
rtk git add -- tests scripts SKILL.md
rtk git commit -m "test: cover prompt tuning workflows"
```

### Task 10: Personal Installation and Real Local-Model Validation

**Files:**
- Modify: `tests/test_behavior_contract.py`
- Create: `docs/superpowers/evidence/2026-09-11-stabilizing-prompts-skill-validation.md`

**Interfaces:**
- Consumes: completed Skill, fixed `kds` environment, fixed LAN model, and a disposable real Python Git fixture.
- Produces: installation evidence, real smoke-test evidence, and behavior-forward-test results with credentials removed.

- [ ] **Step 1: Run all offline verification from a clean checkout**

Run:

```powershell
rtk conda run -n kds python -m pytest tests -v
rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
rtk git diff --check
```

Expected: all tests PASS, Skill validation succeeds, and `git diff --check` prints no errors.

- [ ] **Step 2: Install the Skill into the personal Codex Skill directory**

Use the supported local Skill installation workflow to install this repository as `C:/Users/kgcda/.codex/skills/stabilizing-prompts`. Do not copy test reports, `.runtime`, target-project data, or credentials into any target repository.

- [ ] **Step 3: Run the fixed-model smoke test**

From a disposable committed fixture, run one development case through the production renderer, `with_structured_output(..., method="function_calling", include_raw=True)`, and the production Pydantic Schema. Verify model identity, fixed request settings, response-contract classification, timeout behavior, and secret redaction without printing the token.

- [ ] **Step 4: Run Codex behavior-forward scenarios**

Invoke the installed Skill through one explicit `$stabilizing-prompts` request and one natural-language request. Exercise normal tuning, multiple-prompt scoping, business-contract conflict, open-ended prompt refusal, no-change exit, validation regression rejection, equal-perfect acceptance, acceptance failure, failure-asset-only delivery, model outage, and original-workspace conflict.

- [ ] **Step 5: Record sanitized evidence and rerun the secret scan**

Record commands, exit codes, case counts, hashes, stop reasons, and redacted configuration in the evidence document. Then run `rtk rg -n "Authorization:|Bearer " .` and inspect every match; no credential value may appear.

- [ ] **Step 6: Commit validation evidence**

```powershell
rtk git add -- tests/test_behavior_contract.py docs/superpowers/evidence/2026-09-11-stabilizing-prompts-skill-validation.md
rtk git commit -m "test: validate stabilizing prompts skill"
```

- [ ] **Step 7: Perform final repository verification**

Run:

```powershell
rtk conda run -n kds python -m pytest tests -v
rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
rtk git diff --check
rtk git status --short --branch
```

Expected: all tests PASS, Skill validation succeeds, no diff errors exist, and the worktree is clean.
