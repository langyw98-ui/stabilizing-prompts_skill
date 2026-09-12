# Evaluation Method

This reference preserves sections 10 and 11 of the approved design.

## 10. 确定性断言与评分

Skill 不使用 LLM Judge。案例的完整 `expect.output` 先由生产 Pydantic Schema 构造为预期对象；模型结果由 `ChatOpenAI.with_structured_output` 构造后，从 `include_raw=True` 返回值的 `parsed` 字段取得同一 Pydantic 类型的实际对象。两者只比较生产 Schema 声明字段通过规范序列化得到的完整数据，并使用规范字段名（不使用别名）表示结果。

字段集合、类型、默认值、别名、必填、可空和结构约束全部由生产 Schema 决定，不另建断言 DSL。V1 不允许省略字段规避评分，也不提供自定义判定器。规范序列化数据不相等时，scorer 读取声明字段生成具体字段路径、期望值和实际值差异；字段展开只用于报告，不改变规范数据比较这一评分依据。`PrivateAttr`、缓存及其他 runtime-only state 不持久化、不计分，也不生成字段差异。

错误分成两层：

不参与 Prompt 评分：

1. `setup_error`：适配器导入、Prompt 渲染、Schema 定位或配置错误，以及 `ChatOpenAI` 或 OpenAI SDK 抛出的其他非重试异常，暂停运行；
2. `transport_error`：连接失败、30 秒超时、HTTP 408、429 或 5xx，对应槽位最多重试 2 次；
3. `protocol_error`：`ChatOpenAI` 明确报告服务响应不符合 Chat Completions 外层协议，或结构化调用正常返回但结果不是同时包含 `raw`、`parsed` 和 `parsing_error` 的字典，暂停运行。

参与 Prompt 评分：

1. `parse_error`：已取得原始 `AIMessage`，但缺少结构化 function call、目标载荷缺失或载荷无法解码；
2. `schema_error`：结构化载荷存在且可解码，但 `with_structured_output` 无法用生产 Pydantic Schema 构造对象；
3. `business_error`：实际与预期均为同一生产 Pydantic 类型，但声明字段的规范序列化数据不相等；
4. `pass`：实际与预期的声明字段规范序列化数据完全相等。

错误分类以明确证据为准。连接、超时、HTTP 408、429 或 5xx 进入可恢复的 `transport_error`；其他由 `ChatOpenAI` 或 OpenAI SDK 抛出的异常统一进入非评分 `setup_error`。结构化调用正常返回时，结果必须是同时包含 `raw`、`parsed` 和 `parsing_error` 的字典，否则进入 `protocol_error`。只有取得原始 `AIMessage` 后才进入 Prompt 评分：没有预期 function call 时记为 `parse_error: missing_payload`；拒答、截断或不可解码载荷记录为 `parse_error` 的对应细分原因；Pydantic `ValidationError` 中的 `json_invalid` 归为 `parse_error`，其他 Pydantic `ValidationError` 归为 `schema_error`，不得匹配异常文本分类。`message.content` 为 `None` 但 `tool_calls` 中存在可由生产 Schema 构造的结果时正常评分。

同一案例的多次结果中只要存在非 `pass`，该案例就不是完全稳定案例。错误类别应保留，不能统一折叠为失败。

## 11. 指标和默认门禁

核心指标：

```text
scored_responses
= parse_error + schema_error + business_error + pass

schema_valid_rate
= (business_error + pass) / scored_responses

run_accuracy
= pass / scored_responses

stable_case_rate
= 所有重复调用均通过的案例数 / 案例总数

regression_count
= 基线达到案例门禁、候选未达到门禁的案例数

stability_regression_count
= 基线全部重复通过、候选未全部重复通过的案例数
```

每次运行在开始时生成固定调用槽位，每个槽位由案例 ID、重复序号和 Prompt 哈希唯一标识。传输重试耗尽后槽位保持 `incomplete`，服务恢复后只能按原 manifest 补齐该槽位。所有计划槽位取得可评分响应前，不计算最终指标或门禁，不允许删除失败槽位或用额外成功调用替换指定槽位。

默认执行强度：

- 调优内循环：受影响开发案例每例 5 次；
- 开发集回归：全部开发案例每例 5 次；
- 验证集：全部验证案例每例 5 次；
- 最终验收：原 Prompt 与冻结候选对全部验收案例分别运行 10 次。

默认门禁分阶段定义。所有阶段共同要求 Schema 合法率为 100%、`critical` 案例全部通过，且任一失败都能追溯到案例、重复序号、错误分类和原始响应。

开发集门禁：

- 普通案例至少 4/5 通过；
- `regression_count` 和 `stability_regression_count` 均为 0。

验证集候选选择门禁：

- 普通案例至少 4/5 通过；
- `regression_count` 和 `stability_regression_count` 均为 0；
- 候选的 `run_accuracy` 和 `stable_case_rate` 必须同时不低于验证集基线，且至少一项严格提高。

验收集交付门禁：

- 普通案例至少 9/10 通过；
- `regression_count` 和 `stability_regression_count` 均为 0；
- 候选的 `run_accuracy` 和 `stable_case_rate` 必须同时不低于验收集中的原 Prompt，但不要求严格提高。

这些次数和阈值是本 Skill 的实用门禁，不代表统计显著性保证。每个 Prompt 可以在 `eval-config.yaml` 中声明更严格的重复次数或阈值。降低默认门禁必须在契约确认阶段由用户明确批准，调优循环不能自行降低。模型、地址、Token、`temperature=0.0`、关闭 thinking/reasoning/search、30 秒超时和 2 次重试不属于项目配置项。

每次运行还生成不可变 manifest，至少记录周期 ID、`cycle_base_commit`、Prompt、契约、配置、案例、适配器和 Skill 执行脚本哈希、固定环境名 `kds`、实际 Python 命令和 Python 版本、固定客户端报告的模型身份、移除 Authorization Token 后的固定 `ChatOpenAI` 与结构化调用配置、明确省略的采样字段、调用槽位计划及开始、恢复和完成时间。manifest 不记录各 Python 包版本。基线与候选比较时，除 Prompt 哈希和运行时间外，其他已记录且影响结果的字段必须兼容，否则拒绝比较并要求建立新基线。
