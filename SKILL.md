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

`tune` automatically initializes the repository-local exclude for this cycle
before creating the managed worktree. It verifies the final managed-worktree
target before creation and the concrete `reports/` and `.runtime/` paths after
creation; both ignore gates complete before evaluation assets, confirmation, or
any model call. Initialization is idempotent, preserves existing exclude bytes,
and never edits the project `.gitignore`, Git config, or global excludes.

There are two user gates. The contract confirmation gate confirms the contract,
cases, adapter, coverage obligations, frozen split, fixed environment,
repetition counts, thresholds, and stop rules before any model call. The
contract, cases, adapter, and coverage obligations are frozen together by the
first gate. The delivery confirmation gate is one combined
delivery-and-cleanup confirmation for every formal scored result; it authorizes
only that result's allowlisted synchronization and, after delivery verification,
cleanup. A declined or ambiguous answer leaves the original workspace unchanged
and retains the prepared evidence, worktree, branch, and state. A user
cancellation at the contract gate stops before any model call.

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

`preflight → repository-local exclude initialization → pre-create managed-worktree ignore verification → create and persist dedicated worktree → post-create reports/runtime ignore verification → contract/cases/adapter → user confirmation (contract confirmation gate) → model probe/smoke → asset commit → dev/validation baseline → no-change conclusion or candidate loop → optional candidate freeze → optional single acceptance activity → scored terminal outcome → normalize final result and render summary → append compact history and commit summary/deliverable evaluation assets as prepared_commit → show result, summary path, and delivery set → delivery-and-cleanup confirmation → resolve delivery_commit → allowlisted synchronization → verified cleanup`

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
  -> repository-local exclude initialization
  -> pre-create managed-worktree ignore verification
  -> create branch and worktree with git worktree add
  -> persist WorktreeCycle
  -> post-create reports/runtime ignore verification
  ```

- Identity/path: resolve the primary workspace identity, reject a linked
  worktree or detached `HEAD`, and derive the fixed target
  `.worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/` inside the target
  repository; never accept a caller-selected worktree path.
- Repository-local exclude initialization: automatically ask Git for the
  repository-local exclude path, preserve its existing bytes and comments, and
  add only the needed anchored rules when the concrete paths are not ignored.
  The operation is idempotent and atomic; it does not edit, stage, or commit
  `.gitignore`, `.git/config`, global Git configuration, or global excludes. It
  runs before creating any cycle asset, asking for confirmation, or calling the
  model.
- Pre-create managed-worktree ignore verification: run
  `git check-ignore --no-index --quiet -- .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/`
  for the final derived directory before creating its parent, branch, or
  worktree. If the actual Git query fails, stop with `setup_error` and do not
  write cycle state or evaluation assets.
- Command: `manage_worktree.py create --repo PATH --prompt-id ID --state PATH [--branch BRANCH]`.
- Output: a persisted `WorktreeCycle` state with the dedicated project-local
  worktree, internal or explicitly validated branch, original workspace
  identity, immutable `cycle_base_commit`, and completed pre-create ignore
  gate. The create operation performs the automatic initialization and
  pre-create check before `git worktree add`.
- Post-create reports/runtime ignore verification: from the linked worktree,
  run `manage_worktree.py verify-ignores --state STATE_PATH` and require Git to
  ignore `.prompt-evals/<prompt-id>/reports/` and
  `.prompt-evals/<prompt-id>/.runtime/`. This is a second, concrete Git gate;
  do not build assets, ask for confirmation, or call the model until it passes.
- Next: `contract/cases/adapter`, in this same worktree and cycle. Never make a
  second worktree for bootstrap or candidate rounds. A non-scoring setup
  interruption retains the exact cycle for diagnosis; successful delivery may
  proceed to verified cleanup after the combined confirmation.
- Setup stop: reject a repository-root mismatch, linked worktree, detached
  `HEAD`, failed exclude initialization or ignore coverage, invalid branch,
  `git worktree add` failure, or WorktreeCycle state-persistence failure before the first model call. Fail closed: never fall back to tuning in the original
  checkout. Preserve the original workspace and report the precise phase,
  paths, and safe Git diagnostic.

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
  The confirmed `coverage-obligations.yaml` is part of this immutable asset
  set and remains eligible for the result-specific delivery profile.
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
- Next: `no-change conclusion or candidate loop`. Do not calculate a gate while a
  planned slot is incomplete; resume the same manifest slot if the fixed
  client permits it, without replacing it with an extra successful call.
- Stop: setup/protocol failures, exhausted transport slots, incompatible
  manifests, or a newly discovered contract/adapter mismatch pause the cycle;
  record the non-scoring reason and do not generate a candidate.

### 8. no-change conclusion or candidate loop

If every planned development and validation call is `pass` and both baseline
gates pass, conclude the formal result `no_change_needed`: do not generate a
candidate or run acceptance, then route that scored conclusion through result
normalization, summary, prepared commit, and the delivery-and-cleanup
confirmation. A no-change result still has an assets delivery profile. If the
baseline reproduces an evidenced failure, use one failure cluster per round and
make the smallest prompt-only change in
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
new baseline; it is not fed into this cycle's acceptance. A scored candidate
gate failure is classified as `validation_failed`, while an equal/rejected
candidate, the consecutive no-improvement stop, and the round-count stop are
classified as `no_strict_improvement`, `no_improvement_limit`, and
`round_limit`, respectively, then all use the same finalization path.

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
- Next: `scored terminal outcome`, then `normalize final result`, render the
  summary, append compact history, and create `prepared_commit`.
- Stop: an acceptance failure is the scored result `acceptance_failed`; do not
  edit the candidate or rerun acceptance in the same cycle. The acceptance
  file remains owned by `tune`, never by `verify`, and the result proceeds to
  the unified finalization path.

### 11. normalize final result, summary, and `prepared_commit`

Every scored terminal outcome enters one finalization path. Normalize exactly
these seven formal outcomes before presenting any delivery decision:

When acceptance fails, normalize `acceptance_failed` and retain the candidate
out of production delivery. When acceptance passes, normalize
`acceptance_passed` and make the frozen candidate eligible for the success
profile only after confirmation.

- `no_change_needed` — both baseline gates pass, so no candidate or acceptance
  activity is needed.
- `no_strict_improvement` — a candidate is rejected or equal without a strict
  validation improvement.
- `validation_failed` — a candidate gate or regression fails at validation.
- `no_improvement_limit` — the consecutive no-improvement stop is reached.
- `round_limit` — the candidate-round limit is reached.
- `acceptance_failed` — the single acceptance activity fails; the candidate is
  not eligible to change the production Prompt.
- `acceptance_passed` — the frozen candidate passes the single acceptance
  activity and is eligible for success delivery.

Setup, protocol, transport, model-service, identity, asset, and user-confirmation
failures are non-scoring interruptions. They do not generate a formal summary,
`prepared_commit`, delivery confirmation, or cleanup; retain the diagnostic
cycle under the corresponding failure policy.

- Inputs: saved score reports, comparison reports, coverage audit and
  confirmation evidence, runner slot counts, smoke result, normalized stop
  reason, fixed UTC finish time, and (only for `acceptance_passed`) the frozen
  candidate path and hash. Never infer facts from raw model responses.
- Action: run
  `finalize_cycle.py prepare --state STATE_PATH --result RESULT --finished-at UTC --evidence SUMMARY_JSON [--candidate FROZEN_CANDIDATE --candidate-hash SHA256]`.
  Validate evidence and identity, normalize the result/profile, and render a
  deterministic Markdown summary at
  `.prompt-evals/<prompt-id>/evaluation-summaries/YYYY-MM-DD-HHMMSS-<result>.md`.
  Do not overwrite an existing identity-matching destination, include hashes or
  internal absolute paths in the summary, or include raw responses, tokens, or
  patch details. Append one compact optimization-history entry before asking
  for delivery confirmation.
- Output: the result, stop reason, summary path, and planned delivery set. The
  first six results use the `assets` (asset-only) profile; `acceptance_passed` uses the
  `success` profile. The summary, compact history, and current-cycle
  deliverable evaluation assets are committed in the worktree as
  `prepared_commit`; the production Prompt is not changed yet.
- Next: show the result, summary path, planned delivery set, paired acceptance
  reports/diff when applicable, and the exact resources the authorized cleanup
  would remove, then enter `delivery-and-cleanup confirmation`.

### 12. delivery-and-cleanup confirmation

- Inputs: every formal result's deterministic summary, planned delivery set,
  `prepared_commit`, exact cycle worktree/branch identity, and the paired
  acceptance evidence when the result is `acceptance_passed`.
- Action: present one explicit confirmation that combines delivery and
  successful-delivery cleanup. Explain that cleanup permanently removes the
  exact managed worktree (including ignored `reports/`, `.runtime/`, raw
  responses, patch files, manifests, and temporary candidates), the exact
  cycle branch, and eventually the single external `STATE_PATH`; show the
  precise branch name. For `acceptance_passed`, retain the paired reports,
  frozen candidate identity, Prompt diff, and gate results in the confirmation
  material.
- Refusal retention: a declined, ambiguous, or cancelled answer performs no
  synchronization, does not modify the original workspace, and does not start
  cleanup. Retain the `prepared_commit`, summary, evidence, worktree, branch,
  and state for inspection or a later delivery decision. This same retention
  applies to delivery or verification failure.
- On an affirmative answer, run
  `finalize_cycle.py approve --state STATE_PATH` to resolve `delivery_commit`:
  `assets` results reuse `prepared_commit` without an empty child commit;
  `acceptance_passed` alone writes the frozen candidate to the canonical
  production Prompt, updates only non-path fields in `prompt-contract.yaml`,
  and creates a child `delivery_commit`. No other result may change the
  production Prompt. The committed contract must retain the cycle's canonical
  Prompt path.
- State: atomically record delivery confirmation and cleanup authorization in
  the single external `STATE_PATH`; do not create a second confirmation or
  cleanup state file. Next: `allowlisted synchronization`.

### 13. allowlisted synchronization

- Inputs: `cycle_base_commit`, `prepared_commit`/resolved `delivery_commit`,
  final committed worktree `HEAD`, the formal result's delivery profile,
  canonical Prompt path from the committed contract, and the combined delivery
  and cleanup confirmation.
- Commands:

  ```text
  manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
  manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
  ```

- Output: a freshly generated, result-specific allowlisted patch applied
  unstaged to the original workspace. The effective delivery profile is always
  derived from current persisted finalization state; compatibility
  `--result success|failure` remains accepted for this version but cannot
  override that state. All formal results may deliver only the current prompt
  ID's committed evaluation assets, including
  `.prompt-evals/<prompt-id>/coverage-obligations.yaml`; only
  `acceptance_passed` may include the production Prompt. Reports, `.runtime/`,
  raw responses, `__pycache__`, temporary patch files, and unrelated paths are
  excluded. The original worktree and branch remain until verified cleanup.
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
  cycle_base_commit`, the final worktree `HEAD == delivery_commit`, the fixed
  managed-root path, a clean cycle worktree, and the committed contract to
  retain the cycle's canonical Prompt path. Run `git apply --check`, snapshot
  exact targets, apply without staging, and verify actual paths and destination
  hashes. Then atomically record applied/verified delivery state in
  `STATE_PATH`. This is transactional: if apply, verification, or the delivery
  state write fails, restore the exact pre-apply snapshots, report rollback,
  and do not start cleanup or claim delivery success. Never auto-merge,
  overwrite user edits, stage/commit the original workspace, or rely on a
  persisted patch/manifest/hash as an adversarial trust anchor. Successful
  synchronization leaves files unstaged and uncommitted; next is
  `verified cleanup`.

