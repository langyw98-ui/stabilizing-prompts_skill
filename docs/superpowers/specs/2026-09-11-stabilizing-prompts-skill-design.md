# 通用结构化 Prompt 稳定性调优 Skill 设计

## 1. 背景与目标

Agent 在一次业务流程中可能多次调用 LLM。即使单次调用正确率较高，多次调用仍会放大不稳定性。现有开发流程主要依靠 Prompt 人工调试、代码单元测试和少量样例验证，缺少可重复执行的真实模型稳定性评测。

本设计新增个人级 Codex Skill：`stabilizing-prompts`。它面向 Python Git 工程中具有确定业务真值的结构化输出 Prompt，通过理解 Prompt 所在工程的真实业务、生成可审查的数据集、使用固定局域网模型重复执行、确定性评分和基线对比，自动产生并迭代候选 Prompt。

核心原则：

- 弱模型是固定压力测试模型，不是调参对象；
- Python 运行环境固定为本机 Conda `kds` 环境，不探测或切换其他环境；
- 一次只调优一个 `.md` Prompt；
- `tune`（包括首次初始化阶段）必须在同一个专用 Git worktree 和调优周期中连续运行；
- 评测复用工程现有 Prompt 渲染、消息组装和 Pydantic 结构化输出 Schema；
- 正确性优先于一致性，稳定错误不算通过；
- 测试预期在模型运行前由用户确认；
- 开发集、验证集和验收集职责分离，验收集每个周期只允许由 `tune` 执行一次终验；
- 调优只修改候选 Prompt，不通过修改 Schema、断言或案例刷分；
- 原 Prompt 只有在验收通过且用户确认后才能在 worktree 中被替换；
- 最终成果以严格预检的补丁同步回原工作区，并保持未暂存、未提交。

## 2. 适用范围

### 2.1 支持范围

目标 Prompt 必须同时满足：

1. 位于 Python Git 工程内，且是单个 `.md` 文件；
2. 存在可定位或可由用户明确指定的生产 Pydantic `BaseModel` 结构化输出 Schema，并能在当前 Conda `kds` 环境中导入；
3. 输出具有确定的业务真值；
4. 结果能够通过代码断言判定正确或错误；
5. 工程中存在足够的调用链、业务代码、文档、测试或领域数据供 Codex 建立业务契约；
6. 工程能够通过固定命令 `conda run -n kds python` 直接复用生产渲染器和 Schema。

典型场景包括分类、字段抽取、路由决策、工具选择、实体匹配、规则审核和结构化转换。

### 2.2 不支持范围

- 内联字符串、YAML 字段或代码常量中的 Prompt；
- 非 Python 工程；
- 不能在本机 Conda `kds` 环境中导入或运行所需生产代码的工程；
- 一次共同调优多个 Prompt；
- 没有确定真值的故事、文风、人格化和其他开放式生成；
- 使用 LLM-as-Judge 给目标模型输出判分；
- 自动选择模型、切换模型、搜索模型参数或通过解码参数提高得分；
- 修改生产 Schema、业务逻辑或测试预期以迁就候选 Prompt；
- 将该 Skill 本体或局域网访问凭据复制到目标工程。

如果目标不符合支持范围，Skill 应说明原因并停止，不得退化为主观评审。

### 2.3 交付威胁模型与非目标

交付同步采用本地单用户、非对抗性威胁模型。它保护个人使用中常见的误操作、并发编辑、过期周期状态、路径错误、意外变更文件、补丁无法应用以及应用或校验失败导致的数据损坏；它不试图抵抗能够任意修改 Skill 代码、Git 仓库、周期状态、补丁、补丁清单或哈希的本地攻击者。该边界不承诺消除所有理论上的 TOCTOU 间隔。

因此，交付实现不使用仓库锁或密码学信任锚，不构造自定义的完整 Git 补丁解析器，也不把持久化补丁或哈希清单当作敌对环境下的证明。交付时从可信的 `cycle_base_commit`、当前周期 worktree `HEAD` 和已提交目标内容重新生成补丁；使用 Git 报告的实际变更路径核对派生白名单和补丁路径；应用前立即复核原工作区 `HEAD` 与提交版本的规范 `prompt-contract.yaml` 中的 canonical Prompt path；应用后校验实际路径和内容哈希，任何失败都恢复精确目标快照。

