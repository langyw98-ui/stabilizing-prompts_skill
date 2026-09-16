# Stabilizing Prompts 安全周期生命周期整合设计

日期：2026-09-16  
状态：已批准，待实现

## 1. 背景与起始状态

`stabilizing-prompts` 的 `tune` 在目标 Python Git 仓库中为单个 Markdown
Prompt 建立专用 linked worktree，在其中构建评测资产、运行固定模型、比较候选，
再把允许交付的内容同步回原工作区。运行时的原始响应、manifest、报告和临时候选
位于 `.prompt-evals/<prompt-id>/reports/` 与
`.prompt-evals/<prompt-id>/.runtime/`；可交付评测资产位于同一 prompt ID 的其他
路径。

起始实现已经具备以下安全边界：

- `tune` 只能从 primary workspace 的非 detached 分支开始，并把周期 worktree
  固定在 `.worktrees/stabilizing-prompts/<cycle-dir>/`；
- 创建 worktree 前使用 `git check-ignore --no-index` 检查最终 worktree 目标；
- 交付时根据 immutable cycle base、已提交 worktree `HEAD` 和 committed contract
  身份实时生成 allowlisted patch；
- apply 前检查原工作区 `HEAD`、目标冲突和补丁，建立精确快照；apply 后校验目标
  内容，失败时恢复快照；
- 成功同步的文件保持 unstaged/uncommitted，不切换原工作区分支，不改 index，
  也不触碰无关脏文件；
- `reports/`、`.runtime/`、原始响应、凭据和 Token 不属于可交付资产。

但周期两端存在两个彼此独立、会共同破坏完整生命周期的问题。

### 1.1 初始化边界不完整

起始流程只验证 managed worktree 目标是否已被 Git 忽略，并要求用户在运行
`tune` 前自行建立相应规则。Skill 同时要求 linked worktree 内的 `reports/` 和
`.runtime/` 已被忽略、禁止修改目标项目 `.gitignore`，却没有初始化仓库本地
exclude、明确的运行目录前置 gate 或对应状态转换。

根因链如下：

1. 运行目录最初可以通过项目 `.gitignore` 建立忽略规则；
2. worktree 安全边界要求 Skill 不得修改或交付项目 `.gitignore`；
3. 安全加固只为 `.worktrees/` 增加显式 ignore gate，仍把 `reports/` 和
   `.runtime/` 已被忽略当作隐含前提；
4. 执行器因此可能在业务契约、案例和适配器已经审阅冻结后，直到即将写运行
   产物时才发现缺少规则；
5. 用户被迫进行一次与业务确认无关的手工配置，已有确认记录则绑定了一个尚未
   满足基础设施条件的周期。

主工作区忽略 `.worktrees/` 不能解决这个问题：该规则只让主工作区忽略整个
linked worktree 目录；在 linked worktree 自身的 Git 视图中，prompt 对应的
`reports/` 和 `.runtime/` 仍可能成为未跟踪路径。项目级否定规则还可能覆盖
更宽的 ignore 规则，因此读取 exclude 文件或匹配固定文本也不能证明最终行为。

### 1.2 正式评分后的终态不统一

起始流程对不同评分结论采取不同收尾路径：

- 开发集和验证集基线全部通过时以 `no_change_needed` 直接结束；
- acceptance 失败时可以在单独确认后只交付评测资产；
- acceptance 通过时可以在交付确认后交付生产 Prompt 与评测资产；
- validation 退化、候选无严格改善、连续无改善或轮次耗尽等已经形成评分结论的
  终态没有统一资产出口；
- 同步成功后，周期 worktree、分支和临时状态仍默认保留。

其结果是：正式评分已经完成，确定性报告和评测资产却可能只留在隔离 worktree；
原工作区没有可复用的 `.prompt-evals/<prompt-id>/`，用户也没有面向阅读的结论
简报。长期保留 worktree、周期分支和状态会形成没有明确退出条件的本地残留。

## 2. 统一目标

最终生命周期同时满足以下目标：

1. `tune` 在创建周期 worktree 前自动、无交互地建立本周期需要的仓库本地 Git
   排除规则，并在创建前与创建后分别验证真实 Git ignore 行为。
2. 所有 ignore 初始化和验证都早于评测资产构建、业务确认记录和第一次模型
   调用；业务确认只冻结业务契约、覆盖义务、案例、适配器和评测设置。
3. 所有完成适用正式评分并形成最终业务结论的周期进入同一个 finalization。
4. 每个正式完成周期生成一份独立、确定性、面向用户的 Markdown 评测简报。
5. 每个正式结果都展示结论和计划交付集合，并获得一次明确的评测资产交付确认。
6. acceptance 通过时交付冻结生产 Prompt 和完整可交付评测目录；其他正式结果
   只交付可交付评测目录。
7. 用户拒绝或回答含糊时，原工作区保持不变，并保留已提交证据、worktree、
   周期分支和状态以供检查或以后重新发起交付。
