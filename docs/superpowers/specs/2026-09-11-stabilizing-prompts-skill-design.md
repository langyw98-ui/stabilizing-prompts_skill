# 通用结构化 Prompt 稳定性调优 Skill 设计

## 1. 背景与目标

Agent 在一次业务流程中可能多次调用 LLM。即使单次调用正确率较高，多次调用仍会放大不稳定性。现有开发流程主要依靠 Prompt 人工调试、代码单元测试和少量样例验证，缺少可重复执行的真实模型稳定性评测。

本设计新增个人级 Codex Skill：`stabilizing-prompts`。它面向 Python Git 工程中具有确定业务真值的结构化输出 Prompt，通过理解 Prompt 所在工程的真实业务、生成可审查的数据集、使用固定局域网模型重复执行、确定性评分和基线对比，自动产生并迭代候选 Prompt。

核心原则：

- 弱模型是固定压力测试模型，不是调参对象；
- 一次只调优一个 `.md` Prompt；
- `bootstrap` 和 `tune` 必须在专用 Git worktree 中运行；
- 评测复用工程现有 Prompt 渲染和结构化输出 Schema；
- 正确性优先于一致性，稳定错误不算通过；
- 测试预期在模型运行前由用户确认；
- 开发集、验证集和验收集职责分离，验收集每个周期只执行一次终验；
- 调优只修改候选 Prompt，不通过修改 Schema、断言或案例刷分；
- 原 Prompt 只有在验收通过且用户确认后才能在 worktree 中被替换；
- 最终成果以严格预检的补丁同步回原工作区，并保持未暂存、未提交。

## 2. 适用范围

### 2.1 支持范围

目标 Prompt 必须同时满足：

1. 位于 Python Git 工程内，且是单个 `.md` 文件；
2. 存在可定位或可由用户明确指定的结构化输出 Schema；
3. 输出具有确定的业务真值；
4. 结果能够通过代码断言判定正确或错误；
5. 工程中存在足够的调用链、业务代码、文档、测试或领域数据供 Codex 建立业务契约；
6. 工程能够在用户确认的 Python 环境中直接复用生产渲染器和 Schema。

典型场景包括分类、字段抽取、路由决策、工具选择、实体匹配、规则审核和结构化转换。

### 2.2 不支持范围

- 内联字符串、YAML 字段或代码常量中的 Prompt；
- 非 Python 工程；
- 一次共同调优多个 Prompt；
- 没有确定真值的故事、文风、人格化和其他开放式生成；
- 使用 LLM-as-Judge 给目标模型输出判分；
- 自动选择模型、切换模型、搜索模型参数或通过解码参数提高得分；
- 修改生产 Schema、业务逻辑或测试预期以迁就候选 Prompt；
- 将该 Skill 本体或局域网访问凭据复制到目标工程。

如果目标不符合支持范围，Skill 应说明原因并停止，不得退化为主观评审。

## 3. Skill 安装与组成

Skill 作为个人级 Codex Skill 安装在：

```text
C:\Users\kgcda\.agents\skills\stabilizing-prompts\
├── SKILL.md
├── agents\
│   └── openai.yaml
├── scripts\
│   ├── local_model_client.py
│   ├── validate_workspace.py
│   ├── validate_cases.py
│   ├── run_prompt_eval.py
│   ├── score_results.py
│   ├── compare_runs.py
│   └── manage_worktree.py
└── references\
    ├── business-contract.md
    ├── case-schema.md
    ├── adapter-contract.md
    ├── evaluation-method.md
    └── worktree-lifecycle.md
```

`SKILL.md` 只保留触发条件、核心约束、三种运行模式和对 supporting resources 的按需路由。确定性执行、数据校验和指标计算进入 `scripts/`；详细格式和方法进入 `references/`。

建议的 Skill 描述为：

```yaml
description: Use when a Python repository-backed Markdown prompt with structured output is inconsistent across repeated model calls or needs a deterministic local-model regression suite.
```

