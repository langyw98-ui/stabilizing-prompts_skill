---
name: stabilizing-prompts
description: Use when a Python repository-backed Markdown prompt with Pydantic structured output is inconsistent across repeated model calls or needs a deterministic local-model regression suite.
---

# Stabilizing Prompts

Operate on exactly one repository-relative `.md` prompt in a Python Git
repository. Python commands are always run with `conda run -n kds python`; do
not probe, select, or fall back to another environment. The fixed local model
is a pressure-test dependency, not a tuning parameter. Do not use this Skill
for open-ended prose, prompts without deterministic business truth, multiple
targets in one cycle, or a Schema/call path that cannot be reproduced through
the production code.

The five references are the detailed contracts. Read only the references
needed for the current state, and use them as the source of formats and
evidence rules:

- `references/business-contract.md`: evidence precedence and the contract confirmation gate.
- `references/case-schema.md`: complete expected objects, split ownership, and leakage checks.
- `references/adapter-contract.md`: the only project-specific rendering and Schema boundary.
- `references/evaluation-method.md`: fixed slots, deterministic scoring, metrics, gates, and reports.
- `references/worktree-lifecycle.md`: cycle state, delivery safety, and failure handling.

## tune

`tune` owns one continuous cycle. If the evaluation assets are missing,
`bootstrap` is an internal initialization state of `tune`: it must complete in
the same cycle and the same dedicated worktree before `tune` continues to the
baseline. Bootstrap is not a separately callable user mode and may not be
paused and resumed in another worktree. The original workspace is never used
for candidate experiments.

There are two independent user gates. The contract confirmation gate confirms
the contract, cases, adapter, frozen split, fixed environment, repetition
counts, thresholds, and stop rules before any model call. The delivery
confirmation gate is required for both success delivery and failure-asset-only
delivery. A user cancellation at either gate stops with no model call or no
synchronization, respectively.

Coverage has two deliberately separate layers, and they run in this order:

```text
mechanical coverage audit
  -> Codex evidence scan and saturation statement
  -> user confirmation
  -> model probe/smoke
```

The ownership boundary is:

```text
validate_cases.py:
  validates schemas, counts, hard duplicates, explained near duplicates,
  quotas, category declarations, critical coverage, and matrix completeness

Codex + user:
  review scanned evidence, unregistered evidenced boundaries, near-duplicate
  distinctions, total call slots, and the saturation statement
```

The mechanical audit is evidence about the declared obligations and cases; it
does not decide whether the evidence scan is saturated. Codex records the
scan scope and its saturation statement, and the user confirms both before
the model probe. The confirmation remains cycle state and binds
`coverage_obligations_hash` and `case_suite_hash` alongside the other frozen
asset identities. It is not a project asset or a CLI input. Any change to a
frozen asset invalidates the confirmation and all old runs, so the assets must
be audited and confirmed again before another model call.

The state machine is:

`preflight → worktree → contract/cases/adapter → user confirmation → model probe/smoke → asset commit → dev/validation baseline → no-change exit or candidate loop → candidate freeze → single acceptance activity → failure exit or delivery confirmation → worktree commit → allowlisted synchronization`

Each state below names its inputs, command/API, output, next state, and stop
behavior. Paths are placeholders; `PATH` is resolved inside the appropriate
repository or worktree and is never taken from model output.

### 1. preflight

- Inputs: the repository root, one `.md` target, and the production files that
  affect rendering, Schema loading, message assembly, and downstream truth.
- Command: `validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode tune --output WORKSPACE_JSON`.
- Output: a read-only `WorkspaceSnapshot` JSON containing the Git root,
  current `HEAD`, canonical prompt path, prompt ID/hash, fixed `kds` command,
  and Python version. Treat this `HEAD` as `cycle_base_commit` after the
  worktree is created.
- Next: `worktree`. If the target is outside scope, a critical dependency is
  dirty/staged, the prompt is not exactly one tracked `.md`, or an existing
  contract points at another path, stop with a non-scoring `setup_error`.
  Unrelated dirty files may remain in the original workspace; do not stash,
  copy, repair, or commit them.

### 2. worktree

- Inputs: the preflight snapshot and its stable `prompt_id`.
- Setup sequence:

  ```text
  resolve primary checkout identity
  -> reject linked worktree or detached HEAD
  -> derive .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/
  -> verify that directory is ignored with git check-ignore
  -> create branch and worktree with git worktree add
  -> persist WorktreeCycle
  ```