8. 用户确认且同步验证成功后，自动、按安全顺序移除本周期 worktree、清理
   worktree 注册、删除精确周期分支和周期临时状态。
9. 同步或验证失败时不开始清理；清理失败时不撤销已经验证成功的交付，也不
   扩大清理范围。
10. 现有原工作区冲突检测、路径白名单、精确快照、失败回滚、内容哈希验证和
    fixed-model 评测语义保持不变。

## 3. 术语和结果分类

### 3.1 正式完成

“正式完成”表示周期已经完成所有适用的正式评分活动，并形成可向用户报告的
最终业务结论。至少包括：

- `no_change_needed`；
- 候选没有严格改善；
- validation 退化或 validation 门禁未通过；
- 连续无改善而停止；
- 达到候选轮次上限；
- acceptance 失败；
- acceptance 通过并形成可交付冻结候选。

preflight、exclude 初始化、worktree setup、adapter、Schema、协议、传输、模型
服务、资产身份或用户确认失败属于非评分中断。它们继续使用对应错误报告和保留
worktree 的规则，不生成正式简报，也不进入正式结果交付或自动清理。

### 3.2 可交付评测目录

可交付评测目录是 `.prompt-evals/<prompt-id>/` 中已经提交、属于当前周期并通过
身份校验的评测资产、精简历史和独立评测简报。它明确排除：

```text
.runtime/
reports/
__pycache__/
原始模型响应
临时 patch 或 patch manifest
凭据或 Token
```

“同步整个评测目录”仅表示同步这个 allowlisted 可交付文件集合，不表示递归复制
任意运行时文件。

## 4. 全局硬约束与非目标

### 4.1 整个周期的硬约束

- 只处理一个 canonical、仓库相对的 Markdown Prompt；模型、调用配置、重复
  次数、评分公式、候选轮数和 acceptance 只运行一次的语义不变。
- 不修改、暂存、提交或交付目标项目 `.gitignore`；也不修改 `.git/config`、
  全局 Git 配置或全局 excludes。
- 不自动暂存或提交同步到原工作区的文件，不改变用户当前分支、index 或无关
  未提交文件。
- 不把 `reports/`、`.runtime/`、原始响应、逐槽 manifest、凭据、Token、临时
  patch 或 patch manifest 同步到原工作区。
- 不从模型文本、原始响应或调用者提供的任意路径推断 Prompt 身份、结果类型、
  交付路径或清理目标。
- 任一 setup、同步、验证或身份异常都 fail closed；不得回退到原工作区运行
  调优，不得换用较弱的路径或校验规则。

### 4.2 初始化责任的局部约束

- 自动写入仅限当前仓库由 Git 解析出的 repository-local exclude 文件。
- 必须逐字保留用户已有 exclude 内容，不删除、排序、规范化或重写其规则和注释。
- 初始化必须幂等；重复执行不能生成第二个管理块或重复规则。
- 不承诺绕过使 repository-local exclude 失效的项目级否定规则；最终
  `git check-ignore` 结果是安全依据。
- `verify` 仍是只读工作流，不因本设计获得修改 repository-local exclude 的
  权限。

### 4.3 finalization 与清理责任的局部约束

- Markdown 简报不得包含 commit/file/manifest hash、内部绝对 worktree 路径、
  patch 实现细节、Authorization、Token 或原始响应；内部机器状态仍可保存安全
  校验所需哈希。
- 同名简报不得覆盖，也不得用随机改名掩盖周期身份冲突。
- 只有 acceptance 通过的结果可以修改生产 Prompt；其他正式结果禁止修改。
- 只有明确确认、同步已应用且目标验证成功的周期可以自动清理。
- worktree 移除不得使用 `--force`；不得运行 `git reflog expire`、
  `git gc --prune` 或其他仓库级破坏性命令。
- 清理只作用于当前周期精确记录且重新验证的 managed worktree、周期分支和临时
  状态，不能触碰已交付评测目录、远端分支、默认/当前分支或其他周期。

### 4.4 非目标

- 不改变 Prompt、Schema、案例、适配器、评分、停止门禁或验收业务语义。
- 不改变 `reports/`、`.runtime/`、manifest 或候选文件的逻辑位置，也不把它们
  迁移到 worktree 外。
- 不自动删除用户拒绝交付、交付失败、验证失败、旧格式、外部路径或无法通过
  当前状态校验的历史周期。
- 不迁移旧周期状态，不自动处理既有 worktree、分支、报告或候选。
- 不修改通用 `superpowers:using-git-worktrees` skill。
- 不保证已经不可达的 commit 立即从 Git 对象数据库物理抹除。

## 5. 统一生命周期与责任边界

最终 `tune` 生命周期为：

