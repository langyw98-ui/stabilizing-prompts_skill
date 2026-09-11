---
name: stabilizing-prompts
description: Use when a Python repository-backed Markdown prompt with Pydantic structured output is inconsistent across repeated model calls or needs a deterministic local-model regression suite.
---

# Stabilizing Prompts

Operate on one repository-relative `.md` prompt. Use only `conda run -n kds python` for Python execution. Stop when the target is outside the supported scope.

## tune

Create one isolated cycle, confirm the business contract and all cases before any model call, build a baseline, generate minimal candidates, run acceptance once, and deliver only after user confirmation.

Read the stable contracts in `references/` before operating:

- `business-contract.md` defines evidence precedence and the contract-confirmation gate.
- `case-schema.md` defines case sources, complete expected objects, and dataset isolation.
- `adapter-contract.md` defines the only project-specific execution boundary.
- `evaluation-method.md` defines deterministic scoring, error classes, metrics, and gates.
- `worktree-lifecycle.md` defines the two modes, evidence-driven loop, reports, and stop conditions.

Do not call the fixed model until the user has explicitly confirmed the contract, complete cases, dataset split, fixed environment, repetition counts, thresholds, and stop conditions. Keep candidates in the runtime area until the final delivery confirmation.

## verify

Run development, validation, or separately supplied non-acceptance cases without changing canonical assets. Never read or run `acceptance-cases.yaml`.

Use the fixed `kds` Conda environment and the production renderer, message assembly, Pydantic schema, and adapter contract. Report deterministic results and regressions without creating candidates or changing the prompt, contract, cases, adapter, or optimization history.