- Preflight: resolve the primary workspace identity, then reject a linked
  worktree or detached `HEAD`. Derive the fixed target
  `.worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/` inside the target
  repository; never accept a caller-selected worktree path.
- Ignore gate: run `git check-ignore --no-index --quiet` for the final target
  directory before creating its parent, branch, or worktree. The target
  directory must already be covered by an ignore rule. Do not edit, stage, or commit the target repository's `.gitignore`. Before invoking `tune`, the user must establish an ignore rule that
  covers `.worktrees/` (in the project rules, `.git/info/exclude`, or a global
  excludes file); if the rule is missing, stop with `setup_error`.
- Command: `manage_worktree.py create --repo PATH --prompt-id ID --state PATH [--branch BRANCH]`.
- Output: a persisted `WorktreeCycle` state with the dedicated project-local
  worktree, internal or explicitly validated branch, original workspace
  identity, and immutable `cycle_base_commit`.
- Next: `contract/cases/adapter`, in this same worktree and cycle. Never make a
  second worktree for bootstrap or for candidate rounds. The worktree and
  branch are not automatically deleted.
- Setup stop: reject a repository-root mismatch, linked worktree, detached
  `HEAD`, missing ignore coverage, invalid branch, `git worktree add` failure,
  or WorktreeCycle state-persistence failure before the first model call. Fail
  closed: never fall back to tuning in the original checkout. Preserve the
  original workspace and report the precise reason; retained worktrees and
  branches require manual inspection or cleanup.

### 3. contract/cases/adapter

- Inputs: production Pydantic Schema and renderer/call-chain evidence,
  dependency files, existing `prompt-contract.yaml`/`eval-config.yaml` when
  valid, and any history that is already confirmed.
- Action: derive or update the mandatory proposed/editable
  `coverage-obligations.yaml` from production evidence before generating
  cases.
- Command: `validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON`.
  Derive or update `coverage-obligations.yaml` from production evidence first,
  then validate all three split files together during asset construction; use
  the selected-split behavior of the runner for later isolated runs. This is
  the mechanical coverage gate: it validates schemas, counts, hard duplicates,
  explained near duplicates, quotas, category declarations, critical coverage,
  and matrix completeness.
- Output: proposed/editable `prompt-contract.yaml`, `eval-config.yaml`,
  `dev-cases.yaml`, `validation-cases.yaml`, `acceptance-cases.yaml`,
  mandatory proposed/editable `coverage-obligations.yaml`, `adapter.py`, and a
  coverage matrix, plus
  `CASE_SUITE_JSON`. Every case has a complete expected object validated by the
  production Schema; IDs and semantic/input fingerprints are globally
  isolated across splits. The adapter must expose production messages and
  Schema through `prepare_call`. After the mechanical gate passes, Codex
  scans the Schema, production branches, business contract, historical
  failures, and input boundaries; Codex and the user review scanned evidence,
  unregistered evidenced boundaries, near-duplicate distinctions, total call
  slots, and the saturation statement.
- Hashes in `CASE_SUITE_JSON` have distinct meanings: each
  `case_file_hashes.<split>` value is the exact raw-byte SHA-256 of that split
  file, while `case_suite_hash` is the canonical hash of validated cases,
  normalized production expected objects, and split identity. YAML formatting
  changes can affect a raw file hash without changing the canonical suite hash.
- Next: `user confirmation`. No acceptance case is run in this state.
- Stop: pause for user adjudication on business evidence conflict or an
  uncertain truth. Invalid cases, Schema import, adapter fidelity, path
  identity, critical dependencies, an incomplete mechanical audit, or an
  incomplete saturation statement are `setup_error`; do not weaken cases,
  duplicate the Schema, or continue to a model call.

### 4. user confirmation (contract confirmation gate)

- Inputs: the proposed contract, mandatory proposed/editable
  `.prompt-evals/<prompt-id>/coverage-obligations.yaml`, complete case
  inputs/expected objects and sources, split coverage matrix and mechanical
  audit, the Codex evidence scan and saturation statement, adapter boundary,
  fixed `kds` command and Python version, default repetitions (5 for
  development/validation, 10 for acceptance), thresholds, and every stop
  condition.
- Action: show this material to the user and obtain an explicit confirmation.
  The user reviews the mechanical audit separately from Codex's evidence and
  saturation statement, then freezes the contract, coverage obligations, all
  three datasets, adapter, and evaluation settings only after confirmation.