```text
preflight
  -> repository-local exclude initialization
  -> pre-create managed-worktree ignore verification
  -> create and persist dedicated worktree
  -> post-create reports/runtime ignore verification
  -> contract/cases/adapter
  -> business confirmation
  -> model probe/smoke
  -> asset commit
  -> dev/validation baseline
  -> no-change conclusion or candidate loop
  -> optional candidate freeze
  -> optional single acceptance activity
  -> scored terminal outcome
  -> normalize final result and render summary
  -> commit summary and deliverable evaluation assets
  -> show result, summary path, and delivery set
  -> delivery-and-cleanup confirmation
       -> declined/ambiguous: retain cycle unchanged
       -> approved: create result-specific final commit
                    -> fresh-build allowlisted patch
                    -> apply and verify delivery
                    -> clean exact cycle resources
                    -> report delivery and cleanup outcome
```

责任分为四个稳定边界：

1. **本地 exclude 初始化器**：解析并安全更新 repository-local exclude，验证
   创建前目标；不创建评测资产、不请求业务确认、不调用模型。
2. **worktree 生命周期管理器**：创建并持久化专用周期，从 linked worktree
   验证具体运行路径，并维护受控交付/清理入口；不从模型文本接收目标路径。
3. **结果与简报生成器**：把已保存的评分、比较、覆盖和停止证据规范化为正式
   结果并渲染 Markdown；不执行 Git 操作，不读取 raw response 补事实。
4. **结果感知交付与周期清理器**：根据正式结果派生交付 profile，复用并扩展
   现有 stale-state、allowlist、snapshot、apply、rollback 和 hash 验证能力；
   只有交付验证成功后才清理周期拥有的资源。

Skill 文档负责状态转换、展示材料和用户确认；脚本负责确定性解析、渲染、机械
校验、补丁应用和资源清理。

## 6. Repository-local exclude 合同

### 6.1 最终行为

`tune` 必须确保以下三个目标类别被当前仓库的 Git 规则实际忽略：

```gitignore
/.worktrees/stabilizing-prompts/
/.prompt-evals/*/reports/
/.prompt-evals/*/.runtime/
```

规则使用仓库根锚定形式，避免误伤其他层级的同名目录；worktree 规则只覆盖本
Skill 管理的子目录，不要求忽略用户其他 worktree 布局。

已有更宽或语义等价的规则只要能让三个具体目标通过 `git check-ignore`，即可
满足安全合同。实现不必追加固定文本；若无法可靠证明现有覆盖，可以追加上述
精确规则，最终仍以 Git 查询结果为准。

### 6.2 推荐设计及理由

推荐默认设计是在 `preflight` 与 worktree 创建之间加入无交互的
repository-local exclude 初始化：

1. 从 primary workspace 请求 Git 解析 repository-local exclude 路径；不通过
   `.git/info/exclude` 字符串拼接假定实际位置。
2. 将解析结果约束到当前仓库的 shared Git metadata，以兼容普通仓库、linked
   worktree 和 Prompt 仓库自身为 submodule 的场景。
3. 读取并逐字保留原文件；只有实际覆盖不足时才追加管理规则。
4. 原文件非空且没有结尾换行时，先增加一个分隔换行。
5. 使用固定注释标记建立可审计管理块，但不依赖标记判断规则是否生效：

   ```gitignore
   # stabilizing-prompts managed local excludes
   /.worktrees/stabilizing-prompts/
   /.prompt-evals/*/reports/
   /.prompt-evals/*/.runtime/
   ```

6. 在 exclude 文件同目录写临时文件并原子替换；任何写入或替换失败都保留原文件
   并返回精确错误。

该设计复用 Git 自身的仓库本地、非跟踪配置边界，使 primary workspace 和新建
linked worktree 共享规则，同时避免污染项目资产、提交历史或用户全局环境。
满足全部可观察结果和硬约束的替代实现可以替换具体函数拆分、临时文件命名或
管理块写法，但不能改用项目/全局配置边界，不能丢失原子性、幂等性、用户内容
保真和最终 Git 行为验证。

### 6.3 两阶段验证

创建前，初始化器对最终派生的 worktree 路径执行等价于：

```text
git check-ignore --no-index --quiet --
  .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/
```

该检查必须早于创建目录、分支、worktree、周期状态、评测资产或确认记录。失败时
不进行任何周期写入。

创建并持久化 `WorktreeCycle` 后，必须从 linked worktree 的 Git 上下文验证：

```text
.prompt-evals/<prompt-id>/reports/
.prompt-evals/<prompt-id>/.runtime/
```

只有两个具体路径都被 Git 确认 ignored，流程才进入
`contract/cases/adapter`。失败时返回 `setup_error`，报告未通过路径以及
`git check-ignore -v` 的安全诊断；此时尚未生成评测资产、确认记录或调用模型，
但已创建的 worktree 和分支按现有失败策略保留供检查。

写入任何运行产物前仍要防御性复查。如果业务确认后规则被外部修改而使路径失去
保护，当前确认失效并返回 `setup_error`；不得要求用户补规则后复用旧确认。

### 6.4 错误合同

以下情况均是第一次模型调用前的非评分 `setup_error`：

