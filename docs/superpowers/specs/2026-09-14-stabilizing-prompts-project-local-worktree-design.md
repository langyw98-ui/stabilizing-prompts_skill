# Stabilizing Prompts 项目内 Worktree 与覆盖驱动数据集设计

日期：2026-09-14  
状态：待实现

## 1. 背景

`stabilizing-prompts` 当前通过 `scripts/manage_worktree.py` 为每个 `tune`
周期创建专用 Git worktree。默认路径位于目标仓库的父目录，形如：

```text
<repo-parent>/.<repo-name>-stabilizing-prompts-<prompt-id>-<cycle-id>
```

这与 `superpowers:using-git-worktrees` 的项目内 `.worktrees/` 约定不一致，
会在多个项目的共同父目录产生较长的隐藏目录。当前实现还显式拒绝项目内
worktree，因此用户无法通过参数选择通用布局。

当前 `tune` 会生成 `dev-cases.yaml`、`validation-cases.yaml` 和
`acceptance-cases.yaml` 三个数据集，但只规定案例格式、Schema 有效性和跨
split 隔离，没有规定最低案例数，也没有机器可校验的覆盖义务。每个 split
只有一条合法案例时也能通过 `validate_cases.py`。因此案例生成可以在格式正确
的同时缺少代表性、边缘条件和历史回归覆盖。

本设计在一个变更中解决这两个问题：

1. 让 `tune` 只从普通主工作区启动，并将专用 worktree 创建到目标仓库的
   `.worktrees/` 下；
2. 为三个固定数据集增加每个 split 至少 30 条有效案例的硬门槛，以及由生产
   证据驱动、可冻结、可校验的覆盖义务和覆盖饱和门禁。

## 2. 目标

- 手工 Git worktree 的唯一根目录是 `<repo>/.worktrees/`。
- `.worktrees/` 必须在创建 worktree 前已被目标仓库的 Git ignore 规则覆盖。
- 从 linked worktree 启动 `tune` 时 fail closed，不创建嵌套 worktree。
- 隔离创建失败时返回 `setup_error`，绝不回退到原工作区运行调优。
- 保留 `WorktreeCycle`、不可变基准提交、白名单补丁和安全交付机制。
- `dev`、`validation` 和 `acceptance` 每个 split 至少包含 30 条有效案例。
- 30 条是下限而不是目标；只要还有有证据支持且尚未覆盖的边界条件，就继续
  生成案例。
- 禁止用复制、同义改写或无关上下文变化凑数量。
- 在第一次模型调用前冻结并校验覆盖义务、案例、未适用类别和覆盖矩阵。

## 3. 非目标

- 不兼容、迁移、发现或恢复旧的仓库外 worktree 和旧周期状态。
- 不支持并行 `tune` 周期、worktree 池、锁或调度。
- 不自动修改、暂存或提交目标仓库的 `.gitignore`。
- 不自动删除 worktree 或内部调优分支。
- 不支持 `worktrees/`、仓库同级目录、临时目录或用户自定义 worktree 位置。
- 不修改模型端点、调用参数、重复次数、评分方法、候选轮数或终验只运行一次的
  语义。
- 不使用目标模型输出反推业务真值、覆盖义务或预期对象。
- 不通过无业务依据的字段笛卡尔积批量制造案例。
- 不修改通用 `superpowers:using-git-worktrees` skill。

## 4. 总体流程

调整后的 `tune` 前半段为：

```text
preflight
  -> verify primary-workspace identity
  -> verify .worktrees ignore rule
  -> create dedicated worktree under .worktrees/
  -> persist WorktreeCycle
  -> derive coverage obligations from business evidence
  -> generate dev/validation/acceptance cases
  -> validate minimum counts, uniqueness, and coverage saturation
  -> user confirmation
  -> model probe/smoke
  -> existing baseline, candidate, acceptance, and delivery lifecycle
```