## 3. Skill 安装与组成

Skill 作为个人级 Codex Skill 安装在 `$CODEX_HOME/skills`；未设置 `CODEX_HOME` 时使用 `~/.codex/skills`：

```text
~/.codex/skills/stabilizing-prompts/
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

`SKILL.md` 只保留触发条件、核心约束、两种用户运行模式、`tune` 的内部初始化阶段和对 supporting resources 的按需路由。确定性执行、数据校验和指标计算进入 `scripts/`；详细格式和方法进入 `references/`。

局域网凭据不属于受跟踪的 Skill 源码。Skill 根目录可以由用户手动建立
`.local/model-credentials.json`，并且根目录 `.gitignore` 必须精确排除该文件。
安装 Skill 时不得隐式复制该文件；没有该文件时由固定客户端报告非计分的
`setup_error`。

建议的 Skill 描述为：

```yaml
description: Use when a Python repository-backed Markdown prompt with Pydantic structured output is inconsistent across repeated model calls or needs a deterministic local-model regression suite.
```

Skill 保持默认的隐式发现能力，也允许用户通过 `$stabilizing-prompts` 显式调用。发布验证必须证明 Codex 能从上述个人 Skill 目录隐式发现并显式调用它。

## 4. 固定测试模型

`scripts/local_model_client.py` 使用 `langchain_openai.ChatOpenAI`，并固定以下连接与调用信息：

- `base_url`：`http://192.168.168.230:8000/v1`，由 `ChatOpenAI` 调用其 `/chat/completions` 路径；
- 模型：`dbirks/Qwen3.8-27B-W4A16-AutoRound`；
- Authorization Token：只从 Skill 根目录 `.local/model-credentials.json` 读取；该文件的 JSON 对象只能包含一个非空字符串字段 `authorization_token`，且文件不可跟踪；
- `temperature=0.0`；
- `extra_body={"enable_thinking": False, "enable_reasoning": False, "enable_search": False}`；
- 单次请求超时：30 秒；
- `max_retries=2`，即每个调用槽位最多 3 次传输尝试。

客户端不得：

- 自动切换地址、模型或提供商；
- 自动尝试其他 Token；
- 从项目 `eval-config.yaml` 覆盖模型、地址、Token、超时或重试次数；
- 搜索或调整 `temperature`、`top_p`、seed 等模型参数；`temperature` 只能使用固定值 `0.0`，其他采样参数不得发送；
- 在命令、异常、日志、报告或目标工程文件中输出 Authorization 头或 Token。

固定客户端不得从项目配置或候选循环继承采样参数。模型端点、模型名称、超时、重试次数和生成参数全部固定于受跟踪代码，项目配置不能覆盖。参数的固定值或明确省略都属于请求契约；manifest 记录移除 Authorization Token 后的固定客户端配置，并明确记录未发送的采样字段。同一基线与候选对比必须使用完全相同的客户端和结构化调用配置。凭据文件缺失、不可读、JSON 畸形、字段为空或包含额外字段时，停止并记录非计分的 `setup_error`。

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

`tune` 为每个调优周期创建一个专用 worktree 和内部调优分支。`cycle_base_commit` 固定为创建该 worktree 时原工作区的 `HEAD`。首次运行缺少有效评测资产时，`tune` 在该 worktree 中先完成内部初始化阶段，再于同一连续流程、同一 worktree 和同一周期中直接进入基线与候选调优；初始化阶段不是可单独退出后再恢复的用户运行模式。周期开始前：

- 允许原工作区存在无关的未提交修改；
- 目标 Prompt、生产 Pydantic Schema、渲染器和关键消息组装文件必须与当前 `HEAD` 一致；
- Skill 不自动 stash、不复制相关脏文件，也不替用户创建临时提交；
- 已确认的契约、三套数据集、适配器和配置先在 worktree 中提交，再建立基线。