- Git 无法解析 repository-local exclude 文件；
- 文件不可读、不可写，或无法原子替换；
- 解析出的 Git metadata 路径不属于当前仓库；
- 创建前 managed worktree 目标未被忽略；
- 创建后任一具体 `reports/` 或 `.runtime/` 路径未被忽略；
- Git ignore 查询本身异常；
- 确认后防御性复查发现规则漂移。

错误必须包含失败阶段、具体仓库相对路径和可安全展示的 Git 诊断。不得继续生成
候选、写原始响应、修改 `.gitignore`、改用全局 excludes 或回退到原工作区。

### 6.5 候选方案与取舍

- **要求用户预先或确认后手工配置 ignore**：被替换。它把基础设施职责混入
  业务确认，并可能让确认记录先于必要安全条件产生。
- **修改项目 `.gitignore`**：拒绝。它会改变可跟踪项目资产，可能污染用户
  index/提交和交付补丁，违反现有安全边界。
- **修改全局 excludes 或 `.git/config`**：拒绝。它扩大到当前仓库之外，且
  产生难以审计的用户环境副作用。
- **只检查 `.worktrees/`**：拒绝。它不能证明 linked worktree 自身的运行目录
  被忽略。
- **只检查固定规则字符串或管理注释**：拒绝。更宽规则可能已经有效，项目级
  否定规则也可能使文本存在但最终不生效；Git 的实际查询结果才是依据。
- **把运行产物移到 worktree 外**：拒绝。它改变已批准的资产边界和路径语义，
  且不是解决本地 ignore 缺口所必需。
- **让 `verify` 自动初始化**：拒绝。`verify` 必须保持只读；它仅在写报告或
  缓存前检查路径，未忽略时在任何输出和模型调用之前返回 `setup_error`。

## 7. 正式结果与 Markdown 简报合同

### 7.1 结果规范化

所有 scored terminal outcome 先转换成结构化正式结果，至少携带结果类别、停止
原因、适用评分阶段、Prompt 是否应修改、候选是否通过交付门禁、证据身份和
结果对应交付 profile。结果分类必须覆盖第 3.1 节全部结论，但具体私有枚举名可
替换，只要文件命名稳定且所有可观察分类、路由和安全约束不变。

缺失、互相矛盾或版本不兼容的证据不得被解释成正式结论。生成器 fail closed，
不写正式简报，也不进入交付确认。

### 7.2 简报路径与身份

每个正式完成周期生成一个独立文件：

```text
.prompt-evals/<prompt-id>/evaluation-summaries/
  YYYY-MM-DD-HHMMSS-<normalized-result>.md
```

时间来自周期记录的固定 UTC 结束时间；文件名只使用 ASCII 数字、连字符和稳定
规范化结果名。目标已存在表示身份冲突，finalization 必须停止，不覆盖、不随机
改名。

### 7.3 内容合同

简报使用稳定章节；未运行的阶段必须说明规则原因，不能伪造零值：

1. **评测对象**：Prompt 名称和仓库相对路径、运行模式、固定模型名称。
2. **结论**：最终结果、停止原因、生产 Prompt 是否需要修改、候选是否达到
   交付门禁。
3. **测试力度**：dev/validation/acceptance 案例数，各 split 重复次数、计划与
   完成调用数，adapter smoke 结果，acceptance 是否按规则运行。
4. **覆盖面**：业务类别和关键边界、机械覆盖矩阵完整性、near-duplicate 审查、
   明确排除及理由、饱和性结论的用户可读摘要。
5. **结果指标**：通过、解析错误、Schema 错误、业务错误数量；Schema 合法率、
   单次正确率、稳定案例率；修复、退化、稳定性退化和未变化案例数。
6. **失败摘要**：可采取行动的失败类别、代表案例和字段级差异摘要，不含原始
   模型响应。
7. **Prompt 结果**：保持原样、候选被拒绝及原因，或候选通过全部适用门禁并
   等待/获得交付确认。

### 7.4 确定性数据来源

简报只能消费已保存、身份匹配的确定性证据：

- frozen contract、coverage obligations 和案例统计；
- score report；
- development、validation、acceptance comparison；
- runner manifest 的计划/完成槽位统计；
- 已记录的 smoke、机械覆盖审计、near-duplicate review 和 saturation statement；
- 当前周期的规范化停止原因。

### 7.5 推荐设计及候选方案

推荐使用独立的确定性生成器，将支持版本的报告与周期状态渲染为 Markdown，
并在交付确认前把简报与可交付评测资产一起提交。该边界让相同证据产生稳定内容，
使 Git 层只处理已经确定的文件，不让自然语言生成承担身份或路由职责。替代实现
可以更换内部模块或模板，只要保持完整内容合同、确定性、fail-closed 证据校验和
敏感信息排除。

- **让模型自由读取 raw response 并总结**：拒绝。它会产生新事实、非确定输出
  和敏感内容泄漏风险。