Worktree 检查和数据集检查都是第一次模型调用前的硬门禁。任一门禁失败均停止
本周期，不生成候选，也不回退到原工作区。

## 5. 项目内 Worktree 管理

### 5.1 启动环境检测

`manage_worktree.py create` 在任何写操作前读取：

```text
git rev-parse --show-toplevel
git rev-parse --git-dir
git rev-parse --git-common-dir
git rev-parse --show-superproject-working-tree
git branch --show-current
git rev-parse --verify HEAD
```

解析后的 Git directory 与 common directory 不同，且当前目录不是 submodule
工作区时，当前 checkout 被视为 linked worktree。`tune` 立即返回
`setup_error`，提示用户回到普通主工作区运行。Skill 不复用该 worktree，也不
在其中创建嵌套 worktree。

Submodule guard 只防止把 submodule 误判为 linked worktree。若 Prompt 仓库
本身是 submodule，它仍作为自己的 Git 仓库执行后续规则，并必须在自身规则中
忽略 `.worktrees/`。

Detached HEAD、无法解析的仓库身份或无法验证的 `HEAD` 同样是
`setup_error`。所有检测结果使用解析后的绝对路径，不使用模型输出或未经验证
的环境变量。

### 5.2 唯一目录和 ignore 门禁

唯一允许的 worktree 根目录是：

```text
<repo>/.worktrees/
```

不再检查 `worktrees/`，也不接受调用者传入其他根目录。CLI 删除
`manage_worktree.py create --worktree PATH`，Python API 删除
`create_cycle(..., worktree=...)`。

生成 `prompt-slug` 和 `cycle-id` 后、创建任何目录或分支前，通过 Git 对最终
目标形状下一个不存在的哨兵路径执行 ignore 查询：

```text
git check-ignore --no-index --quiet -- \
  .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/.stabilizing-prompts-probe
```

该命令只查询规则，不创建哨兵文件。仓库 `.gitignore`、`.git/info/exclude` 或
全局 excludes 中当前生效的任一规则都可以满足本地隔离门禁。实现可以额外使用
`-v` 报告匹配规则来源，但不要求规则必须来自已提交的 `.gitignore`。查询不
通过时返回 `setup_error`，并推荐用户自行把 `.worktrees/` 加入项目
`.gitignore`、提交后重新运行。Skill 不修改或提交 `.gitignore`。

如果 `.worktrees` 已存在但不是目录，或者解析后的目标目录逃逸
`<repo>/.worktrees/`，返回 `setup_error`。ignore 门禁通过后，Skill 可以创建
尚不存在的 `.worktrees/` 及其内部父目录。

### 5.3 分支和目录命名

未指定自定义分支时，内部调优分支继续使用：

```text
stabilizing-prompts/<prompt-slug>-<cycle-id>
```

CLI 的 `--branch` 和 Python API 的 `branch=` 保持可用，但自定义值必须通过
现有 Git 分支名和安全校验。无论是否指定自定义分支，对应 worktree 都固定为：

```text
<repo>/.worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>
```

`cycle-id` 保留当前随机唯一标识。它用于避免保留的旧目录或默认分支与下一
周期重名，不代表支持并行调优。自定义分支名不得参与 worktree 路径派生。目标
目录或分支已存在时返回 `setup_error`，不得猜测其所有权或自动复用。

### 5.4 创建、状态和失败处理

所有只读检查通过后，调用：

```text
git worktree add -b <branch> <repo>/.worktrees/stabilizing-prompts/<name> <base>
```

成功后继续使用现有 `WorktreeCycle` 记录：

- `original_repo`；
- `worktree`；
- `branch`；
- `cycle_base_commit`；
- `prompt_id`；
- canonical `prompt_path`；
- 后续资产提交和最终提交身份。

Git 创建失败时保留原始 stderr 并返回 `setup_error`。状态文件持久化失败时停止
并报告已创建的精确 worktree 和分支，供用户人工检查；Skill 不自动删除可能
含有诊断信息的 worktree 或分支。任何失败都不得进入 contract/cases/adapter、
模型 probe 或候选实验。