### 14. verified cleanup

Start cleanup only after the combined confirmation, successful patch apply and
destination verification, and the atomic `STATE_PATH` update recording
`delivery_applied`, `delivery_verified`, and cleanup authorization. A setup,
protocol, transport, model, confirmation, synchronization, or verification
failure never starts cleanup.

- Command: `manage_worktree.py cleanup --state STATE_PATH`. The command derives
  every deletion target from the single current cycle state; it accepts no
  caller-supplied worktree, branch, or extra path.
- First-start gates: verify the original workspace identity and cleanliness,
  the exact managed worktree and cycle branch, `delivery_commit`, and the
  recorded delivery evidence. Then perform this exact sequence from outside
  the worktree:

  ```text
  verify fixed Git cleanliness
  -> git worktree remove <exact-cycle-worktree>
  -> verify exact cycle directory and Git registration are absent
  -> remove .worktrees/stabilizing-prompts/ only when empty
  -> verify exact cycle branch is unused by every worktree
  -> git branch -D <exact-cycle-branch>
  -> delete exact STATE_PATH
  ```

  Never use `git worktree remove --force`, `git worktree prune`, reflog expiry,
  Git GC, glob deletion, or recursive deletion. `.worktrees/` itself always
  remains; an occupied managed parent belongs to another cycle or user and is
  preserved.
