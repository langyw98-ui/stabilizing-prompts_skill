# Task 9 Report: Offline Integration Boundary

## Scope

Task 9 adds an offline integration harness around a disposable, real Git
fixture.  The fixture contains the production renderer, Pydantic decision
schema, adapter call assembly, prompt contract, split datasets, and history
assets.  Fixture helpers sequence the real production leaf scripts/modules;
they do not implement a second production `tune`/`verify` orchestrator or claim
to cover the overall `SKILL.md` state machine.  `CountingTransport` is test-only
and is supplied through the runner's explicit `client` boundary (with a
test-scoped constructor seam for the CLI config-boundary check).  A real
`transport: fake` value is written into the disposable project config, but it
cannot select that transport.  No LAN model or credential file is used.

## TDD evidence

The first focused run was intentionally RED: collection failed because the
new integration harness had not yet been added.  A second RED run collected
22 tests and exposed missing runtime-directory setup, line-ending-sensitive
fixture identity, and incorrect per-file Git status assertions.  After those
test-only fixes, the focused suite is GREEN:

```text
rtk conda run -n kds python -m pytest tests/test_integration_tune.py tests/test_integration_verify.py tests/test_behavior_contract.py -q
22 passed in 82.70s
```

## Coverage

- Real temporary Git repositories and isolated cycle worktrees.
- Tune happy delivery, contract/delivery confirmation gates, no-change exit,
  validation regression, acceptance failure and failure-only asset delivery.
- Equal-perfect baseline/candidate behavior, dirty unrelated and critical
  dependencies, patch conflict, rollback, and interrupted-slot resume.
- Verify development/validation/external execution, read-only acceptance
  rejection before file/client access, and the documented CLI chain through
  workspace, case, run, score, and comparison commands.
- Production renderer/schema markers, explicit test-only transport-boundary
  usage, a project `transport: fake` setting that cannot select the transport,
  per-file unstaged/untracked delivery checks, candidate isolation, complete
  slot call/attempt evidence, and credential redaction.  Fixed production-client
  identity and request settings are intentionally not claimed here; Task 4 and
  Task 10 provide that evidence.

## Verification

The focused suite above passes.  The repository-wide checks also pass:

```text
rtk conda run -n kds python -m pytest tests -q
168 passed in 169.34s

rtk conda run -n kds python C:/Users/kgcda/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
Skill is valid!

rtk conda run -n kds python -m ruff check tests/integration_support.py tests/test_integration_tune.py tests/test_integration_verify.py tests/test_behavior_contract.py tests/fixtures/target_repo/target_app/production.py
All checks passed!

rtk git diff --check
clean
```

No real model call was made and no real token was read or persisted; the only
credential-shaped value is a temporary sentinel used by the redaction test.