现有交付过程保持不变：最终交付仍从不可变 `cycle_base_commit` 和已提交的
worktree `HEAD` fresh-generate 白名单补丁，预检原工作区身份和目标哈希，应用
后保持 unstaged/uncommitted，并在异常时恢复精确快照。

## 6. 覆盖义务资产

### 6.1 文件和信任边界

新增冻结资产：

```text
.prompt-evals/<prompt-id>/coverage-obligations.yaml
```

覆盖义务由 Codex 根据已确认的生产证据提出，但不能从目标模型输出推导。证据
优先级沿用 `references/business-contract.md`：生产 Schema、生产控制流、正式
业务文档和已确认历史故障优先于现有 Prompt 文案和优化历史。

该文件与 contract、三个 split、adapter 和 eval config 一起在确认门禁冻结并
哈希。它加入成功交付和失败资产交付白名单；运行报告、近重复分析缓存和中间
候选仍不交付。

### 6.2 固定覆盖类别

覆盖义务文件必须对以下每个类别声明 `required` 或 `not_applicable`：

- `normal_path`：高频合法路径；
- `output_partition`：每个枚举输出或业务动作；
- `near_boundary`：近邻正例和近邻反例；
- `field_boundary`：必填、缺失、空值、默认值、可空和值域边界；
- `conditional_branch`：生产规则中条件分支的两侧和临界点；
- `conflict`：冲突条件、否定和复合意图；
- `ambiguity`：信息不足或存在多个合理解释；
- `irrelevant_input`：领域外或任务外输入；
- `fallback`：明确兜底路径；
- `historical_regression`：已确认故障及其相邻变体；
- `adversarial`：容易诱发 Schema 合法但业务错误的输入。

`evidence_checked` 记录为判断适用性而检查过的仓库路径、测试集合或 Git 历史
范围；它不是业务真值本身。`not_applicable` 必须包含非空
`evidence_checked` 和非空理由。缺失类别或没有理由的 `not_applicable` 是
`setup_error`。这不是要求每个 Prompt 强行制造所有类别，而是要求用户能看到
哪些类别被有意排除。

### 6.3 义务结构

文件格式为：

```yaml
version: 1
categories:
  - category: historical_regression
    applicability: not_applicable
    evidence_checked:
      - git-history:prompt-and-call-chain
      - repository-tests
    rationale: 没有已确认的历史故障证据
obligations:
  - id: reject-unrelated-question
    source:
      - target_app/production.py
    category: irrelevant_input
    risk: critical
    rule: 与目标任务无关的请求必须进入拒绝动作
    required_splits:
      dev:
        - normal
        - boundary
      validation:
        - adversarial
      acceptance:
        - natural_variation
    variant_exclusions:
      conflict: 生产规则不存在可同时成立的冲突条件
```

规则如下：

- `id` 在义务文件中唯一且稳定；
- `source` 非空并指向仓库内证据；
- `category` 必须属于固定分类且其 applicability 为 `required`；
- `risk` 只能是 `normal` 或 `critical`；
- `rule` 是可由完整预期对象判定的业务陈述；
- `required_splits` 至少包含一个 split，每个 split 的 variant 列表非空且无
  重复；
- `variant_exclusions` 是可选映射，只能为未出现在 `required_splits` 中的固定
  variant 提供非空义务级理由；
- critical 义务必须在 `required_splits` 中包含 `normal`，并至少包含
  `boundary`、`conflict` 或 `adversarial` 之一；这三种 critical variant 中其余
  未声明者必须出现在 `variant_exclusions` 中。

固定 variant 枚举为：

```text
normal
boundary
conflict
ambiguity
irrelevant
fallback
regression
adversarial
natural_variation
```

`variant` 只表达校验器需要理解的覆盖类型。更具体的业务前提和案例差异由
`condition_id` 表达，不允许新增项目自定义 variant。