- **只保留机器 JSON/报告**：拒绝。它不能提供要求的用户可读结论，也不能形成
  独立、可交付的周期摘要。
- **覆盖一个固定 summary 文件**：拒绝。它丢失周期历史并掩盖身份冲突。
- **同名时随机改名**：拒绝。它规避而不是暴露周期身份冲突。
- **对非评分中断生成正式简报**：拒绝。此类失败没有完整正式评分证据，继续
  使用既有错误报告和保留策略。

## 8. 结果感知的确认与交付合同

### 8.1 交付矩阵

| 正式结果 | 生产 Prompt | 可交付评测目录 | Acceptance |
| --- | ---: | ---: | --- |
| `no_change_needed` | 不交付 | 交付 | 不运行 |
| 候选无改善、连续无改善或轮次停止 | 不交付 | 交付 | 不运行 |
| validation 退化或门禁未通过 | 不交付 | 交付 | 不运行 |
| acceptance 失败 | 不交付 | 交付 | 已运行一次 |
| acceptance 通过 | 交付冻结候选 | 交付 | 已运行一次 |

每一种正式结果都必须先展示简报结论、简报仓库相对路径和计划交付集合，再请求
一次明确确认。确认同时授权该结果对应的同步，以及仅在同步验证成功后执行的精确
周期清理；它不授权更宽的文件或 Git 操作。

acceptance 通过时，确认材料还必须保留现有的 paired reports、冻结候选身份、
Prompt diff 和各门禁结果，使用户能够核对即将交付的生产 Prompt。内部 hash 可以
在确认材料和机器状态中用于身份验证，但不得写入面向长期阅读的 Markdown 简报。

拒绝或含糊回答产生统一结果：不生成或应用交付补丁，不修改原工作区，不清理
worktree/分支/状态，并保留已提交简报和证据。

### 8.2 acceptance 通过的 Prompt 语义

只有 acceptance 通过且用户确认时，才把冻结候选作为 worktree 中的生产 Prompt，
更新 `prompt-contract.yaml` 当前 Prompt 的非路径字段，追加精简历史并创建最终
交付 commit。committed contract 必须继续指向周期开始时的 canonical Prompt
path；任何路径重定向都拒绝交付。

### 8.3 交付前门禁

构建 patch 前必须重新验证：

- 原工作区仍是周期记录的 primary workspace；
- 原工作区 `HEAD == cycle_base_commit`；
- worktree 是固定 managed root 下记录的精确目录；
- worktree 当前分支与周期分支一致，`HEAD` 等于 finalization 记录的最终 commit；
- worktree 干净，没有未提交或仅存在于其中的未跟踪文件；
- committed contract 仍指向周期开始时的 canonical Prompt path；
- 简报属于当前 prompt ID、当前周期和当前正式结果；
- 实际 Git changed paths 与结果派生的白名单完全一致；
- 原工作区每个目标路径都没有用户修改、新增或 staged 冲突。

### 8.4 patch、apply 与验证

推荐继续复用现有结果感知交付边界，并扩充其 allowlist 以包含当前 prompt ID 的
可交付资产和 evaluation summary：

1. 从 `cycle_base_commit`、当前最终 worktree commit 和已提交内容实时生成
   canonical patch；不信任预先持久化的 patch 文本、manifest 或 hash 作为信任锚。
2. 所有正式结果可同步当前 prompt ID 的可交付评测文件；只有 acceptance 通过
   profile 可同步 canonical production Prompt。
3. 拒绝删除、目录逃逸、其他 prompt ID、`reports/`、`.runtime/`、
   `__pycache__`、额外 patch section 或任意无关文件。
4. apply 前执行 `git apply --check` 并为精确目标建立快照；apply 后验证实际路径
   和目标内容。
5. 任一步骤失败都恢复精确快照，确保没有部分交付，并且不启动清理。
6. 成功文件保持 unstaged/uncommitted；不运行 `git add`、commit、merge 或
   checkout。

复用该责任边界是推荐默认设计，因为它继承现有 cycle-base 身份、stale-state
guard、canonical prompt 约束、原生 Git patch 解析、精确 snapshot 和回滚行为。
具体私有 helper、字段和 patch 传递对象可替换；替代方案必须保留完整行为包、
结果路由和全部失败语义，不能只实现“复制到最终路径”的表面效果。

### 8.5 候选方案与取舍

- **保留不同终态的独立出口**：被替换。它使部分正式结果没有资产交付与简报，
  也让确认语义不一致。
- **`no_change_needed` 直接返回且不交付**：被替换。没有 Prompt 变化不等于评测
  资产没有复用价值。
- **acceptance 失败专用 failure-asset-only 旁路**：被统一为非成功正式结果的
  资产 profile。候选仍不得交付，但确认、简报、allowlist 和清理语义应一致。
- **所有正式结果都交付生产 Prompt**：拒绝。非 acceptance-pass 结果没有通过
  生产 Prompt 交付门禁。