- Output: a confirmation record tied to the cycle with frozen-asset hashes
  `coverage_obligations_hash` and `case_suite_hash`, plus
  `evidence_checked`, `saturation_statement`, and the explicit near-duplicate review confirmation/status (`near_duplicate_review_status`). The
  record remains cycle state, not a project asset or a CLI input.
- Next: `model probe/smoke` only on an affirmative answer.
- Stop: a declined, ambiguous, or cancelled confirmation leaves assets
  uncalled and the original prompt untouched. Contract/cases/adapter user
  confirmation is always earlier than the first model call.

### 5. model probe/smoke

- Inputs: the frozen confirmation, including the coverage-obligation and case
  suite hashes, Skill-local
  `.local/model-credentials.json`, the fixed client configuration, the
  production adapter, original prompt, and one development case.
- API/command: use `build_client()` and `probe_model()` from
  `scripts/local_model_client.py`, then exercise the adapter through the
  runner with a development smoke manifest. The endpoint, model identity,
  `temperature=0.0`, disabled thinking/reasoning/search, 30-second timeout,
  two retries, and structured `include_raw=True` call are fixed in tracked
  code; project config and candidates cannot override them.
- Output: a redacted probe identity/configuration and a development-only smoke
  result. Do not load or infer anything from acceptance results.
- Next: `asset commit` only when identity, transport, structured response
  envelope, adapter rendering, and Schema instantiation pass.
- Stop: missing/malformed credentials, model identity mismatch, unavailable
  model, rendering/adapter failure, or protocol failure pauses as
  `setup_error`, `transport_error`, or `protocol_error` as applicable. Never
  switch endpoint/model or print an authorization value.

### 6. asset commit

- Inputs: frozen, validated assets and successful smoke evidence.
- Action: after the model probe/smoke succeeds, commit the contract,
  configuration, three case files,
  `.prompt-evals/<prompt-id>/coverage-obligations.yaml`, adapter, and the
  confirmed evaluation assets in the dedicated worktree. Do not edit, stage, or commit the target repository's `.gitignore`; evaluation outputs must
  remain under already-ignored `reports/`/`.runtime/` paths.
- Output: an asset commit recorded in the cycle state and the immutable
  committed input hashes used by manifests.
- Next: `dev/validation baseline`.
- Stop: if any frozen asset, including `coverage-obligations.yaml`, changed
  after confirmation, the commit cannot be made, or its prompt identity is
  inconsistent, invalidate the old confirmation and all old runs and stop;
  do not silently rebuild a baseline.

### 7. dev/validation baseline

- Inputs: the asset commit, original production prompt, frozen adapter/schema,
  and only the selected development or validation case file.
- Commands (five repetitions each):

  ```text
  run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev --repeats 5 --manifest DEV_BASELINE_MANIFEST --mode tune
  score_results.py --manifest DEV_BASELINE_MANIFEST --report DEV_BASELINE_REPORT
  run_prompt_eval.py --eval-root PATH --prompt PATH --dataset validation --repeats 5 --manifest VALIDATION_BASELINE_MANIFEST --mode tune
  score_results.py --manifest VALIDATION_BASELINE_MANIFEST --report VALIDATION_BASELINE_REPORT
  ```

- Output: immutable manifests, deterministic score reports, failure clusters,
  and development/validation baseline gates. `acceptance-cases.yaml` is not
  loaded or run in this state.
- Next: `no-change exit or candidate loop`. Do not calculate a gate while a
  planned slot is incomplete; resume the same manifest slot if the fixed
  client permits it, without replacing it with an extra successful call.
- Stop: setup/protocol failures, exhausted transport slots, incompatible
  manifests, or a newly discovered contract/adapter mismatch pause the cycle;
  record the non-scoring reason and do not generate a candidate.

### 8. no-change exit or candidate loop

If every planned development and validation call is `pass` and both baseline
gates pass, stop with `no_change_needed`: this does not generate a candidate
and does not run acceptance. If the baseline reproduces an evidenced failure, use one
failure cluster per round and make the smallest prompt-only change in
`.runtime/`; never change the Schema, contract, assertions, datasets, adapter,
or fixed client. Read `optimization-history.yaml` to avoid repeating rejected
strategies, but do not treat it as business truth.

For each candidate round:

1. Run affected development cases first, then the complete development split;
   score each manifest and compare the original baseline with:
   `compare_runs.py --baseline BASELINE --candidate CANDIDATE --phase development --report DEVELOPMENT_COMPARISON`.
