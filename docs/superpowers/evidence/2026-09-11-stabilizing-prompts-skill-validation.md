# Stabilizing Prompts Skill Validation

Validation date: 2026-09-12  
Task: 10 (personal installation and local-model validation)  
Source checkout: `feature/stabilizing-prompts-skill` at `d3c66b586c95a44fdeaf4ffc58249776cddf3d18`

## Offline validation

The checks below were run from the implementation worktree after removing the
exact generated directories `scripts/__pycache__/` and `tests/__pycache__/`.
They use the fixed `kds` environment and do not require network access.

| Check | Exit code | Result |
| --- | ---: | --- |
| `rtk conda run -n kds python -m pytest tests -v` | 0 | 168 passed, no skipped tests |
| `rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .` | 0 | `Skill is valid!` |
| `rtk git diff --check` | 0 | clean |

The repository secret scan was run as:

```text
rtk proxy rg -n "Authorization:|Bearer " .
```

Matches were limited to credential-redaction implementation patterns in
`scripts/local_model_client.py`, `scripts/run_prompt_eval.py`, and
`scripts/validate_cases.py`; test-only sentinel patterns in the local-client,
runner, integration-support, and behavior-contract tests; and literal pattern
examples in the design/plan documents. No local credential file or real
credential value was present in the checkout.

## Personal installation

The target did not exist at the first read-only check, so no backup or
replacement was needed. The Skill was installed at:

```text
C:\Users\kgcda\.codex\skills\stabilizing-prompts
```

Only these runtime files were installed: `SKILL.md`, `.gitignore`,
`agents/openai.yaml`, seven files under `scripts/`, and five files under
`references/` (15 files total). The installation explicitly excluded `.git`,
`.worktrees`, `.superpowers`, `docs`, `tests`, `reports`, `.runtime`, `.local`,
and all credential files. No target-project data was copied.

The installed directory passed:

```text
rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py C:/Users/kgcda/.codex/skills/stabilizing-prompts
```

Exit code was 0 (`Skill is valid!`). The installed credential-file check was
false, every excluded-directory check was false, and the installed scan found
only the same static redaction identifiers as the source scan.

SHA-256 parity checks between source and installed metadata:

```text
SKILL.md        06a696197873cecc3378001b70a6a3f251e5c6a290ea4276093407d6eb4d3dbd
agents/openai.yaml
                b0565abef10658234c5b0d90b8eb9bef98485999ca853bc7f6c4ea42437ddb68
.gitignore      5c4d5ce215b9bffc13319e733ff95aac298b2602bbc18a0bf780f8c8986cbaca
```

## Credential gate and real smoke

The only supported credential path was checked without printing or copying its
contents:

```text
C:\Users\kgcda\.codex\skills\stabilizing-prompts\.local\model-credentials.json: absent
strict loader result: absent
```

The source checkout credential path was also absent. This is a setup gate, so
no client was constructed and no LAN request was attempted. The real fixed
model smoke is therefore **not completed**; model identity, structured-call
response, timeout behavior, and live redaction remain an external gate blocked
by missing local credentials. No Authorization material was emitted.

The safe fixed-client configuration reserved for the smoke is:

```yaml
base_url: http://192.168.168.230:8000/v1
model: dbirks/Qwen3.8-27B-W4A16-AutoRound
temperature: 0.0
timeout: 30
max_retries: 2
extra_body:
  enable_thinking: false
  enable_reasoning: false
  enable_search: false
omitted_sampling_parameters:
  - top_p
  - seed
  - presence_penalty
  - frequency_penalty
  - logprobs
  - top_logprobs
  - logit_bias
  - n
  - max_completion_tokens
  - reasoning_effort
  - reasoning
  - stop_sequences
```

## Behavior-forward validation

Behavior-forward scenarios were not run or simulated in this task, and no
results were edited. The requested explicit and natural-language evaluator
passes are **pending controller-dispatched evaluators**. Their results must be
added by the controller after dispatch; this document does not claim coverage
for those scenarios.

## Final state

The installation is outside the repository and is not a repository commit.
Generated Python caches are disposable and are removed by the final exact
cleanup. The source evidence/validation commit and final worktree status are
reported by the controller after commit and cleanup.