- **信任持久化 patch/manifest 或递归复制目录**：拒绝。它可能绕过最新工作区
  冲突、实际 Git changed paths 和运行目录排除。
- **同步时自动 stage/commit 或切换分支**：拒绝。它侵犯原工作区状态并改变
  现有交付合同。
- **同步失败后保留部分文件**：拒绝。交付必须精确回滚，清理也必须保持未启动。

## 9. 自动清理合同

### 9.1 启动条件

清理器只有在以下条件同时成立时运行：

- 用户明确确认本次结果的交付及交付成功后的清理；
- canonical patch 已应用；
- 交付路径和目标内容验证成功；
- 原工作区已交付文件仍通过刚完成的验证；
- worktree 仍对应记录的精确周期目录、周期分支和最终 commit；
- worktree 干净。

任何同步或验证错误发生在清理之前并阻止清理。

### 9.2 推荐顺序与安全理由

清理必须从原工作区或其他位于目标 worktree 之外的目录执行：

```text
git worktree remove <exact-cycle-worktree>
-> git worktree prune
-> git branch -D <exact-cycle-branch>
-> delete exact cycle/confirmation/delivery temporary state files
```

该顺序按可恢复性排列：先让 Git 安全验证并移除 worktree，再清理注册，然后删除
不会被 merge 的周期分支引用，最后删除诊断和恢复所需状态。

- `git worktree remove` 不使用 `--force`；若发现未提交或未跟踪文件，停止并列出
  风险文件。
- worktree 必须是 `WorktreeCycle` 记录、位于固定 managed root 且本次重新验证
  的精确路径。
- 分支必须是本周期记录的 `stabilizing-prompts/...` 分支，名称和 commit 都匹配；
  当前分支、默认分支、远端分支和其他 worktree 使用的分支一律拒绝。
- `git branch -D` 是推荐且获准的操作：评测资产通过 patch 交付为原工作区未提交
  文件，周期 asset/finalization commit 不会 merge；用户确认必须明确覆盖永久
  删除这个可见本地分支引用。
- 状态文件最后删除，且只删除周期精确记录的 cycle、confirmation 和 delivery
  临时状态。
- 不删除或改写 `.prompt-evals/<prompt-id>/` 中已交付内容。

具体清理函数和逐步状态字段可以替换，但执行顺序、精确目标派生、重新验证、
非强制 worktree 删除、分支保护和部分失败语义是合同的一部分。

### 9.3 部分失败

清理不与 patch apply 构成一个原子事务。交付已经验证成功后，任何清理失败都不
回滚原工作区资产：

- worktree 移除失败：分支和状态均保留；
- prune 失败：分支和状态均保留；
- 分支删除失败：已交付资产不变，状态保留并记录精确剩余分支；
- 状态删除失败：报告残留的精确状态文件，不影响已交付资产。

失败后不猜测替代目标、不扩大范围、不执行仓库级破坏性命令。重试必须重新检查
当前 Git 身份、worktree 清洁度和已完成的交付证据，不能仅凭旧路径重放命令。

### 9.4 候选方案与取舍

- **永久保留所有成功周期资源**：被替换。它没有稳定退出条件，会持续积累本地
  worktree、分支和状态。
- **在交付前清理**：拒绝。失败时会丢失诊断和可恢复状态，也无法证明原工作区
  已获得完整资产。
- **同步/验证失败后仍清理**：拒绝。它会删除修复、检查和重试所需证据。
- **使用 `git worktree remove --force`**：拒绝。它可能丢弃未提交或未跟踪内容。
- **使用调用者给出的任意 worktree、branch 或状态路径**：拒绝。清理目标必须
  从经过验证的周期状态派生。
- **使用安全分支删除 `-d`**：不采用为默认。周期 commit 有意不 merge，`-d`
  会阻止正常成功清理；受严格身份校验和明确用户授权保护的 `-D` 符合该生命周期。
- **清理失败时回滚已验证交付**：拒绝。清理与交付不是同一原子事务，回滚会
  破坏用户已经确认并验证成功的成果。
- **运行 reflog expire 或 Git GC 立即擦除对象**：拒绝。范围过宽且不是释放
  周期可见资源所必需。

## 10. 状态模型与 CLI 合同

`WorktreeCycle` 或关联 finalization 状态需要表达：

- 原工作区、managed worktree、周期分支、`cycle_base_commit` 和 canonical
  Prompt 身份；
- repository-local exclude 初始化与两阶段验证结果；
- 规范化正式结果、固定结束时间和简报仓库相对路径；
- finalization commit 与结果对应的交付 profile；
- 交付确认状态；
- apply、目标验证与 rollback 状态；
- cleanup 授权及 worktree remove、prune、branch delete、state delete 的逐步状态；
- 本周期精确的 cycle、confirmation 和 delivery 临时状态文件集合。

这些机器状态可以包含内部安全哈希；简报的无哈希约束不适用于它们。

清理入口的稳定命令形状为：

```text
manage_worktree.py cleanup --state STATE_PATH
```