## 7. 案例结构、数量和唯一性

### 7.1 最低数量

`validate_cases.py` 使用不可由项目配置降低的固定常量：

```python
MIN_CASES_PER_SPLIT = 30
```

`dev`、`validation` 和 `acceptance` 分别独立计算有效案例数。三个 split 不要求
数量相同，也不设置上限。无效、硬重复或疑似近重复但缺少 `distinction` 的案例
不计入最低数量。

如果生产证据不足以支持任一 split 至少 30 条实质独立案例，本周期必须以
`setup_error` 停止，并报告未达到门槛的 split 和数量。不得降低门槛、制造无
依据义务或用同义改写补足数量。

### 7.2 案例覆盖元数据

每条案例在现有字段之外增加：

```yaml
coverage:
  primary_obligation: reject-unrelated-question
  secondary_obligations: []
  variant: boundary
  condition_id: mentions-domain-keyword-but-intent-is-unrelated
  distinction: 与其他无关输入不同，本例包含领域关键词但真实意图仍在任务外
```

规则如下：

- 每条案例必须引用一个真实的 `primary_obligation`；
- `secondary_obligations` 只能引用真实义务，用于报告而不贡献最低数量或义务
  配额；
- `variant` 必须属于固定枚举，并满足对应义务在该 split 声明的一个 variant；
  具体的有证据变体使用不同 `condition_id` 记录，不能新增自定义 variant；
- `condition_id` 描述产生独立业务判断的条件组合，采用稳定 slug；
- `distinction` 在近重复审计命中或同一义务存在多个相近案例时说明实质差异。

### 7.3 硬重复

以下任一情况属于硬重复，不得计入数量或覆盖，并使资产验证失败：

1. 全局重复 case ID；
2. 规范化完整 `input` 的 canonical JSON 指纹在任意 split 重复；
3. 全局重复场景键：

   ```text
   normalized(primary_obligation, variant, condition_id)
   ```

4. 同一案例试图通过 secondary obligations 重复贡献配额。

同一业务义务可以跨 split 出现，因为三个 split 都需要验证关键业务规则；但其
输入、variant 或 condition 必须形成不同测试实例。现有跨 split 输入指纹、ID
和泄漏检查继续保留，并与场景键检查共同执行。

### 7.4 疑似近重复

校验器生成确定性的近重复审计，不调用模型。对于 primary obligation 和规范化
预期对象相同的案例对：

1. 将所有输入字符串做 Unicode NFKC、大小写归一、标点去除和空白折叠；
2. 保留非字符串叶子的字段路径和值；
3. 当非字符串叶子相同且字符串字符三元组 Jaccard 相似度达到或超过 `0.85`
   时，标记为疑似近重复。

疑似近重复不是自动判定业务等价。命中案例必须提供非空 `distinction`，说明
不同的业务前提、决策边界或预期行为；缺少说明时返回 `setup_error`，案例不
计数且不能贡献覆盖配额。只说明“换了一种说法”或“使用不同措辞”不构成实质
差异，但该语义判断由用户而不是确定性校验器完成。

存在非空 `distinction` 时，校验保持 `status: valid`，同时输出
`requires_user_review: true`。这些案例暂时计入预计有效数量和覆盖矩阵；
`CASE_SUITE_JSON` 必须列出案例对、相似度和差异说明，供用户在确认门禁逐项
审阅。用户接受全部差异说明后，案例才正式计数并冻结；用户不接受任一说明时，
必须修改或删除相关案例，再重新运行校验和确认。无需新增逐 pair 裁决文件。

## 8. 覆盖义务生成和覆盖饱和

### 8.1 生成顺序

Codex 必须先完成覆盖义务，再生成案例：