Skill 保持默认的隐式发现能力，也允许用户通过 `$stabilizing-prompts` 显式调用。

## 4. 固定测试模型

`scripts/local_model_client.py` 内固定以下连接信息：

- Chat Completions URL：`http://192.168.168.230:8000/v1/chat/completions`
- 模型：`dbirks/Qwen3.8-27B-W4A16-AutoRound`
- Authorization Token：使用用户提供的局域网固定值，直接写入该个人 Skill 的客户端代码。
- 单次请求超时：30 秒；
- 传输层失败最多重试 2 次，即每个调用槽位最多 3 次传输尝试。

客户端不得：

- 自动切换地址、模型或提供商；
- 自动尝试其他 Token；
- 从项目 `eval-config.yaml` 覆盖模型、地址、Token、超时或重试次数；
- 搜索或调整 `temperature`、`top_p` 等模型参数；
- 在命令、异常、日志、报告或目标工程文件中输出 Authorization 头或 Token。

默认请求不添加示例调用中不存在的采样参数。若未来需要固定附加参数，必须作为显式设计变更完成，而不是由调优循环自行决定。同一基线与候选对比必须使用完全相同的请求配置。

开始评测前执行一次连接、模型身份和最小结构检查。服务返回的模型身份与固定模型不一致时明确报告并停止，不得寻找替代模型。

## 5. 目标工程资产

每个使用该 Skill 的 Git 工程在仓库根目录建立一个 `.prompt-evals/`：

```text
.prompt-evals/
└── <prompt-id>/
    ├── prompt-contract.yaml
    ├── eval-config.yaml
    ├── dev-cases.yaml
    ├── validation-cases.yaml
    ├── acceptance-cases.yaml
    ├── adapter.py
    ├── optimization-history.yaml
    ├── reports/
    └── .runtime/
```

每个被调优的 Prompt 对应一个独立 `<prompt-id>` 子目录。`prompt-id` 使用“可读 slug + 规范路径 SHA-256 前 12 位”生成，例如：

```text
src/agent-a/prompts/classify.md
→ src--agent-a--prompts--classify--7c91e8a4d2f0
```

规则如下：

- Prompt 内容变化不改变 `prompt-id`；内容哈希记录在每次运行中；
- 规范路径取自 Git 记录的仓库相对路径，并统一使用 `/`；
- `prompt-contract.yaml` 保存完整规范路径，目录名不承担反向解码职责；
- 已存在目录记录的完整路径不一致时停止，不能静默复用；
- Prompt 移动或重命名时，Skill 必须提示迁移已有目录；
- 不得仅凭内容哈希自动迁移，因为不同 Prompt 可能具有相同内容；
- 不得因路径变化静默生成一套重复数据；
- 不同 Prompt 的契约、数据集、候选版本和报告完全隔离；
- `prompt-contract.yaml`、配置、数据集和适配器允许提交 Git；
- `optimization-history.yaml` 是可提交、可同步的精简失败知识；
- 原始响应、运行缓存和临时候选只进入 `reports/` 或 `.runtime/`，默认不提交 Git；
- 更新 `.gitignore` 时只添加所需的 `.prompt-evals/**/reports/` 和 `.prompt-evals/**/.runtime/` 规则，不覆盖用户已有内容。

### 5.1 Worktree 与交付边界

`bootstrap` 和 `tune` 为每个调优周期创建专用 worktree 和内部调优分支。周期开始前：

- 允许原工作区存在无关的未提交修改；
- 目标 Prompt、生产 Schema、渲染器、关键消息组装文件和自定义判定器必须与当前 `HEAD` 一致；
- Skill 不自动 stash、不复制相关脏文件，也不替用户创建临时提交；
- 已确认的契约、三套数据集、适配器和配置先在 worktree 中提交，再建立基线。

中间候选只作为 `.runtime/` 中的临时文件，不提交、不交付。最终验收通过并经用户确认后，Skill 在 worktree 中替换生产 Prompt，更新 `prompt-contract.yaml` 中的当前 Prompt 哈希，追加 `optimization-history.yaml` 并提交。随后根据周期基准提交到最终提交生成仅包含可交付资产的 Git 补丁。