中间候选只作为 `.runtime/` 中的临时文件，不提交、不交付。最终验收通过并经用户确认后，Skill 在 worktree 中替换生产 Prompt，更新 `prompt-contract.yaml` 中的当前 Prompt 哈希（只允许更新当前 Prompt 的非路径字段），追加 `optimization-history.yaml` 并提交。随后从 `cycle_base_commit` 到当前周期 worktree `HEAD` 在交付时重新生成差异，并通过交付白名单过滤为 Git 补丁。评测资产提交和最终结果提交的名称及数量属于实现细节。

同步回原工作区前，先以 Git 的 `--name-status -z` 或 `--name-only -z`（或等价原生命令）得到实际变更路径，再确认它们严格等于本次选择的可交付路径；补丁中的每个 section/文件路径也必须与该集合完全一致，任何额外路径一律停止。预检必须立即确认原工作区 `HEAD == cycle_base_commit`，并确认提交版本的 `prompt-contract.yaml` 仍指向周期开始时的 canonical Prompt path；只允许当前 Prompt hash 等非路径字段变化。随后执行 `git apply --check`，对精确目标做快照，在不暂存的情况下应用补丁，再校验实际路径和内容哈希。补丁无法干净应用、目标文件已变化或待新增路径发生冲突时停止，不自动三方合并或覆盖；应用或校验异常时恢复同步前保存的精确目标状态。同步成功后的文件保持未暂存、未提交。Skill 不自动删除 worktree 或内部调优分支。

默认同步的可交付资产只有：最终生产 Prompt、契约、配置、三套案例、`adapter.py`、`optimization-history.yaml` 和必要的 `.gitignore` 增量。原始响应、缓存、临时候选和完整运行报告不进入原工作区。

终验或调优周期失败时不交付任何候选或生产 Prompt 修改。Skill 可以在再次取得用户确认后，从 `cycle_base_commit` 到当前评测资产提交生成差异，通过交付白名单过滤出已确认评测资产和本周期新增失败历史的补丁，并按相同预检规则同步回原工作区。该失败周期补丁允许包含契约、配置、三套案例、`adapter.py`、`optimization-history.yaml` 和必要的 `.gitignore` 增量，不包含原始响应、报告、缓存或临时候选。开启后续新周期前，用户必须自行审查并提交上一周期同步的资产；Skill 不替用户提交原工作区，也不能从未提交资产创建新的评测基线。

## 6. 单 Prompt 输入契约

每次运行的主输入是一个仓库内 `.md` 文件路径：

```text
target_prompt: <repository-relative-path-to-prompt.md>
```

Skill 在执行前验证：

- 当前目录属于 Git 仓库；
- 工程可由固定的 `conda run -n kds python` 运行，并可在该环境中导入 `langchain_openai.ChatOpenAI` 和生产 Pydantic Schema；
- Prompt 位于仓库内且扩展名为 `.md`；
- 本轮只有一个目标 Prompt；
- 可以定位其加载和渲染逻辑；
- 可以定位真实的 Pydantic `BaseModel` 结构化输出 Schema；
- 可以识别调用方式和下游消费路径；
- 可以识别影响评测的关键依赖文件及其 Git 状态。

若工程包含多个 Prompt，其他 Prompt 可以作为端到端上下文被读取，但不得在本轮修改、共同计分或共享测试数据。完成当前 Prompt 后才能启动另一个独立调优任务。

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
```

`expect.output` 必须给出生产 Pydantic Schema 的完整预期对象，不支持只声明部分字段。案例在任何模型调用前通过同一个生产 Schema 的 `model_validate` 预验证；验证失败属于评测资产 `setup_error`，必须修正并重新确认案例。

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

## 9. 项目专属适配器

`.prompt-evals/<prompt-id>/adapter.py` 是通用 Skill 与具体工程之间的唯一项目专属执行边界。

V1 只支持能在本机 Conda `kds` 环境中运行的 Python 工程，固定命令为 `conda run -n kds python`，不探测 `.venv`、uv、Poetry 或其他 Python 环境。设计和发布验证以当前环境中的 Python `3.14.6` 与 Pydantic `2.13.4` 为基准；`kds` 环境后续变更由用户管理，Skill 不在 manifest 中记录或比较各 Python 包版本。

适配器必须复用：

- 工程现有的 Prompt 文件加载；
- 工程现有的模板渲染；
- 工程现有的 Pydantic Schema 和 `with_structured_output(..., method="function_calling")` 调用语义；
- 对评测结果有影响且可安全复用的消息组装逻辑。

适配器只把生产消息组装和生产 Pydantic Schema 暴露给通用 runner。runner 使用固定 `ChatOpenAI`，通过 `with_structured_output(ProductionSchema, method="function_calling", include_raw=True)` 向局域网接口发起评测。适配器不得复制一套简化 Prompt、重新声明 Schema、修改生产配置或绕过关键消息上下文。

适配器提供以下最小接口：

```python
def prepare_call(prompt_path, case) -> dict:
    """复用生产加载、渲染和消息组装，返回消息与生产 Pydantic Schema。"""
