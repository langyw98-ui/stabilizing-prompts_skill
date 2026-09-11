# Task 4 report — fixed local model client and secret safety

## Scope

Implemented the fixed `ChatOpenAI` client, strict local credential loading,
model-identity probing, safe configuration reporting, and recursive secret
redaction. The design spec and implementation plan now follow Ruling 4.

The real authorization value is not present in this repository. Runtime setup
reads only the Skill-root `.local/model-credentials.json`; tests use temporary
files with a non-production sentinel. The root `.gitignore` contains the exact
`.local/model-credentials.json` exclusion, and no installation behavior copies
that file.

## TDD evidence

1. Added `tests/test_local_model_client.py` first.
2. Ran the focused test before implementation. Collection failed because
   `scripts.local_model_client` did not exist.
3. Added `scripts/local_model_client.py` and reran the focused suite.
4. Focused suite passed: 14 tests.

## Implemented behavior

- Fixed endpoint, model, timeout, retry count, temperature, and disabled
  thinking/reasoning/search settings are constructed in tracked code only.
- Credential JSON accepts exactly one non-empty string field,
  `authorization_token`; missing, unreadable, malformed, empty, whitespace-only,
  duplicate, or extra-field files raise `CredentialSetupError` without exposing
  file contents.
- `probe_model` rejects missing, empty, or non-matching identities and returns
  `ModelProbe` only for the exact configured model.
- `safe_client_config` contains no authorization value and records omitted
  sampling fields.
- `safe_error` and `redact_secret` remove authorization/bearer/API-key values
  from text and nested mappings/sequences.

## Verification

- `conda run -n kds python -m pytest tests -v`: 43 passed.
- `git check-ignore -v .local/model-credentials.json`: exact root rule matched.
- `git diff --check`: passed.
- Secret scan found only intentional schema names, documentation examples, and
  temporary-test sentinel literals; no real credential or generated output.

## Fix round 1

- Updated the Task 4 brief so Ruling 4 explicitly supersedes the obsolete
  tracked-code Token instruction.
- The brief now consistently documents Skill-root
  `.local/model-credentials.json` loading, temporary sentinel paths for tests,
  the exact `.gitignore` rule, strict one-field validation, and no implicit
  credential installation or output.