1. 读取 Prompt、生产 Pydantic Schema、生产渲染器和消息组装代码；
2. 追踪每个输出枚举、业务动作、显式条件分支和兜底路径；
3. 枚举必填、可空、默认值、条件字段和有证据的值域边界；
4. 收集正式业务文档、现有测试和已确认历史故障；
5. 为正常、近邻反例、模糊、冲突、无关、回归和对抗行为建立义务；
6. 对固定类别逐项标记 required 或带证据理由的 not_applicable；
7. 为义务声明各 split 的必需 variants；
8. 再生成具有完整生产 Schema 预期对象的独立案例。

自动字段组合只能用于发现候选边界。候选组合只有在能关联生产证据、明确业务
规则和完整预期对象后，才能成为覆盖义务和案例。

### 8.2 Split 分配

- `dev` 优先包含可诊断的正常、反向、历史故障和具体边界案例；
- `validation` 使用不同输入和条件组合，独立检查候选是否泛化；
- `acceptance` 使用独立实例，重点覆盖关键行为、自然变化和高风险边界；案例在
  用户确认后冻结，候选迭代阶段不得读取或运行，其现有只运行一次且不反馈调优
  的规则不变；
- critical 义务原则上跨三个 split 验证，但每个 split 必须使用不同实例；
- 普通义务按风险和适用性分配到一个或多个 split；
- 分配优先扩大业务义务和边界维度覆盖，不追求三个 split 数量相等。

### 8.3 覆盖饱和门禁

只有同时满足以下条件，案例资产才能进入用户确认：

1. 每个 split 至少有 30 条有效案例；
2. 每项义务的所有必需 split/variant 配额都已满足；
3. 所有 fixed categories 均已标记 required 或有证据理由的 not_applicable；
4. 所有 critical 义务完成其正常和适用的边界、冲突或对抗覆盖；
5. 没有硬重复，且所有疑似近重复均提供了待用户审阅的非空 `distinction`；
6. 对 Schema、生产分支、业务契约、历史故障和输入边界完成一轮系统扫描后，
   没有尚未登记的、有证据支持的边界条件；
7. coverage matrix 不存在缺口。

30 是最低门槛而不是停止条件。达到 30 后发现新的有证据边界时，必须继续增加
案例；三个 split 可以超过 30 且数量不同。不得为了增加数量生成无业务意义的
笛卡尔积。

第 6 项由 Codex 在 coverage summary 中列出已扫描证据和未发现新增边界的结论，
再由用户确认。校验器只对已声明义务和案例做机械校验，不能替代用户判断义务
提取是否完整。coverage summary 不新增独立项目资产；其扫描范围和结论记录在
现有 confirmation record 中。

## 9. 校验输出和用户确认门禁

`validate_cases.py` 在资产构建时同时读取：

- `coverage-obligations.yaml`；
- `dev-cases.yaml`；
- `validation-cases.yaml`；
- `acceptance-cases.yaml`；
- production Schema。

扩展后的 `CASE_SUITE_JSON` 至少包含：

- 顶层 `status: valid|error`，不得增加第三种状态；
- 是否存在待用户审阅近重复的 `requires_user_review` 布尔值；
- 各 split 总数、有效数和不计数案例；
- 各类别、义务、风险和 variant 的覆盖分布；
- 缺失的 split/variant 配额；
- 输入指纹和场景键冲突；
- 疑似近重复案例对、相似度和 distinction；
- required/not_applicable 类别及理由；
- coverage saturation 的逐项状态；
- 三个 split 和覆盖义务资产的内容哈希。

以下任一情况返回非评分 `setup_error`：

- 任一 split 有效案例少于 30；
- 未知义务、未知 variant 或非法类别；
- 义务配额、critical 覆盖或类别声明不完整；
- 硬重复，或疑似近重复缺少非空 `distinction`；
- 无理由的 not_applicable；
- expected object 无法由生产 Schema 验证；
- 现有 ID、semantic/input leakage 或 Schema import 检查失败。

