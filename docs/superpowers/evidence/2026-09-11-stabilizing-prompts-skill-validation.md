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

Exit code was 0 (`Skill is valid!`). At installation time the credential-file
check was false; the controller later supplied the credential and the current
strict-load result is recorded below. Every excluded-directory check was
false, and the installed scan found only the same static redaction identifiers
as the source scan.

SHA-256 parity checks between source and installed metadata:

```text
SKILL.md        06a696197873cecc3378001b70a6a3f251e5c6a290ea4276093407d6eb4d3dbd
agents/openai.yaml
                b0565abef10658234c5b0d90b8eb9bef98485999ca853bc7f6c4ea42437ddb68
.gitignore      5c4d5ce215b9bffc13319e733ff95aac298b2602bbc18a0bf780f8c8986cbaca
```

## Credential gate and real smoke

The controller supplied the local credential at the supported installed-Skill
path. Only file existence and the strict loader result were inspected; its
contents were never printed, copied, or persisted in this repository:

```text
C:\Users\kgcda\.codex\skills\stabilizing-prompts\.local\model-credentials.json: present
strict loader result: valid
```

The source checkout credential path remained absent, as required. A fixed
client construction check exited 0 and reported `client-settings-status:
verified`; its safe configuration hash was:

```text
1cb27d35b75624521c182572a2dca939c99cc9746ca2d0246d062ecdfd2a6436
```

The disposable committed Python Git fixture was:

```text
D:\Workspace\AgentPlugin\stabilizing-prompts-smoke-fixture-20260912
fixture final HEAD: b51ca78e8c67f1fd2172623ce74a7c23eeba6269
```

Its production renderer/Schema preflight exited 0 with
`renderer-schema-status: verified`, schema reference
`target_app.production:Decision`, and renderer-message SHA-256
`627b82ecbfc9c8ba3ce774106ba4864cd83302bb1146abbba93c36c561241b54`.
Full case validation exited 0 with counts `dev=1`, `validation=2`,
`acceptance=2`, and dataset SHA-256
`5d277e28b9b8e7a8c27b110d47c06d5034fa7161d2450a5a3a57ef93d4f53d09`.

### Historical pre-migration attempt (old endpoint)

The following identity and smoke results are retained from the pre-migration
attempt, which used the old endpoint `http://192.168.168.230:8000/v1`. They are
historical evidence only and are not the current client configuration.

The fixed model identity probe returned a failure (command exit 1) before an
identity could be accepted. The real runner then entered the production
structured-output path and exited 2 because the LAN service returned
`transport_error:http_status_502`. Its sanitized manifest reported:

```text
manifest status: incomplete
slots: 5
pending: 4
classifications: transport_error=1
manifest SHA-256: 135193696952bfc333cb581092b9174c2179bc11757d8c8b8fd4bb9827945cd1
```

The renderer and production Pydantic Schema therefore passed the pre-call
boundary, and the fixed `with_structured_output` call path was attempted, but
the service failed before returning a response envelope. No parsed response,
protocol classification, live timeout observation, or live redaction result
was available. The constructor-level fixed settings and 30-second timeout
were verified; offline redaction coverage remains the only redaction evidence.
This smoke is **not completed** and is blocked by the external model-service
gate (`HTTP 502`); no alternate model, endpoint, or credential was tried.

The safe fixed-client configuration used for that historical attempt (old
endpoint) is:

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
The committed temporary fixture was removed after its sanitized hashes and
statuses were captured. Generated Python caches are disposable and are removed
by the final exact cleanup. The source evidence/validation commit and final
worktree status are reported in the controller handoff.