同步回原工作区前必须执行严格补丁预检。补丁无法干净应用、目标文件已变化或待新增路径发生冲突时停止，不自动三方合并或覆盖。同步成功后的文件保持未暂存、未提交，并校验内容哈希。应用或校验异常时恢复同步前保存的目标文件状态。Skill 不自动删除 worktree 或内部调优分支。

默认同步的可交付资产只有：最终生产 Prompt、契约、配置、三套案例、`adapter.py`、`optimization-history.yaml` 和必要的 `.gitignore` 增量。原始响应、缓存、临时候选和完整运行报告不进入原工作区。

终验或调优周期失败时不交付任何候选或生产 Prompt 修改。Skill 可以在再次取得用户确认后，生成只包含已确认评测资产和本周期新增失败历史的补丁，并按相同预检规则同步回原工作区。该失败周期补丁允许包含契约、配置、三套案例、`adapter.py`、`optimization-history.yaml` 和必要的 `.gitignore` 增量，不包含原始响应、报告、缓存或临时候选。开启后续新周期前，用户必须自行审查并提交上一周期同步的资产；Skill 不替用户提交原工作区，也不能从未提交资产创建新的评测基线。

## 6. 单 Prompt 输入契约

每次运行的主输入是一个仓库内 `.md` 文件路径：

```text
target_prompt: <repository-relative-path-to-prompt.md>
```

Skill 在执行前验证：

- 当前目录属于 Git 仓库；
- 工程是可由已确认 Python 命令运行的 Python 工程；
- Prompt 位于仓库内且扩展名为 `.md`；
- 本轮只有一个目标 Prompt；
- 可以定位其加载和渲染逻辑；
- 可以定位真实结构化输出 Schema；
- 可以识别调用方式和下游消费路径；
- 可以识别影响评测的关键依赖文件及其 Git 状态。

若工程包含多个 Prompt，其他 Prompt 可以作为端到端上下文被读取，但不得在本轮修改、共同计分或共享测试数据。完成当前 Prompt 后才能启动另一个独立调优任务。

## 7. 工程理解与业务契约

Skill 不从 Prompt 文案单独推导正确答案。它按以下优先级收集证据：

1. 结构化输出 Schema、枚举和字段约束；
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
- 每个案例的输入、期望字段和依据；
- 开发集、验证集与验收集的划分；
- 自动探测到的候选 Python 命令及最终选择；
- 默认重复次数、通过阈值和停止条件。

只有用户明确确认后，契约和验收预期才被冻结，Skill 才能进入基线评测。确认前不得调用测试模型。

调优中如发现契约或案例错误，Skill 必须停止当前实验，说明问题，等待用户确认修订；修订后旧运行失效并重新建立基线。

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
  fields: {}
priority: normal
dimensions: []
rationale: why-this-result-is-correct
```

### 8.2 开发集、验证集与验收集

- `dev-cases.yaml` 用于失败诊断和调优内循环；
- `validation-cases.yaml` 用于每轮完整候选选择；
- `acceptance-cases.yaml` 在首次模型运行前冻结，只在候选哈希冻结后执行一次最终评测活动；
- “一次终验”允许按每例默认 10 次重复，但终验结果不得反馈到本周期继续调优；
- 三个集合按业务维度和 `semantic_family` 划分，不能简单随机拆行；
- 案例 ID 在三个集合中全局唯一，同义或轻微改写案例不能跨集合形成明显泄漏；
- 这是一套冻结最终评测集，不宣称对负责生成案例和候选的 Codex 构成统计意义上的盲测；
- 调优循环不得删除、弱化或重标验证、验收案例；
- 终验失败即结束本周期。经用户确认后，暴露的问题可进入下一周期开发集，但下一周期必须重新生成、确认和冻结验收集。

## 9. 项目专属适配器

`.prompt-evals/<prompt-id>/adapter.py` 是通用 Skill 与具体工程之间的唯一项目专属执行边界。

V1 只支持 Python 工程。Skill 自动探测 `.venv`、uv、Poetry 或工程文档声明的候选 Python 命令，向用户展示并确认最终命令，再冻结到 `eval-config.yaml`。运行中不得自动切换 Python 环境。

适配器必须复用：

- 工程现有的 Prompt 文件加载；
- 工程现有的模板渲染；
- 工程现有的结构化输出 Schema；
- 工程现有的 structured output、function calling 或 `response_format` 调用语义；
- 对评测结果有影响且可安全复用的消息组装逻辑。

适配器只替换模型客户端，使请求发送到固定局域网接口。它不得复制一套简化 Prompt、重新声明 Schema、修改生产配置或绕过关键消息上下文。

适配器提供以下最小接口：

```python
def prepare_call(prompt_path, case) -> dict:
    """复用生产加载、渲染和消息组装，返回本次调用所需信息。"""