```

`prepare_call` 返回 `messages` 和 `schema`；`schema` 必须是生产 Pydantic `BaseModel` 类型。模型、地址、Token、`temperature=0.0`、关闭 thinking/reasoning/search、超时、重试和 `include_raw=True` 均由固定客户端与通用 runner 控制。`include_raw=True` 只保留 LangChain 的原始消息与解析错误，不改变发给模型的 function calling 请求。V1 不支持 `parse_response`、`check_result`、部分字段断言、数值容差、无序集合特殊比较或多个合法预期。

通用 runner 负责调用槽位、固定 `ChatOpenAI` 调用、原始 `AIMessage` 保存、错误分类和生产 Schema 声明字段的规范序列化比较。适配器 smoke test 必须证明原 Prompt 能通过该边界完成一次真实渲染、function calling 和生产 Schema 实例化。

如果工程调用链无法注入候选 Prompt 或固定客户端，Skill 可以在评测目录生成最薄的兼容层，但必须继续直接导入生产渲染器和 Schema。无法等价复现时应停止并说明差异，不能给出通过结论。

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

## 12. 两种用户运行模式

### 12.1 `tune`

用于在一个专用 worktree 和调优周期中连续建立或复用评测资产、建立基线并自动产生候选 Prompt。缺少有效评测资产时，`tune` 先执行内部初始化阶段；该阶段不是可独立调用或退出后再由另一次 `tune` 恢复的用户模式。

完整流程：

1. 验证 Python Git 工程中的单个 `.md` Prompt、相关文件 Git 状态并创建专用 worktree；
2. 追踪渲染、生产 Pydantic Schema、`ChatOpenAI` 结构化调用、下游业务和关键依赖；
3. 若缺少有效评测资产，生成业务契约、开发集、验证集、验收集和覆盖矩阵；
4. 验证固定 Conda `kds` 环境并生成或验证项目适配器；
5. 向用户展示契约、三套完整 Pydantic 预期、固定执行环境与命令、重复次数和门禁并等待确认；
6. 确认后执行连接、模型身份和适配器真实 smoke test，只使用开发案例，不接触验收结果；
7. 提交已确认且通过 smoke test 的评测资产，建立评测周期；
8. 对原 Prompt 运行开发集和验证集基线并保存摘要和失败聚类，验收集暂不运行；
9. 若开发集和验证集的所有计划调用均为 `pass`，以 `no_change_needed` 结束，不生成候选、不运行验收集；
10. 若用户掌握基线未复现的真实故障，停止调优，补充有证据的案例并重新执行确认和基线；
11. 读取 `optimization-history.yaml`，按错误类别和业务维度聚类失败；
12. 一次选择一个有证据的失败簇，从原 Prompt 或上一候选生成最小修改候选；
13. 将候选写入 `.runtime/`，不覆盖原 Prompt；
14. 重跑受影响的开发案例，通过后重跑完整开发集；
15. 只有完整开发集不退化时才运行验证集；
16. 根据验证结果选择满足门禁且至少一个核心指标严格改善的最终候选并冻结哈希；
17. 在同一最终评测活动中，分别对原 Prompt 和冻结候选运行验收集；
18. 终验失败时结束本周期，不得根据结果继续修改本周期候选；经用户再次确认后，只能将已确认评测资产和新增失败历史同步回原工作区；
19. 终验通过后请求用户确认是否交付最终 Prompt；
20. 确认后在 worktree 中替换生产 Prompt，更新契约中的当前 Prompt 哈希，提交最终结果，并将可交付资产补丁同步回原工作区。

默认最多进行 5 轮候选迭代。连续两轮没有指标改善、出现业务契约冲突、适配器失真、模型不可用或达到最大轮数时停止并报告，不能无限循环。

交付生产 Prompt 是独立的最终用户确认动作。同步前后通过候选哈希、worktree 提交和补丁内容证明落盘内容就是已经终验的候选，不在同步后重新打开本周期调优。

### 12.2 `verify`

用于只读验证：

- 默认直接在当前工作区执行，不创建 worktree；
- 固定使用 Conda `kds` 环境；
- 校验评测资产和适配器；
- 对指定的原 Prompt 或候选 Prompt 运行开发集、验证集或用户另外提供的非验收案例；
- 不得读取或运行 `acceptance-cases.yaml`；周期结束后也不例外；
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
- 若开发集和验证集的所有计划调用均通过，以 `no_change_needed` 结束，不为假设性问题修改 Prompt，也不运行验收集；若用户掌握未被复现的真实故障，先补充并重新确认案例，再重新建立基线。

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
- 不含 Authorization Token 的完整固定请求配置；
- 数据集版本或哈希；
- 每个案例的重复次数、通过次数和错误分类；
- Schema 合法率、单次正确率、完全稳定案例率；
- `regression_count`、`stability_regression_count` 及对应案例；
- 基线与候选的修复、退化和未变化案例；
- 候选 Prompt diff（适用时）；
- 停止原因；
- 是否达到交付门禁。

报告不得包含 Token 或 Authorization 头。个人使用场景中的业务输入、完整 Pydantic 预期、模型输出和字段差异可以原样保存，以支持失败追溯。

`reports/` 中的原始响应和完整运行报告只保留在 worktree，不提交、不默认同步。可交付的 `optimization-history.yaml` 按周期追加以下精简信息：

- 周期、基线 Prompt、数据集、配置和 manifest 哈希；
- 失败簇、案例 ID、错误分类和字段差异；
- 每轮修改意图、指标变化、修复与回归案例和淘汰原因；
- 最终停止原因和尚未解决的问题；
- 经用户确认、应在下一周期转入开发集的真实故障。

`optimization-history.yaml` 不保存中间候选全文、原始响应或 Token。新周期必须读取历史记录以避免重复无效策略，但当前已确认的契约和案例始终是业务真值来源。基础设施故障只进入运行报告，不作为 Prompt 优化经验。

## 15. 错误处理

- 模型服务不可用或身份不匹配：暂停，不切换模型；
- Schema 无法定位：请求用户提供位置，不能猜测替代 Schema；
- Prompt 渲染、适配器导入或固定 Conda `kds` 环境失败：记录 `setup_error` 并暂停，不用简化路径继续；
- 业务证据冲突：暂停契约确认；
- 单次传输失败：记录明确原因，最多重试 2 次；仍失败时槽位保持 `incomplete`，不按 Prompt 失败计分；
- `ChatOpenAI` 或 OpenAI SDK 抛出其他非重试异常：记录 `setup_error` 并暂停；
- 服务响应 envelope 不合法，或结构化调用返回值缺少 `raw`、`parsed`、`parsing_error`：记录 `protocol_error` 并暂停；
- 候选输出无法解析或验证：记录 `parse_error` 或 `schema_error`，不得用文本猜测结构；
- 评测资产、Prompt 路径或 manifest 失配：提示迁移或重建基线，不能混用旧数据；
- 契约、案例、Python 命令、门禁、适配器、生产关键依赖或固定客户端请求行为变化：结束旧周期并重新建立基线；
- 最终验收失败：结束本周期，不交付失败候选；
- 失败周期只有在再次获得用户确认后，才能向原工作区同步已确认评测资产和新增的 `optimization-history.yaml` 记录；
- 交付时无法从 `cycle_base_commit` 和当前已提交 worktree `HEAD` 重新生成补丁、Git 报告的实际路径不等于派生白名单路径、存在额外 section/文件，或提交版本的 canonical Prompt path 被重定向：停止，不采用持久化补丁、清单或哈希作为信任锚；
- 补丁预检冲突、原工作区 `HEAD` 不再等于 `cycle_base_commit`、目标文件已变化或待新增路径发生冲突：停止，不自动合并或覆盖；
- `git apply --check`、补丁应用、实际路径验证或哈希校验异常：恢复同步前保存的精确目标文件状态；
- 用户取消：保留原工作区和生产 Prompt，不进行最终同步。

## 16. Skill 自身的测试方法

目标 Prompt 评测与 Skill 自身测试是两套不同测试：

1. 目标 Prompt 评测使用固定局域网 27B 和确定性断言；
2. Skill 自身通过结构校验、离线脚本测试、临时 Python 工程集成测试、真实局域网 smoke test 和 Codex 行为前向测试验证。

结构校验运行 Skill Creator 的 `quick_validate.py`，但该工具只检查 Skill 结构、名称、frontmatter 和未完成占位符，不作为功能通过证明。

离线脚本测试使用临时 Git 仓库和仅供测试注入的伪造 HTTP transport，至少覆盖：

- `prompt-id` 生成和路径碰撞；
- 三套案例的 ID、语义族和格式校验；
- 生产 Schema 声明字段的规范序列化比较和字段差异报告；
- 结构化调用返回值不符合字典契约、原始消息缺失、结构化载荷缺失、解析失败、Schema 失败和业务失败分类；
- 30 秒超时、最多 2 次重试、固定调用槽位中断与恢复；
- manifest 兼容性和两类回归计算；
- Token 和 Authorization 不进入任何输出；
- worktree 创建、白名单路径过滤、Git 实际变更路径与补丁 section 完全匹配、canonical Prompt path 绑定、补丁预检、冲突停止和精确目标同步恢复；
- 交付只从可信周期状态重新生成补丁，不把持久化补丁、manifest 或哈希当作敌对篡改防护；覆盖原工作区 `HEAD` 漂移、提交契约路径重定向、额外路径、删除、失败结果不含 Prompt、worktree 边界、`git apply --check` 失败、应用后路径/哈希校验失败和未暂存未提交结果；
- 中间候选不会进入提交或同步资产。

测试 transport 不能通过项目配置进入生产运行路径。生产客户端仍只允许固定局域网模型。

Python 工程集成测试在隔离的临时仓库中验证固定 Conda `kds` 环境、生产渲染器和 Pydantic Schema 导入、拒绝其他 Python 环境、相关与无关脏文件处理、从 `cycle_base_commit` 生成白名单补丁和最终同步、未暂存未提交的同步结果，以及 `verify` 不修改规范资产且不能运行验收集。Task 9 的 fixture orchestration 只调用这些真实生产 leaf CLI/module，并通过显式的测试专用 client 边界注入离线 transport；它不实现第二套生产 `tune`/`verify` 编排，也不代表已经覆盖 `SKILL.md` 的整体状态机。Task 9 对固定生产客户端的身份和请求设置不作覆盖声明，该证据由 Task 4 和 Task 10 提供。

发布前必须使用固定局域网模型完成至少一次端到端 smoke test，确认模型身份、`temperature=0.0`、关闭 thinking/reasoning/search、Pydantic function calling、`include_raw=True`、超时、结构化返回契约与缺失载荷分类、基线与候选请求一致性，以及 Token 不进入输出。日常离线测试不依赖局域网服务。

Codex 行为前向测试使用现实请求，且不给评测者预期答案、已知缺陷或建议修复。至少覆盖：

- 一个具有明确 Schema 和调用链的正常 Python 工程；
- 一个包含多个 Prompt、要求只处理指定文件的工程；
- 一个业务证据冲突、必须暂停的工程；
- 一个开放式输出 Prompt、必须拒绝自动调优的工程；
- 一个开发集和验证集基线完全通过、必须以 `no_change_needed` 结束且不运行验收集的工程；
- 一个候选提高局部分数但引入回归、必须拒绝交付的工程；
- 一个候选在验证集严格改善、原 Prompt 与候选在验收集均满分且允许交付的工程；
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
| `scripts/local_model_client.py` | 固定 `ChatOpenAI`、`temperature=0.0`、关闭 thinking/reasoning/search、30 秒超时和 2 次重试 | 项目配置覆盖、模型选择、参数搜索、报告业务结论 |
| 通用 runner/scorer | Pydantic function calling、固定槽位、恢复执行、声明字段规范序列化比较、错误分类、指标和基线比较 | 推断业务真值、修改验收预期、实现通用断言语言 |
| `scripts/manage_worktree.py` | 隔离 worktree、同步补丁预检和哈希校验 | 自动合并、覆盖用户修改、提交原工作区 |
| 项目 `prompt-contract.yaml` | 已确认业务契约和证据 | 模型生成的未经确认结论 |
| 项目三套数据集 | 输入、预期、优先级、维度和语义族 | 调优中自动迁就候选、跨集合放置近邻案例 |
| 项目 `adapter.py` | 复用生产渲染和消息组装，返回生产 Pydantic Schema | 解析模型响应、自定义判定、复制或重定义 Schema、选择模型 |
| `optimization-history.yaml` | 精简、可复用的失败知识 | 中间候选全文、原始响应、Token、基础设施故障 |
| `.runtime/` | 临时候选、调用槽位状态和缓存 | 提交或同步到原工作区 |
| `reports/` | 原始运行和对比证据 | 凭据、Authorization 头 |

## 18. 验收标准

设计实现完成必须满足：

1. Skill 可从 `$CODEX_HOME/skills` 被 Codex 隐式发现和显式调用，并在能够通过固定 Conda `kds` 环境运行的不同 Python Git 仓库中对单个 `.md` Pydantic 结构化 Prompt 启动，不包含任何具体 Agent 的业务字段；
2. 每个仓库只建立一个 `.prompt-evals/`，每个 Prompt 建立一个稳定 `prompt-id` 子目录；
3. Skill 能从真实工程调用链生成有证据的业务契约和确定性测试集；
4. 用户确认契约和案例前不调用本地模型；
5. `tune` 在同一专用 worktree 和周期中连续完成必要的内部初始化、基线和候选调优，允许无关脏文件但拒绝未提交的关键依赖；
6. 评测固定通过 Conda `kds` 环境和最小适配器复用生产 Prompt 渲染、消息组装和 Pydantic Schema，不探测或切换其他 Python 环境；
7. 模型地址、名称、`temperature=0.0`、关闭 thinking/reasoning/search、30 秒超时和 2 次重试固定在受跟踪的个人 Skill 客户端中；Token 只从 Skill 根目录 `.local/model-credentials.json` 读取，且不出现在日志、异常、manifest、报告或目标工程；
8. 原 Prompt、候选 Prompt 使用相同固定请求行为和兼容 manifest；
9. 开发集、验证集和验收集隔离，验收结果不反馈到本周期；
10. 案例完整预期与模型结果均由同一生产 Pydantic Schema 构造，并以声明字段规范序列化数据相等判定正确性；PrivateAttr、缓存和其他 runtime-only state 不参与持久化或评分；Schema 合法性、稳定性、门禁回归和完全稳定性回归分别计量；严格改善只用于验证集候选选择，验收集只要求达标且相对原 Prompt 无回归；
11. 传输失败不计入 Prompt 指标，固定槽位可恢复补齐，未完成时不形成结论；
12. Skill 不使用 LLM Judge，不处理无法确定判分的 Prompt；
13. 结构化调用正常返回但不满足 `raw`、`parsed`、`parsing_error` 字典契约时计为 `protocol_error`；取得原始消息后缺少结构化载荷才计为 `parse_error`；自动调优只写临时候选，未经用户最终确认不在 worktree 中替换生产 Prompt；
14. 最终只把白名单资产安全同步到原工作区，并保持未暂存、未提交；
15. 冻结验收集不得在调优循环中自动修改或重复用于同周期调优，并且不能由 `verify` 读取或运行；
16. `optimization-history.yaml` 保留必要失败知识，但不包含中间候选全文或原始响应；
17. 模型不可用、契约冲突、回归、无改善、终验失败或同步冲突时能按明确停止条件退出；
18. 失败周期未经再次确认不修改原工作区；确认后也只能同步已确认评测资产和新增失败历史；
19. 开启新周期前，上一周期同步的评测资产必须已经由用户提交到目标仓库；
20. 开发集和验证集基线完全通过时以 `no_change_needed` 结束，不生成候选、不运行验收集；成功或失败交付补丁都在交付时从 `cycle_base_commit` 和当前已提交 worktree `HEAD` 重新生成，Git 报告的实际路径与选定白名单路径及补丁 section 必须完全一致，且经过 canonical Prompt path、`git apply --check`、精确快照、应用后路径/哈希校验和失败恢复；Skill 自身通过 `quick_validate.py`、离线脚本测试、临时工程集成测试、真实局域网 smoke test 和 Codex 行为前向测试。

## 19. 明确决策

- 这是通用 Prompt 调优 Skill，不是百科专属工具；
- 百科可作为首个真实验证工程，但其字段、路径和规则不得进入 Skill 本体；
- V1 专门面向 Python Git 工程，不设计跨语言适配器协议；
- Python 运行环境固定为本机 Conda `kds`，命令为 `conda run -n kds python`，不探测 `.venv`、uv、Poetry 或其他环境；设计和发布验证以当前 Python `3.14.6`、Pydantic `2.13.4` 为基准；
- 所有目标 Prompt 都是单独的 `.md` 文件；
- 所有目标调用都使用生产 Pydantic Schema 和 `ChatOpenAI.with_structured_output(..., method="function_calling")`；
- 一次只处理一个 Prompt；
- 允许生成项目专属适配器，且必须复用工程现有渲染、消息组装和 Pydantic Schema；适配器不解析响应或提供自定义判定器；
- 固定局域网模型不由 Skill 调整，`temperature=0.0` 且 thinking/reasoning/search 始终关闭；
- 局域网 Token 只能由用户手动放入个人 Skill 根目录 `.local/model-credentials.json`；该文件只允许非空字符串字段 `authorization_token`，由根目录 `.gitignore` 精确排除。测试只能使用临时路径中的 sentinel，Token 不得进入测试、报告、manifest、diff 或安装包，安装不得隐式复制凭据；缺失、不可读、畸形、空值或额外字段均为非计分 `setup_error`；
- 模型连接、固定模型参数、30 秒超时和 2 次重试不能由项目配置覆盖；
- 业务契约、三套数据集、固定 Conda 环境与命令和门禁必须在基线运行前向用户展示并由用户确认；
- Skill 可以自动迭代临时候选，但不持久化或交付中间候选；
- 最终验收每周期只运行一次且只能由 `tune` 执行，`verify` 不得读取或运行验收集；失败后继续调优必须建立新周期和新验收集；
- 终验失败不交付候选或生产 Prompt 修改；经用户再次确认后，可以同步已确认评测资产和新增失败历史；
- `bootstrap` 只作为 `tune` 的内部初始化阶段，并在同一 worktree 和周期中连续进入候选调优；`verify` 默认直接在当前工作区只读运行；
- `cycle_base_commit` 是创建 worktree 时原工作区的 `HEAD`；成功结果或经确认允许交付的失败周期资产都从该提交生成白名单补丁，同步回原工作区但不暂存、不提交，冲突时不自动合并；
- Task 7 交付使用本地单用户、非对抗性威胁模型：保护误操作、并发编辑、过期状态、路径错误、意外文件和应用失败；不防御可同时任意修改 Skill、仓库、补丁、manifest 和哈希的本地攻击者，不使用锁、密码学信任或自定义完整补丁解析器；每次交付从可信周期状态 fresh-generate，并在 apply 前立即复核原始 `HEAD` 与提交契约的 canonical Prompt path，apply 后校验实际路径和哈希并在失败时 rollback；
- 每个工程使用根目录 `.prompt-evals/`，每个 Prompt 使用“可读 slug + 12 位路径哈希”的独立子目录。