- Each successful phase atomically updates `STATE_PATH` before the next
  destructive step. If cleanup partially fails, keep the delivered original
  workspace unchanged, retain the branch/state and diagnostic progress, and
  stop later steps. Cleanup is not a rollback of verified delivery.
- Retry is phase-aware: revalidate already-completed postconditions and current
  exact identities, operate only on resources still present, and do not repeat
  deletion of a worktree or branch already verified absent. A removed worktree
  does not have to pass the first-start existence/cleanliness gates again.

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
manage_worktree.py verify-ignores --state STATE_PATH
manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
manage_worktree.py cleanup --state STATE_PATH
finalize_cycle.py prepare --state STATE_PATH --result RESULT --finished-at UTC --evidence SUMMARY_JSON [--candidate FROZEN_CANDIDATE --candidate-hash SHA256]
finalize_cycle.py approve --state STATE_PATH
```

The runner defaults to `--mode tune`; callers running the read-only workflow
must pass `--mode verify`. A verify invocation rejects `--dataset acceptance`
before opening any manifest or case file, importing the adapter, or
constructing the model client. `build-patch` derives the effective delivery
profile from current finalization state; its compatibility `--result` spelling
is accepted during this version but is not an authority for result routing.

`validate_cases.py` consumes the mandatory proposed/editable
`coverage-obligations.yaml` during asset construction. User confirmation
freezes the asset; coverage obligations add no CLI option or command.

## verify

`verify` is read-only execution in the current workspace and does not modify repository-local exclude. The check occurs before any output or model call; perform read-only
Git checks for the concrete report/cache paths; missing ignore coverage returns
`setup_error` without creating output. Reject `--dataset acceptance`
immediately, before loading any dataset file and before constructing the model
client. It must not read or run `acceptance-cases.yaml`—during a cycle or after
it ends.

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