该入口不接受调用者提供的 worktree、branch 或删除路径。缺少“用户已授权清理”
或“交付已应用且验证成功”状态时必须 fail closed。

exclude 初始化和两阶段验证应由受控 worktree 管理接口提供；具体子命令拆分可以在
实现计划中决定，但必须支持第 6 节的原子写入、诊断、调用顺序和 setup-error
合同。

### 10.1 迁移与兼容性

新状态和自动清理只适用于包含这里所需 finalization/cleanup 字段的新周期。旧
worktree、旧周期状态、已结束分支和外部路径不自动迁移或清理，必须由用户检查后
另行处理。

现有 `.prompt-evals/<prompt-id>/` 继续有效。新周期只在其
`evaluation-summaries/` 下追加独立文件，并沿用既有资产身份和 allowlist 规则；
原工作区已有同名简报或与当前周期资产冲突时，交付停止并保留周期 worktree。

## 11. 端到端流程

### 11.1 新仓库缺少全部 Skill 规则

1. preflight 验证仓库和 Prompt 身份。
2. Git 解析当前仓库 shared metadata 中的 local exclude；初始化器原子追加管理
   块并保留用户内容。
3. 最终 managed worktree 目标通过创建前 ignore gate。
4. 创建、持久化专用 worktree；从其 Git 上下文验证当前 prompt ID 的
   `reports/` 和 `.runtime/`。
5. 构建并审查业务资产，创建业务确认记录，然后才允许第一次模型调用。

用户不会看到 ignore 配置门禁，项目 `.gitignore` 和 Git 状态不因初始化而改变。

### 11.2 基线全部通过

1. dev/validation 的全部计划槽位完成并通过；不生成候选、不运行 acceptance。
2. 结果规范化为 `no_change_needed`，生成并提交独立简报和可交付评测资产。
3. 展示结论、简报路径和资产-only 交付集合，请求一次确认。
4. 拒绝时保留整个周期；同意时只同步可交付评测目录。
5. apply 和内容验证成功后，按顺序清理精确周期资源。

### 11.3 候选没有通过 validation 或轮次停止

已完成的正式比较产生对应结论和简报。交付 profile 只包含可交付评测目录，禁止
修改生产 Prompt；确认、同步、回滚和成功后清理与其他正式结果一致。

### 11.4 acceptance 失败

acceptance 保持每周期一次，不编辑候选也不重跑。正式结果记录失败原因，简报
说明已运行的 acceptance 力度和可行动失败摘要。确认后只交付评测目录；冻结
候选和生产 Prompt 均不交付。交付验证成功后才能清理。

### 11.5 acceptance 通过

结果记录所有适用门禁已通过。确认后在 worktree 建立包含冻结生产 Prompt、
contract 非路径字段、精简历史、评测资产和简报的最终 commit；实时构建 success
profile patch。同步和内容验证成功后再清理精确周期资源。

### 11.6 setup、交付或清理失败

- exclude/setup 失败：第一次模型调用前停止；创建前失败不留周期资源，创建后
  路径验证失败只保留已创建 worktree/分支供检查。
- patch preflight/apply/目标验证失败：恢复精确快照，原工作区无部分交付，保留
  worktree、分支和状态，不运行清理。
- 清理失败：保留已验证交付，停止后续危险步骤并保留足够诊断状态。

## 12. 测试与验收

### 12.1 Local exclude 单元与 Git 行为

- 空 local exclude 得到一个完整管理块；已有用户内容逐字保留；缺少结尾换行时
  正确分隔。
- 第二次初始化字节不变；已有等价/更宽规则不重复追加；只有部分覆盖时只补足
  缺口。
- 写入或原子替换失败时原文件不变并返回 `setup_error`。
- `.gitignore`、`.git/config` 和全局配置始终不变。
- managed worktree 目标通过创建前 gate；linked worktree 中两个具体运行目录
  通过创建后 gate。
- 项目否定规则导致最终未忽略时 fail closed，并提供 `check-ignore -v` 诊断。
- submodule Prompt 仓库解析自身 shared Git metadata；包含空格和 Windows 分隔符
  的路径通过参数数组安全传给 Git。
- 初始化与验证先于资产构建、确认记录和 transport call；创建前失败不产生
  worktree/分支/确认/调用，创建后验证失败只允许保留 worktree 和分支。
- 初始化成功后 manifest、原始响应、报告和候选不出现在 worktree
  `git status --short` 中。
- `verify` 未发现已忽略输出路径时保持只读失败，不修改 local exclude。

### 12.2 简报单元测试

- 每种正式结果生成唯一独立 Markdown；固定证据产生确定性内容。
- 简报包含测试力度、覆盖面、指标、停止原因、失败摘要和 Prompt 结论。
- acceptance 未运行时显示规则原因，不伪造指标。
- 缺失、矛盾或不兼容证据 fail closed。
- 简报不含 SHA/hash 字段、绝对 worktree 路径、Authorization、Token、raw
  response 或 patch 细节。