用户确认门禁必须展示完整义务、全部案例、数量、覆盖矩阵、未适用理由、重复
审计、固定重复次数和由实际案例数推导出的预计模型调用量。若
`requires_user_review` 为 true，用户必须接受全部疑似近重复的差异说明；拒绝
任一说明时返回案例生成步骤修订资产。用户明确确认后才冻结资产并进入
probe/smoke。

现有 confirmation record 还必须记录：

- 本次扫描的仓库路径、测试集合和 Git 历史范围 `evidence_checked`；
- 未发现其他有证据边界的 `saturation_statement`；
- `coverage_obligations_hash` 和 `case_suite_hash`；
- 用户已审阅近重复报告的确认标识。

confirmation record 属于周期运行状态，不是新的项目资产、CLI 输入或交付项。
覆盖义务或案例哈希变化时，旧 confirmation record 立即失效，必须重新生成并
确认。

若三个 split 的实际案例数分别为 `D`、`V`、`A`，确认材料至少展示：

```text
baseline slots = 5D + 5V
one full promoted candidate round = 5D + 5V + affected-dev pre-run slots
paired acceptance slots = 10A + 10A
```

此外还应列出 smoke、计划候选轮数上限和提前停止规则。该数字是计划调用槽位，
传输重试不替换槽位，也不把失败调用伪装成额外成功样本。

## 10. 交付和哈希影响

`coverage-obligations.yaml` 属于 confirmed evaluation asset：

- asset commit 必须包含该文件；
- frozen confirmation record 必须绑定其内容哈希；
- baseline 和后续 manifest 必须绑定该哈希；
- 成功交付白名单包含该文件；
- 用户确认失败资产交付时，失败资产白名单也包含该文件；
- 对该文件的额外、缺失、未提交或哈希漂移继续触发现有交付冲突处理。

现有 Prompt、contract、config、三个 split、adapter 和 optimization history 的
交付规则不变。完整报告和重复分析中间数据继续留在忽略目录。

## 11. 实现影响

预计修改：

- `scripts/manage_worktree.py`
  - 主工作区检测；
  - 对实际目标形状执行 `.worktrees/` ignore 门禁；
  - 固定项目内目录；
  - 删除自定义 worktree 参数；
  - 将新资产加入交付白名单。
- `scripts/validate_cases.py`
  - 覆盖义务 Schema；
  - 每 split 30 条下限；
  - 场景键和近重复审计；
  - 义务配额和覆盖饱和检查；
  - 扩展 `CASE_SUITE_JSON`。
- `SKILL.md`
  - 更新 worktree 状态和 contract/cases/adapter 状态；
  - 明确生成顺序、停止规则和用户确认材料。
- `references/worktree-lifecycle.md`
  - 固定目录、启动前提和 fail-closed 行为。
- `references/case-schema.md`
  - 新资产、覆盖元数据、数量、重复和饱和规则。
- `references/business-contract.md`
  - 覆盖义务的证据优先级和确认门禁。
- `references/evaluation-method.md`
  - 实际案例数对计划调用槽位和 manifest 的影响。
- `tests/test_manage_worktree.py`、`tests/test_validate_cases.py`、skill 指令测试、
  集成测试及 fixture helpers。

不新增生产级 tune/verify orchestrator；现有 Skill 状态机仍负责编排叶子 CLI。

## 12. 测试策略

### 12.1 Worktree 测试

- 默认路径严格位于 `<repo>/.worktrees/stabilizing-prompts/...`；
- `.worktrees/` 不存在但 ignore 规则覆盖时成功创建；
- 实际目标形状未 ignore 时在创建分支和 worktree 前失败，包括根级哨兵被 ignore
  但目标被否定规则重新包含的情况；
- `.worktrees` 是文件或目标路径逃逸时失败；
- linked worktree 启动被拒绝，submodule 不被误判；
- CLI 和 Python API 不再接受任意 worktree 路径；
- 默认和自定义分支均使用固定派生的 worktree 路径，自定义分支继续通过安全
  校验；