2. Only when the complete development gate has no regression, run the complete
   validation split and score it, then compare with:
   `compare_runs.py --baseline BASELINE --candidate CANDIDATE --phase validation --report VALIDATION_COMPARISON`.
3. Retain a candidate only when it meets validation gates, has no regression
   or stability regression, and at least one core metric strictly improves.
   Count a rejected/equal candidate as no improvement and keep it runtime-only.

Output is a round report, candidate hash, metric delta, failure cluster, and
the current improvement count. Next is another round or `candidate freeze`.
Stop the loop after five candidate rounds (`five candidate rounds`) or after
`two consecutive rounds` with no metric improvement. Also stop immediately
for a `contract conflict`, adapter distortion, model unavailability,
incomplete/non-scoring evidence, or an unreplicated real-world defect. A new
user-supplied defect requires a new case, renewed contract confirmation, and
new baseline; it is not fed into this cycle's acceptance.

### 9. candidate freeze

- Inputs: a validation-passing candidate and its complete development and
  validation comparisons.
- Action: choose the candidate with the required strict validation improvement
  and freeze its exact content hash. Keep the file in `.runtime/`; do not
  replace the production prompt yet.
- Output: immutable `frozen_candidate_hash` and candidate comparison reports.
- Next: `single acceptance activity`.
- Stop: if no candidate satisfies the validation gate, stop with the relevant
  round limit/no-improvement/regression reason and never run acceptance.

### 10. single acceptance activity

- Inputs: the committed asset state, original prompt, frozen candidate hash,
  frozen acceptance file, and compatible fixed client configuration.
- Commands (the only logical final activity in this cycle; ten repetitions for
  each prompt):

  ```text
  run_prompt_eval.py --eval-root PATH --prompt ORIGINAL_PATH --dataset acceptance --repeats 10 --manifest ACCEPTANCE_BASELINE_MANIFEST --mode tune
  run_prompt_eval.py --eval-root PATH --prompt FROZEN_CANDIDATE_PATH --dataset acceptance --repeats 10 --manifest ACCEPTANCE_CANDIDATE_MANIFEST --mode tune
  score_results.py --manifest ACCEPTANCE_BASELINE_MANIFEST --report ACCEPTANCE_BASELINE_REPORT
  score_results.py --manifest ACCEPTANCE_CANDIDATE_MANIFEST --report ACCEPTANCE_CANDIDATE_REPORT
  compare_runs.py --baseline ACCEPTANCE_BASELINE_MANIFEST --candidate ACCEPTANCE_CANDIDATE_MANIFEST --phase acceptance --report ACCEPTANCE_COMPARISON
  ```

- Output: one paired acceptance comparison and a delivery gate. Acceptance
  runs once per cycle, after the candidate hash is frozen; its result is not
  fed back into this cycle. An interrupted slot may resume the same immutable
  acceptance manifests, but cannot create a second acceptance activity.
- Next: `failure exit or delivery confirmation`.
- Stop: an acceptance failure ends the cycle; do not edit the candidate or
  rerun acceptance in the same cycle. The acceptance file remains owned by
  `tune`, never by `verify`.

### 11. failure exit or delivery confirmation

If acceptance fails, take the failure exit: record the stop reason and compact
failure history, and explicitly tell the user that this is an acceptance
failure and that the candidate is not delivered. A failure-asset-only patch
may be offered only after a separate delivery confirmation gate; it may contain
confirmed evaluation assets and new failure history, but it does not deliver the candidate
or production prompt. If the user declines, leave the worktree
and original workspace unchanged.

If acceptance passes, show the paired reports, frozen hash, diff, gate results,
and allowlisted files, then explicitly confirm whether to deliver the final
production prompt. This success delivery confirmation gate is independent of
the earlier contract confirmation gate. A decline preserves the original
production prompt and performs no synchronization.

### 12. worktree commit

- Inputs: affirmative success delivery confirmation, or affirmative failure
  asset-only delivery confirmation; the frozen cycle state.
- Action: on success only, replace the production Prompt with the frozen
  candidate, update only the current Prompt hash/non-path fields in
  `prompt-contract.yaml`, and append compact failure/optimization history.
  On failure delivery, exclude the production Prompt and candidate. Commit the
  selected deliverables, including the confirmed `coverage-obligations.yaml`,
  in the same cycle worktree; never
  commit runtime candidates, reports, raw responses, credentials, or tokens.
- Output: final committed worktree `HEAD` and a cycle state tied to the
  candidate/asset hashes.