- 用户输入和案例摘要经过安全、稳定的 Markdown 文本渲染。
- 已存在同名文件停止 finalization，不覆盖或随机改名。

### 12.3 正式结果矩阵

- `no_change_needed` 生成简报并请求资产交付确认。
- validation 退化、无改善、连续无改善和轮次耗尽生成简报且不交付 Prompt。
- acceptance 失败生成简报且不交付 Prompt；acceptance 通过交付精确冻结 Prompt。
- 每个结果的拒绝/含糊路径都保持原工作区不变并保留 worktree、分支和状态。
- 每个结果的同意路径同步预期可交付评测资产；只有 acceptance 通过修改生产
  Prompt。
- `reports/`、`.runtime/`、raw response、`__pycache__` 和其他 prompt ID 永不
  进入 patch。

### 12.4 交付与清理

- 同步文件保持 unstaged/uncommitted；无关 `.gitignore`、`data/` 或其他脏文件
  逐字和状态不变。
- 目标冲突、原工作区 `HEAD` 漂移、worktree 身份漂移、extra changed path、
  contract path 重定向或 hash 验证失败都会精确回滚，且不启动清理。
- worktree 不干净时拒绝非强制删除，保留分支和状态并列出风险文件。
- 成功交付后只移除精确周期 worktree，然后 prune、删除精确周期分支和状态。
- 当前/默认/远端分支、其他 worktree 和其他周期不受影响。
- 每个清理步骤失败都停止后续危险步骤，并按第 9.3 节保留诊断状态和已交付资产。
- 清理实现从不调用 reflog expire、Git GC 或任意目标递归删除。
- 清理重试重新验证 Git 身份和交付证据，不信任过期路径。

### 12.5 文档和行为合同

- `SKILL.md`、worktree 生命周期参考和 CLI help 对自动 local-exclude 初始化、
  两阶段验证、统一正式完成、交付 profile、简报、拒绝保留及成功后清理一致。
- 删除“用户必须在 `tune` 前手工建立 ignore rule”、“no-change 不交付”和“所有
  worktree 永不自动清理”的旧断言。
- 保留“非评分中断和用户拒绝不清理”、“不修改/交付 `.gitignore`”以及
  “`verify` 不写 local exclude”的断言。
- CLI、allowlist 和 fixture 包含 evaluation summaries，但继续排除所有运行时
  目录。

### 12.6 最终验收标准

1. 缺少 stabilizing-prompts ignore 规则、其他前置条件有效的仓库运行 `tune`
   时，无需用户手工编辑任何 ignore 文件即可到达业务确认。
2. 用户原有 local exclude 内容保持不变，只增加必要且不重复的规则；项目
   `.gitignore` 内容和 Git 状态不因初始化改变。
3. 用户看到业务确认前，managed worktree、具体 `reports/` 和 `.runtime/` 路径
   均已由 Git 验证为 ignored；确认后不再出现初始 ignore 配置交互。
4. 任一初始化或验证失败发生在第一次模型调用之前，并给出可诊断
   `setup_error`。
5. 所有正式评分终态都通过统一 finalization；非评分中断不会误入。
6. 每个正式完成周期生成一份独立、无哈希、确定性的用户 Markdown 简报，并在
   同步前取得一次明确交付与成功后清理确认。
7. 拒绝或含糊确认时原工作区不变，worktree、分支、简报、证据和状态完整保留。
8. 非 acceptance-pass 结果只同步当前 prompt ID 的可交付评测内容；
   acceptance-pass 同步精确冻结生产 Prompt 和可交付评测目录。
9. 同步保持 unstaged/uncommitted，不影响无关脏文件、当前分支或 index；失败时
   精确回滚且不清理。
10. 同步验证成功后，按序清理精确周期 worktree、Git 注册、周期分支和临时
    状态；局部清理失败不回滚交付并保留可诊断状态。
11. `reports/`、`.runtime/`、manifest、原始响应、凭据和 Token 既不会进入资产
    commit 的可交付集合，也不会进入 delivery patch。
12. 单元、Git 行为、集成、结果矩阵、回滚、逐步清理失败和文档契约测试共同
    证明上述生命周期。

## 13. 预期影响范围

稳定架构边界要求实现至少影响以下组件；具体私有函数拆分由实现计划决定：

- worktree 管理脚本：repository-local exclude 解析/原子初始化、两阶段验证、
  finalization 状态、result-aware delivery 和受控 cleanup CLI；
- 独立确定性简报生成模块或脚本；
- Skill 主状态机与 worktree 生命周期参考；
- worktree、行为合同、`tune` 集成和文档契约测试及必要 fixture；
- delivery allowlist 和 CLI help，使 evaluation summaries 可交付而运行时目录继续
  被拒绝。

无需修改固定模型客户端、评分公式、case/schema 业务合同或通用 worktree skill，
除非实现过程中发现它们与这里明确的稳定责任边界存在真实接口依赖。
