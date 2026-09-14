# Case Schema and Dataset Contract

This reference preserves section 8 of the approved design.

## 8. 数据集生成

### 8.1 案例来源和维度

Skill 根据已确认业务契约生成案例，覆盖适用维度：

- 高频正常路径；
- 每个枚举值或业务动作；
- 必填、可空和条件字段组合；
- 近邻正例与近邻反例；
- 模糊输入及其明确兜底；
- 否定、冲突和复合条件；
- 空值、边界值和非法候选；
- 等价表达和上下文变化；
- 历史测试、故障和回归案例；
- 容易诱发 Schema 合法但业务错误的输入。

案例预期不能从目标模型的当前输出反推。每条案例至少记录：

```yaml
id: stable-case-id
semantic_family: routing-negative-example
source:
  - path/to/evidence.py
input:
  variables: {}
  context: {}
expect:
  output: {}
priority: normal
dimensions: []
rationale: why-this-result-is-correct
coverage:
  primary_obligation: classify-input
  secondary_obligations: []
  variant: normal
  condition_id: normal-classification
  distinction: null
```

`expect.output` 必须给出生产 Pydantic Schema 的完整预期对象，不支持只声明部分字段。案例在任何模型调用前通过同一个生产 Schema 的 `model_validate` 预验证；验证失败属于评测资产 `setup_error`，必须修正并重新确认案例。

每条案例必须包含 `coverage` 元数据：`primary_obligation` 是唯一的主要义务，
`secondary_obligations` 只能引用其他真实义务且不会贡献配额，`variant` 必须是
校验器规定的固定值，`condition_id` 必须是描述独立业务条件的稳定 slug；
疑似近重复或相近案例还必须提供实质性的 `distinction`。

### 8.1.1 覆盖义务资产

案例生成前，Codex 根据生产 Schema、生产控制流、业务文档、测试集合和已确认
历史故障建立 proposed/editable 资产：

```text
.prompt-evals/<prompt-id>/coverage-obligations.yaml
```

该文件声明稳定的 obligation ID、证据来源、风险、业务规则、每个 split 的
required variants，以及固定覆盖类别的 `required` 或有证据理由的
`not_applicable` 状态。`source` 必须指向仓库内证据；义务和 expected object
不能由目标模型输出反推。固定 variants 由校验器定义，案例的具体业务条件由
稳定的 `condition_id` 表达。

The proposed/editable asset is frozen by explicit user confirmation and
committed after the model probe/smoke at the asset-commit state. The committed
coverage obligation file and the three splits are then delivered through the
success and failure-asset-only allowlists. It is not an extra CLI argument,
confirmation file, or independent coverage-summary delivery asset.

### 8.1.2 机械覆盖与饱和确认

覆盖门禁按以下顺序执行：机械校验、Codex 证据扫描及
`saturation_statement`、用户确认、model probe。两层职责必须保持分离：

```text
validate_cases.py:
  validates schemas, counts, hard duplicates, explained near duplicates,
  quotas, category declarations, critical coverage, and matrix completeness

Codex + user:
  review scanned evidence, unregistered evidenced boundaries, near-duplicate
  distinctions, total call slots, and the saturation statement
```

机械校验只判断已声明义务和案例的 schema、数量、重复、配额、类别、critical
coverage 与矩阵完整性。Codex 再检查未登记的有证据边界和近重复差异，提出
饱和声明；用户必须审阅机械结果、证据范围、声明、全部案例和预计调用槽位后，
才能确认并允许第一次模型调用。确认记录绑定
`coverage_obligations_hash`、`case_suite_hash`、`evidence_checked`、
`saturation_statement` 和显式的近重复审阅确认/状态，仍属于周期状态而非
项目资产或 CLI 输入。任何冻结资产变化都会使确认和全部旧运行失效，必须重新
校验和确认。

### 8.2 开发集、验证集与验收集

- `dev-cases.yaml` 用于失败诊断和调优内循环；
- `validation-cases.yaml` 用于每轮完整候选选择；
- `acceptance-cases.yaml` 在首次模型运行前冻结，只在候选哈希冻结后执行一次最终评测活动；
- “一次终验”允许按每例默认 10 次重复，但终验结果不得反馈到本周期继续调优；
- `acceptance-cases.yaml` 只能由 `tune` 的终验步骤读取和运行；`verify` 在周期中或周期结束后都不得运行它；
- 若要复用验收案例，必须经用户确认后将其转入下一周期开发集，并为下一周期重新生成、确认和冻结验收集；
- 三个集合按业务维度和 `semantic_family` 划分，不能简单随机拆行；
- 案例 ID 在三个集合中全局唯一，同义或轻微改写案例不能跨集合形成明显泄漏；
- 这是一套冻结最终评测集，不宣称对负责生成案例和候选的 Codex 构成统计意义上的盲测；
- 调优循环不得删除、弱化或重标验证、验收案例；
- 终验失败即结束本周期。经用户确认后，暴露的问题可进入下一周期开发集，但下一周期必须重新生成、确认和冻结验收集。

## Case validation and split execution interface

During the internal `bootstrap` state of `tune`, validate all three files and
the production Schema before any model call:

```text
validate_cases.py --eval-root PATH --schema MODULE:CLASS --output CASE_SUITE_JSON
```

The input root contains `coverage-obligations.yaml`, `dev-cases.yaml`,
`validation-cases.yaml`, and `acceptance-cases.yaml`. `CASE_SUITE_JSON` records the validated cases,
complete expected-object serialization, split ownership, and the dataset hash.
An invalid expected object, duplicate ID, cross-split normalized input or
semantic-family collision, missing split, or schema import failure is an
asset `setup_error`; it must be repaired and reconfirmed rather than ignored.

After the contract confirmation gate, `run_prompt_eval.py` loads only the
selected split:

```text
run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|acceptance|external --repeats N --manifest PATH [--mode tune|verify]
```

The runner defaults to `--mode tune` for the final acceptance activity. A
read-only caller must pass `--mode verify`; that mode rejects `--dataset
acceptance` before opening any manifest or case file.

Development and validation runs therefore do not require or read the
acceptance file. Only the single final acceptance activity owned by `tune` may
select `--dataset acceptance`, and it does so after the candidate hash is
frozen. An interrupted final activity may resume its existing immutable slots,
but cannot create a second acceptance activity or feed its result back into
the current candidate loop. `verify` may select `dev`, `validation`, or a
separately supplied non-acceptance `external` case set only; it must reject
`--dataset acceptance` before opening any case file.