- Next: `allowlisted synchronization`.
- Stop: a path redirection, unexpected changed file, hash mismatch, or commit
  failure stops before synchronization; do not substitute a different prompt.

### 13. allowlisted synchronization

- Inputs: `cycle_base_commit`, final committed worktree `HEAD`, result type,
  canonical Prompt path from the committed contract, and the user's delivery
  confirmation.
- Commands:

  ```text
  manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
  manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
  ```

- Output: a freshly generated, result-specific allowlisted patch applied
  unstaged to the original workspace. Success may include the production
  prompt and confirmed assets, including
  `.prompt-evals/<prompt-id>/coverage-obligations.yaml`; failure excludes the
  production prompt and candidate but may deliver that confirmed asset. The
  original worktree and branch remain for inspection.
- Delivery-time stale-state guard: after model phases and immediately before
  synchronization, re-check the original `HEAD == cycle_base_commit`, final
  worktree `HEAD`, committed contract identity, and the fixed managed-root
  path (`.worktrees/stabilizing-prompts/<cycle-dir>`). A stale/invalid,
  legacy, or externally located cycle is rejected; it is not migrated. `HEAD`
  drift or worktree commit mismatch also stops delivery. These guards run at
  delivery time and are not setup checks that promise to precede the first
  model call.
- Safety/stop behavior: regenerate from trusted cycle state at delivery time;
  compare Git-reported changed paths and patch paths exactly with the derived
  allowlist, rejecting reports/runtime/unrelated files, deletions, or extra
  sections. Immediately before apply require original `HEAD ==
  cycle_base_commit` and the committed contract to retain the cycle's
  canonical Prompt path. Run `git apply --check`, snapshot exact targets,
  apply without staging, and verify actual paths and destination hashes. On
  any conflict or verification error restore the exact snapshots; never
  auto-merge, overwrite user edits, or rely on persisted patch/manifest/hash
  as an adversarial trust anchor. Successful synchronization leaves files
  unstaged and uncommitted, and does not auto-delete the worktree.

### CLI contracts

These are the stable command shapes consumed by the states above. All are
invoked as `conda run -n kds python scripts/<command>` from the relevant
repository/worktree unless a state explicitly names a Python API. `verify`
uses `--mode verify`; only `tune` may select `--dataset acceptance`.

```text
validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode tune|verify --output WORKSPACE_JSON
validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON
run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|acceptance|external --repeats N --manifest PATH [--mode tune|verify]
score_results.py --manifest PATH --report PATH
compare_runs.py --baseline PATH --candidate PATH --phase development|validation|acceptance --report PATH
manage_worktree.py create --repo PATH --prompt-id ID --state PATH [--branch BRANCH]
manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
```

The runner defaults to `--mode tune`; callers running the read-only workflow
must pass `--mode verify`. A verify invocation rejects `--dataset acceptance`
before opening any manifest or case file, importing the adapter, or
constructing the model client.

`validate_cases.py` consumes the mandatory proposed/editable
`coverage-obligations.yaml` during asset construction. User confirmation
freezes the asset; coverage obligations add no CLI option or command.

## verify

`verify` is read-only execution in the current workspace. Reject
`--dataset acceptance` immediately, before loading any dataset file and before
constructing the model client. It must not read or run `acceptance-cases.yaml`
—during a cycle or after it ends.

### verify state machine

1. Validate the current repository, one prompt, critical dependencies, and
   evaluation assets with `validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode verify --output WORKSPACE_JSON`.
   Allow the user to acknowledge unrelated/critical dirty state only as the
   reference contract permits, and record current hashes; never repair files.
2. Select exactly one `dev`, `validation`, or separately supplied non-
   acceptance `external` dataset. Before loading it, reject any
   `--dataset acceptance` request. Validate the selected non-acceptance cases
   with `validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON`
   or the external-case contract.
3. Run the selected prompt using
   `run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|external --repeats N --manifest PATH --mode verify`,
   then use `score_results.py --manifest PATH --report PATH` and, when a saved
   baseline exists, `compare_runs.py --baseline PATH --candidate PATH --phase development|validation --report PATH`.
4. Write only ignored report/cache outputs. Report deterministic metrics,
   slot/error details, and regressions. Verify does not modify the prompt,
   contract, cases, adapter, or optimization history; do not create candidates,
   a worktree, or a commit.

A verify stop is non-scoring for setup/protocol/transport failures and
explicit for invalid assets, dirty critical dependencies, or incompatible
manifests. It never treats an acceptance file as an input or as a fallback.