def parse_response(raw_response, prepared_call):
    """复用生产 Schema，将原始响应解析为结构化值。"""
```

`prepare_call` 返回消息、生产 Schema 或 `response_format`，以及生产调用需要的非采样参数；模型、地址、Token、超时和重试仍由固定客户端控制。`parse_response` 必须调用生产 Schema，不得复制 Schema。需要复杂业务判断时，适配器可以额外提供 `check_result(parsed, case) -> list[dict]`。

通用 runner 负责调用槽位、固定客户端调用、原始响应保存、错误分类和默认字段比较。适配器 smoke test 必须证明原 Prompt 能通过该边界完成一次真实渲染、调用和解析。

如果工程调用链无法注入候选 Prompt 或固定客户端，Skill 可以在评测目录生成最薄的兼容层，但必须继续直接导入生产渲染器和 Schema。无法等价复现时应停止并说明差异，不能给出通过结论。

## 10. 确定性断言与评分

Skill 不使用 LLM Judge。所有结果先通过生产 Schema 解析，再用默认字段比较或项目自定义确定性判定器评分。

默认 scorer 只递归精确比较案例 `expect.fields` 中声明的字段，并输出具体字段路径、期望值和脱敏后的实际值。类型、必填、可空和结构约束由生产 Schema 负责，不另建断言 DSL。需要无序集合、数值容差、跨字段关系或多个合法答案时，由适配器的可选 `check_result` 处理。自定义判定器必须是确定性、无网络、无 LLM、无副作用的纯函数，并返回结构化失败原因。

错误分成两层：

不参与 Prompt 评分：

1. `setup_error`：适配器导入、Prompt 渲染、Schema 定位或配置错误，暂停运行；
2. `transport_error`：连接失败、30 秒超时、HTTP 408、429 或 5xx，对应槽位最多重试 2 次；
3. `protocol_error`：服务响应不符合 Chat Completions 外层协议，暂停运行。

参与 Prompt 评分：

1. `parse_error`：存在有效模型响应，但目标结构化载荷缺失或无法解码；
2. `schema_error`：载荷已解码，但不符合生产 Schema；
3. `business_error`：Schema 合法但业务断言失败；
4. `pass`：所有断言通过。

整个 HTTP 响应或协议 envelope 为 `None` 时是 `protocol_error`。`message.content` 为 `None` 但合法结果位于 `tool_calls` 或 function arguments 时正常提取；不存在预期载荷时是带 `missing_payload` 或 `null_output` 原因的 `parse_error`。JSON `null` 交给生产 Schema 判断。拒答或截断作为 `parse_error` 的细分原因记录。

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

默认门禁：

- Schema 合法率为 100%；
- `critical` 案例在所有阶段必须全部通过；
- 普通案例在开发集和验证集中至少 4/5 通过，在最终验收中至少 9/10 通过；
- 基线中完全稳定的案例不得退化；
- 候选的 `run_accuracy` 和 `stable_case_rate` 必须同时不低于基线，且至少一项严格提高，除非基线已满足全部目标；
- `regression_count` 必须为 0；
- `stability_regression_count` 必须为 0；
- 任一失败必须能够追溯到案例、重复序号、错误分类和原始响应。

这些次数和阈值是本 Skill 的实用门禁，不代表统计显著性保证。每个 Prompt 可以在 `eval-config.yaml` 中声明更严格的重复次数或阈值。降低默认门禁必须在契约确认阶段由用户明确批准，调优循环不能自行降低。模型、地址、Token、30 秒超时和 2 次重试不属于项目配置项。

每次运行还生成不可变 manifest，至少记录周期 ID、worktree 基准提交、Prompt、契约、配置、案例、适配器和 Skill 执行脚本哈希、实际 Python 命令和版本、固定客户端报告的非敏感模型身份、调用槽位计划及开始、恢复和完成时间。基线与候选比较时，除 Prompt 哈希和运行时间外，其他影响结果的字段必须兼容，否则拒绝比较并要求建立新基线。

## 12. 三种运行模式

### 12.1 `bootstrap`

用于首次建立评测资产：

1. 验证 Python Git 工程中的单个 `.md` Prompt；
2. 追踪渲染、Schema、调用、下游业务和关键依赖；
3. 验证相关文件 Git 状态并创建专用 worktree；
4. 生成业务契约、开发集、验证集、验收集和覆盖矩阵；
5. 探测 Python 命令并生成或验证项目适配器；
6. 向用户展示契约、三套案例、执行命令、重复次数和门禁并等待确认；
7. 确认后执行连接、模型身份和适配器真实 smoke test，只使用开发案例，不接触验收结果；
8. 提交已确认且通过 smoke test 的评测资产，建立评测周期；
9. 只对原 Prompt 运行开发集和验证集基线；
10. 保存基线摘要和失败聚类，验收集暂不运行。

### 12.2 `tune`

用于自动产生候选 Prompt：

1. 读取已确认的契约、数据集和基线；
2. 读取 `optimization-history.yaml`，按错误类别和业务维度聚类失败；
3. 一次选择一个有证据的失败簇；
4. 从原 Prompt 或上一候选生成最小修改候选；
5. 将候选写入 `.runtime/`，不覆盖原 Prompt；
6. 重跑受影响的开发案例；
7. 通过后重跑完整开发集；
8. 只有完整开发集不退化时才运行验证集；
9. 根据验证结果选择最终候选并冻结哈希；
10. 在同一最终评测活动中，分别对原 Prompt 和冻结候选运行验收集；
11. 终验失败时结束本周期，不得根据结果继续修改本周期候选；经用户再次确认后，只能将已确认评测资产和新增失败历史同步回原工作区；
12. 终验通过后请求用户确认是否交付最终 Prompt；
13. 确认后在 worktree 中替换生产 Prompt，更新契约中的当前 Prompt 哈希，提交最终结果，并将可交付资产补丁同步回原工作区。

默认最多进行 5 轮候选迭代。连续两轮没有指标改善、出现业务契约冲突、适配器失真、模型不可用或达到最大轮数时停止并报告，不能无限循环。

交付生产 Prompt 是独立的最终用户确认动作。同步前后通过候选哈希、worktree 提交和补丁内容证明落盘内容就是已经终验的候选，不在同步后重新打开本周期调优。

### 12.3 `verify`

用于只读验证：

- 默认直接在当前工作区执行，不创建 worktree；
- 校验评测资产和适配器；
- 对指定的原 Prompt 或候选 Prompt 重复执行；
- 计算确定性指标；
- 与已保存基线比较；
- 输出通过、失败和回归明细；
- 不创建新候选，不修改 Prompt、契约、案例、适配器或优化历史；
- 可以写入 Git 已忽略的报告和缓存；
- 关键依赖存在未提交修改时明确提示，经用户确认后可继续，并在 manifest 中记录当前文件哈希。

## 13. 证据驱动的调优循环

以下循环是本 Skill 针对 Prompt 稳定性定义的产品方法，不是 Skill Creator 规定的固定流程：

### 建立基线

- 在修改 Prompt 前生成并确认业务场景；
- 使用原 Prompt 运行固定模型的开发集和验证集；
- 观察并保存真实失败和不稳定输出；
- 若基线没有暴露待解决问题，不为假设性问题修改 Prompt。

### 最小修正

- 只针对已观察到的一个失败簇做最小 Prompt 修改；
- 用相同模型、请求行为、案例和调用计划验证修改；
- 不通过扩大 Prompt、修改 Schema、弱化预期或添加无证据规则追求表面覆盖；
- 中间候选只作为运行时临时文件，不进入可交付资产。

### 验证与冻结

- 先运行受影响开发案例，再运行完整开发集和验证集；
- 发现新退化时回到最小修改，而不是弱化断言；
- 最多迭代五轮，连续两轮无改善时停止；
- 最终候选按验证集结果选定并冻结哈希后，才执行一次最终验收活动；
- 终验结果不反馈到本周期。失败时结束周期，必要经验写入 `optimization-history.yaml`。

## 14. 报告格式

每次运行报告至少包含：

- 仓库、Prompt 相对路径和 Prompt 哈希；
- 运行模式、时间和固定模型名；
- 请求参数的非敏感摘要；
- 数据集版本或哈希；
- 每个案例的重复次数、通过次数和错误分类；
- Schema 合法率、单次正确率、完全稳定案例率；
- `regression_count`、`stability_regression_count` 及对应案例；
- 基线与候选的修复、退化和未变化案例；
- 候选 Prompt diff（适用时）；
- 停止原因；
- 是否达到交付门禁。

报告不得包含 Token、Authorization 头或未经必要脱敏的其他凭据。

`reports/` 中的原始响应和完整运行报告只保留在 worktree，不提交、不默认同步。可交付的 `optimization-history.yaml` 按周期追加以下精简信息：

- 周期、基线 Prompt、数据集、配置和 manifest 哈希；
- 失败簇、案例 ID、错误分类和脱敏后的字段差异；
- 每轮修改意图、指标变化、修复与回归案例和淘汰原因；
- 最终停止原因和尚未解决的问题；
- 经用户确认、应在下一周期转入开发集的真实故障。

`optimization-history.yaml` 不保存中间候选全文、原始响应、Token 或未脱敏数据。新周期必须读取历史记录以避免重复无效策略，但当前已确认的契约和案例始终是业务真值来源。基础设施故障只进入运行报告，不作为 Prompt 优化经验。

## 15. 错误处理

- 模型服务不可用或身份不匹配：暂停，不切换模型；
- Schema 无法定位：请求用户提供位置，不能猜测替代 Schema；
- Prompt 渲染、适配器导入或 Python 环境失败：记录 `setup_error` 并暂停，不用简化路径继续；
- 业务证据冲突：暂停契约确认；
- 单次传输失败：记录明确原因，最多重试 2 次；仍失败时槽位保持 `incomplete`，不按 Prompt 失败计分；
- 服务响应 envelope 不合法：记录 `protocol_error` 并暂停；
- 候选输出无法解析或验证：记录 `parse_error` 或 `schema_error`，不得用文本猜测结构；
- 评测资产、Prompt 路径或 manifest 失配：提示迁移或重建基线，不能混用旧数据；
- 契约、案例、Python 命令、门禁、适配器、生产关键依赖或固定客户端请求行为变化：结束旧周期并重新建立基线；
- 最终验收失败：结束本周期，不交付失败候选；
- 失败周期只有在再次获得用户确认后，才能向原工作区同步已确认评测资产和新增的 `optimization-history.yaml` 记录；
- 补丁预检冲突：停止，不自动合并或覆盖；
- 补丁应用或哈希校验异常：恢复同步前保存的目标文件状态；
- 用户取消：保留原工作区和生产 Prompt，不进行最终同步。

## 16. Skill 自身的测试方法

目标 Prompt 评测与 Skill 自身测试是两套不同测试：

1. 目标 Prompt 评测使用固定局域网 27B 和确定性断言；
2. Skill 自身通过结构校验、离线脚本测试、临时 Python 工程集成测试、真实局域网 smoke test 和 Codex 行为前向测试验证。

结构校验运行 Skill Creator 的 `quick_validate.py`，但该工具只检查 Skill 结构、名称、frontmatter 和未完成占位符，不作为功能通过证明。

离线脚本测试使用临时 Git 仓库和仅供测试注入的伪造 HTTP transport，至少覆盖：

- `prompt-id` 生成和路径碰撞；
- 三套案例的 ID、语义族和格式校验；
- 默认字段递归比较和自定义判定器边界；
- `None`、解析失败、Schema 失败和业务失败分类；
- 30 秒超时、最多 2 次重试、固定调用槽位中断与恢复；
- manifest 兼容性和两类回归计算；
- Token、Authorization 和敏感字段脱敏；
- worktree 创建、补丁预检、冲突停止和同步恢复；
- 中间候选不会进入提交或同步资产。

测试 transport 不能通过项目配置进入生产运行路径。生产客户端仍只允许固定局域网模型。

Python 工程集成测试在隔离的临时仓库中验证生产渲染器和 Schema 导入、Python 命令确认、相关与无关脏文件处理、`bootstrap → tune → final sync`、未暂存未提交的同步结果，以及 `verify` 不修改规范资产。

发布前必须使用固定局域网模型完成至少一次端到端 smoke test，确认模型身份、structured output 兼容性、超时、响应规范化、基线与候选请求一致性和凭据脱敏。日常离线测试不依赖局域网服务。

Codex 行为前向测试使用现实请求，且不给评测者预期答案、已知缺陷或建议修复。至少覆盖：

- 一个具有明确 Schema 和调用链的正常 Python 工程；
- 一个包含多个 Prompt、要求只处理指定文件的工程；
- 一个业务证据冲突、必须暂停的工程；
- 一个开放式输出 Prompt、必须拒绝自动调优的工程；
- 一个候选提高局部分数但引入回归、必须拒绝交付的工程；
- 一个终验失败、必须结束周期的工程；
- 一个终验失败后只同步已确认评测资产和失败历史、不交付候选的工程；
- 一个模型服务异常、不得切换模型或泄漏 Token 的工程；
- 一个原工作区存在同步冲突、不得覆盖用户修改的工程。

任何 Skill 测试若使用子代理，必须在该次编排运行开始前按当前全局策略取得一次用户确认；该运行中的所有子代理显式使用 `gpt-5.6-luna` 和 `max` reasoning，并报告可审计的 spawn 参数。

所有新增或修改的脚本必须真实执行验证，不能只检查文案或文件存在。

## 17. 文件与职责边界

| 位置 | 职责 | 不得承担 |
|---|---|---|
| 个人 Skill `SKILL.md` | 触发、决策、工作流和门禁 | 项目业务规则、Token 回显、具体 Prompt 内容 |
| `scripts/local_model_client.py` | 固定局域网调用、30 秒超时、2 次重试和响应规范化 | 项目配置覆盖、模型选择、参数搜索、报告业务结论 |
| 通用 runner/scorer | 固定槽位、恢复执行、默认字段比较、指标和基线比较 | 推断业务真值、修改验收预期、实现通用断言语言 |
| `scripts/manage_worktree.py` | 隔离 worktree、同步补丁预检和哈希校验 | 自动合并、覆盖用户修改、提交原工作区 |
| 项目 `prompt-contract.yaml` | 已确认业务契约和证据 | 模型生成的未经确认结论 |
| 项目三套数据集 | 输入、预期、优先级、维度和语义族 | 调优中自动迁就候选、跨集合放置近邻案例 |
| 项目 `adapter.py` | 复用生产渲染和 Schema，可选提供纯函数判定器 | 复制简化 Prompt、重定义 Schema、选择模型 |
| `optimization-history.yaml` | 精简、脱敏、可复用的失败知识 | 中间候选全文、原始响应、基础设施故障 |
| `.runtime/` | 临时候选、调用槽位状态和缓存 | 提交或同步到原工作区 |
| `reports/` | 原始运行和对比证据 | 凭据、Authorization 头 |

## 18. 验收标准

设计实现完成必须满足：

1. Skill 可在不同 Python Git 仓库中对任意单个 `.md` 结构化 Prompt 启动，不包含任何具体 Agent 的业务字段；
2. 每个仓库只建立一个 `.prompt-evals/`，每个 Prompt 建立一个稳定 `prompt-id` 子目录；
3. Skill 能从真实工程调用链生成有证据的业务契约和确定性测试集；
4. 用户确认契约和案例前不调用本地模型；
5. `bootstrap` 和 `tune` 在专用 worktree 中运行，允许无关脏文件但拒绝未提交的关键依赖；
6. 评测通过已确认的 Python 环境和最小适配器复用生产 Prompt 渲染与 Schema；
7. 模型地址、名称、Token、30 秒超时和 2 次重试固定在个人 Skill 客户端中，且 Token 不出现在日志、报告或目标工程；
8. 原 Prompt、候选 Prompt 使用相同固定请求行为和兼容 manifest；
9. 开发集、验证集和验收集隔离，验收结果不反馈到本周期；
10. 正确性、Schema 合法性、稳定性、门禁回归和完全稳定性回归分别计量；
11. 传输失败不计入 Prompt 指标，固定槽位可恢复补齐，未完成时不形成结论；
12. Skill 不使用 LLM Judge，不处理无法确定判分的 Prompt；
13. 自动调优只写临时候选，未经用户最终确认不在 worktree 中替换生产 Prompt；
14. 最终只把白名单资产安全同步到原工作区，并保持未暂存、未提交；
15. 冻结验收集不得在调优循环中自动修改或重复用于同周期调优；
16. `optimization-history.yaml` 保留必要失败知识，但不包含中间候选全文或原始响应；
17. 模型不可用、契约冲突、回归、无改善、终验失败或同步冲突时能按明确停止条件退出；
18. 失败周期未经再次确认不修改原工作区；确认后也只能同步已确认评测资产和新增失败历史；
19. 开启新周期前，上一周期同步的评测资产必须已经由用户提交到目标仓库；
20. Skill 自身通过 `quick_validate.py`、离线脚本测试、临时工程集成测试、真实局域网 smoke test 和 Codex 行为前向测试。

## 19. 明确决策

- 这是通用 Prompt 调优 Skill，不是百科专属工具；
- 百科可作为首个真实验证工程，但其字段、路径和规则不得进入 Skill 本体；
- V1 专门面向 Python Git 工程，不设计跨语言适配器协议；
- 所有目标 Prompt 都是单独的 `.md` 文件；
- 所有目标调用都采用结构化输出；
- 一次只处理一个 Prompt；
- 允许生成项目专属适配器，且必须复用工程现有渲染和 Schema；
- 固定局域网模型不由 Skill 调整；
- 局域网 Token 经用户明确授权直接写入个人 Skill 代码；
- 模型连接、30 秒超时和 2 次重试不能由项目配置覆盖；
- 业务契约、三套数据集、Python 命令和门禁必须在基线运行前由用户确认；
- Skill 可以自动迭代临时候选，但不持久化或交付中间候选；
- 最终验收每周期只运行一次，失败后继续调优必须建立新周期和新验收集；
- 终验失败不交付候选或生产 Prompt 修改；经用户再次确认后，可以同步已确认评测资产和新增失败历史；
- `bootstrap` 和 `tune` 使用专用 worktree，`verify` 默认直接在当前工作区只读运行；
- 最终成果同步回原工作区但不暂存、不提交，冲突时不自动合并；
- 每个工程使用根目录 `.prompt-evals/`，每个 Prompt 使用“可读 slug + 12 位路径哈希”的独立子目录。
