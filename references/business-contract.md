# Business Contract

This reference preserves the business-contract rules from sections 7 and 7.1 of the approved design.

## 7. 工程理解与业务契约

Skill 不从 Prompt 文案单独推导正确答案。它按以下优先级收集证据：

1. 生产 Pydantic 结构化输出 Schema、枚举和字段约束；
2. Prompt 调用处及下游业务分支；
3. 工程业务文档和设计规格；
4. 现有测试、样例、故障用例和领域数据；
5. Prompt 中尚未被其他来源确认的规则。

Skill 生成 `prompt-contract.yaml`，至少包含：

- Prompt 相对路径和内容哈希；
- Prompt 用途；
- 输入变量、类型和来源；
- 会影响输出的上下文或状态；
- Schema 的导入位置和调用方式；
- 生产渲染器、消息组装入口和关键依赖文件；
- 字段类型、枚举、必填、可空和条件约束；
- 下游业务不变量；
- 错误、空结果和兜底语义；
- 每条业务规则的源码、测试或文档证据；
- 尚有冲突或无法确定的业务语义。

若证据冲突或无法形成确定真值，Skill 必须暂停并请求用户裁决。不得自行选择有利于当前 Prompt 的解释。

### 7.1 契约确认门禁

在调用本地模型或生成候选 Prompt 前，Skill 必须向用户展示：

- 业务契约摘要；
- 数据集覆盖矩阵；
- 每个案例的输入、完整预期对象和依据；
- 开发集、验证集与验收集的划分；
- 固定 Conda `kds` 环境、实际 Python 命令和 Python 版本；
- 默认重复次数、通过阈值和停止条件。

只有用户明确确认后，契约和验收预期才被冻结，Skill 才能进入基线评测。确认前不得调用测试模型。

调优中如发现契约或案例错误，Skill 必须停止当前实验，说明问题，等待用户确认修订；修订后旧运行失效并重新建立基线。

## Operational contract-confirmation state

The contract/cases/adapter package is a pre-model artifact. The confirmation
record must bind the canonical Prompt path and hash, Schema reference, renderer
and message-assembly entry points, critical dependency hashes, the canonical
`case_suite_hash`, adapter identity, the fixed `kds` command/Python version,
repetition counts, phase thresholds, stop conditions, `evidence_checked`,
`saturation_statement`, and explicit near-duplicate review confirmation/status.
A plain approval of the Prompt wording is not sufficient.

The `tune` state uses:

```text
validate_workspace.py --repo PATH --prompt REPO_RELATIVE_MD --mode tune --output WORKSPACE_JSON
validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON
```

`WORKSPACE_JSON` is a read-only repository snapshot. `CASE_SUITE_JSON` is the
validated, split-aware case summary. Its `case_file_hashes` values are exact
raw-byte SHA-256 hashes of the three YAML files; its `case_suite_hash` is a
canonical hash of validated cases, normalized production expected objects, and
split identity. The proposed contract,
complete expected objects, coverage matrix, and adapter boundary are shown to
the user together with those outputs. Only an explicit confirmation freezes
them and permits the fixed local-model probe. If evidence conflicts, a path or
dependency changes, or a user declines, stop without constructing the client
or generating a candidate; after any revision, invalidate old manifests and
rebuild the baseline.

The confirmation gate does not authorize production delivery. Acceptance
success and failure-asset-only synchronization each require a separate,
explicit delivery confirmation later in the same cycle.

## Coverage obligation evidence and confirmation

Before cases are generated, Codex derives the proposed/editable
`.prompt-evals/<prompt-id>/coverage-obligations.yaml` asset from production
evidence. The target model cannot create obligations or expected business
truth. The proposed/editable asset is reviewed with the contract, three case
splits, adapter, and evaluation configuration; it is frozen by explicit user confirmation and committed after the model probe/smoke at the asset-commit
state. The committed asset is then delivered by the existing success and
failure-asset-only allowlists.

The two coverage layers have separate owners:

```text
validate_cases.py:
  validates schemas, counts, hard duplicates, explained near duplicates,
  quotas, category declarations, critical coverage, and matrix completeness

Codex + user:
  review scanned evidence, unregistered evidenced boundaries, near-duplicate
  distinctions, total call slots, and the saturation statement
```

`validate_cases.py` is the mechanical gate for declared coverage. After it
passes, Codex scans the Schema, production branches, business contract,
historical failures, and input boundaries, and records a saturation statement
that no evidence-backed boundary remains unregistered. The user reviews that
statement and the mechanical audit separately before confirming the frozen
assets and permitting the model probe.

The confirmation record remains cycle state. It binds
`coverage_obligations_hash`, `case_suite_hash`, `evidence_checked`,
`saturation_statement`, and explicit near-duplicate review
confirmation/status in addition to the existing contract, adapter,
configuration, and environment identities; it is not a project asset,
confirmation file, coverage-summary asset, or CLI input. Any change to a
frozen asset invalidates the confirmation and all old runs, which requires a
fresh audit and user confirmation. The run manifest schema remains unchanged
and does not contain the obligation hash.