- 目录或分支冲突 fail closed；
- 创建后原工作区不因 worktree 内容变脏；
- `WorktreeCycle` 继续记录正确路径、分支和基准提交；
- 创建或状态持久化失败不触发原工作区调优；
- 现有成功交付、失败资产交付、冲突检测、回滚和白名单测试继续通过。

### 12.2 数据集和覆盖测试

- 任一 split 为 29 条时失败，三个 split 各 30 条时通过；
- 超过 30 条的独立边界案例通过且全部计入 manifest；
- 复制 ID、输入指纹或场景键失败；
- secondary obligations 不贡献配额；
- 未知义务、variant、category 或 source 失败；
- 缺少必需 split/variant 配额失败；
- 未知或自定义 variant 失败；
- critical 义务缺少 `normal`、缺少至少一种边界/冲突/对抗覆盖，或缺少其他
  critical variant 的排除理由时失败；
- 类别缺失或 not_applicable 没有证据理由失败；
- 疑似近重复阈值、Unicode 规范化、distinction 和审计输出具有确定性；缺少
  distinction 时失败，存在说明时保持 `status: valid` 并设置
  `requires_user_review`；
- 同一义务跨 split 的不同实例可以通过；
- 义务文件、三个 split 和 Schema 一起验证并生成稳定哈希；
- `CASE_SUITE_JSON` 正确输出计数、覆盖缺口、重复审计和饱和状态；
- confirmation record 绑定证据扫描摘要、义务哈希、案例哈希和近重复审阅确认，
  但不成为新的交付资产；
- 生产证据不足 30 条独立案例时以 `setup_error` 停止且不允许降低门槛；
- 新资产进入冻结记录、asset commit、manifest 和交付白名单；
- fake transport 集成测试反映实际案例数和固定 repeats，不发起真实模型请求；
- 第一次模型调用前的任一资产失败都停止周期。

测试 fixture 应通过 helper 确定性生成足量的不同案例，避免手写 90 条重复 YAML
掩盖测试意图。生产常量不得提供仅供测试降低数量的后门。

## 13. 验收标准

实现完成时必须同时满足：

1. 从普通主工作区运行 `tune` 时，新 worktree 只出现在目标仓库的
   `.worktrees/stabilizing-prompts/` 下；
2. 从 linked worktree 启动、`.worktrees/` 未 ignore 或创建失败时返回
   `setup_error`，不调用模型且不在原工作区调优；
3. Skill 不修改或提交目标项目的 `.gitignore`；
4. `dev`、`validation`、`acceptance` 各至少有 30 条有效案例；
5. 达到 30 条后仍必须满足所有覆盖义务和覆盖饱和条件；
6. 硬重复和缺少 `distinction` 的疑似近重复不能用于满足数量或配额；带说明的
   疑似近重复必须经用户接受后才能冻结；
7. 同一业务义务可以跨 split 验证，但案例实例必须不同；
8. 覆盖义务、完整案例、未适用理由、重复审计和预计调用量在模型调用前由用户
   明确确认；
9. `coverage-obligations.yaml` 被正确冻结、哈希、提交并按结果类型安全交付；
10. 原有 Prompt 评分、候选选择、单次终验、回滚和白名单交付测试保持通过；
11. 全部离线测试和 skill 结构/指令验证通过，且测试不调用真实模型。

## 14. 安全性结论

项目内 `.worktrees/` 在已被 Git ignore 且路径经过验证时，与仓库同级 worktree
提供相同的 Git checkout 隔离，同时遵循通用 Superpowers 的目录习惯并减少父
目录污染。Prompt 调优继续采用比普通功能开发更严格的 fail-closed 规则：无法
建立专用隔离时绝不回退原工作区。

数据集安全不依赖案例数量本身。30 条下限阻止明显稀疏的数据集，机器可读覆盖
义务、独立场景键、近重复审计和用户确认共同防止通过复制案例获得虚假的稳定
性结论。覆盖饱和而非固定上限决定最终案例数。
