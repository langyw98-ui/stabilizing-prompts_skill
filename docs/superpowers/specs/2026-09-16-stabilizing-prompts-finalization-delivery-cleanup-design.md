# Stabilizing Prompts 评测简报、统一交付与周期清理设计

日期：2026-09-16  
状态：待实现

## 1. 背景

`stabilizing-prompts` 当前在专用 worktree 中建立并提交评测资产，再执行基线、
候选和 acceptance。不同终态的收尾行为并不一致：

- `no_change_needed` 在开发集和验证集基线全部通过后直接结束；
- acceptance 失败可以在单独确认后交付评测资产；
- acceptance 通过可以在交付确认后交付生产 Prompt 和评测资产；
- validation 退化、候选无改善和轮次耗尽等已形成评分结论的终态没有统一的
  资产交付出口；
- 成功同步后，worktree、周期分支和周期状态默认保留。

因此，一次已经完成正式评分的周期可能只在隔离 worktree 中留下评测资产和
机器报告，原工作区没有可复用的 `.prompt-evals/<prompt-id>/`，用户也没有一份
面向阅读的结果简报。长期保留 worktree、周期分支和状态还会形成无明确退出
条件的本地残留。

本设计为所有正式完成的评测周期增加统一 finalization，并在同一协议内完成：

1. 生成独立的 Markdown 评测简报；
2. 请求评测资产交付确认；
3. 按结果同步生产 Prompt 和/或 `.prompt-evals/<prompt-id>/`；
4. 验证成功后清理本周期的 worktree、分支和临时状态。

## 2. 目标

- 所有完成正式评分并得到最终结论的 `tune` 周期进入同一个 finalization。
- 每个正式完成周期在 `.prompt-evals/<prompt-id>/evaluation-summaries/` 下生成
  一份独立、面向用户的 Markdown 简报。
- 简报聚焦测试力度、覆盖面、指标、失败与 Prompt 结论，不展示内部哈希、
  补丁细节或原始响应。
- 每个正式结果都经过一次明确的评测资产交付确认。
- acceptance 通过时交付生产 Prompt 和完整的可交付评测目录；其他正式结果
  只交付可交付评测目录。
- 用户拒绝交付时，原工作区保持不变，并保留 worktree、周期分支和状态。
- 用户确认且同步验证成功后，自动移除 worktree、清理 worktree 注册、删除
  周期分支和周期临时状态。
- 同步或验证失败时不开始清理；清理失败时不撤销已经验证成功的交付。
- 保留现有原工作区冲突检测、路径白名单、精确快照、失败回滚和哈希验证。

## 3. 非目标

- 不为 preflight、setup、协议、传输、模型不可用或用户取消等非评分中断生成
  正式评测简报。
- 不把 `reports/`、`.runtime/`、原始响应、逐槽 manifest、凭据或 Token 同步到
  原工作区。
- 不在 Markdown 简报中记录 commit hash、文件 hash、manifest hash、内部
  worktree 路径或补丁实现细节。
- 不自动暂存或提交同步到原工作区的文件。
- 不修改用户当前分支、index 或无关未提交文件。
- 不执行 `git reflog expire`、`git gc --prune`，也不保证立即从 Git 对象数据库
  中物理抹除已经不可达的 commit。
- 不自动清理用户拒绝交付、交付失败或验证失败的周期。
- 不迁移或自动清理旧格式、外部路径或无法通过当前状态校验的历史周期。
- 不改变固定模型、调用配置、重复次数、评分公式、候选轮数或 acceptance 只
  运行一次的语义。
- 不修改通用 `superpowers:using-git-worktrees` skill。

## 4. 定义

### 4.1 正式完成

“正式完成”指周期已经完成适用的正式评分活动，并形成可以向用户报告的最终
业务结论。包括：

- `no_change_needed`；
- 候选无严格改善；
- validation 退化或 validation 门禁未通过；
- 连续无改善停止；
- 达到候选轮次上限；
- acceptance 失败；
- acceptance 通过并形成可交付候选。

setup、adapter、Schema、协议、传输、模型服务、资产身份或用户确认失败属于
非评分中断，不进入本设计的正式 finalization。它们继续使用现有错误报告和
保留 worktree 的规则。

### 4.2 可交付评测目录

可交付评测目录是 `.prompt-evals/<prompt-id>/` 中已经提交并通过当前周期身份
校验的资产、精简历史和评测简报。它不包含：

```text
.runtime/
reports/
__pycache__/
原始响应
临时 patch 或 patch manifest
凭据或 Token
```

“同步整个评测目录”指同步该目录内的可交付文件集合，不是绕过白名单递归复制
任意运行时文件。

## 5. 总体架构

所有正式评分终态统一进入 `finalization`，不再从各终态直接返回：

```text
scored terminal outcome
  -> normalize final result
  -> render user summary
  -> commit summary and deliverable evaluation assets
  -> show result and delivery set
  -> delivery confirmation
       -> declined: retain cycle unchanged
       -> approved: finalize result-specific commit
                    -> build allowlisted patch
                    -> apply and verify delivery
                    -> clean cycle resources
                    -> report completion
```

finalization 由三个边界清晰的单元组成：

1. **结果与简报生成器**：把评分报告、比较报告、覆盖审计和停止原因转换为统一
   的正式结果，再渲染 Markdown；不执行 Git 操作。
2. **结果感知的交付器**：根据正式结果选择资产交付或 Prompt + 资产交付，沿用
   现有路径白名单、stale-state guard、快照、应用和验证机制。
3. **周期清理器**：仅消费“交付已成功验证”的周期状态，清理该周期拥有的
   worktree、Git 注册、分支和临时状态；不接触原工作区交付文件。

Skill 文档负责状态转换和用户确认。脚本负责确定性渲染、机械校验、补丁应用和
资源清理，不从模型文本推断路径、结果类型或清理目标。

## 6. Markdown 评测简报

### 6.1 路径与命名

每次正式完成生成独立文件：

```text
.prompt-evals/<prompt-id>/evaluation-summaries/
  YYYY-MM-DD-HHMMSS-<result>.md
```

时间使用周期记录的 UTC 固定结束时间，文件名只使用 ASCII 数字、连字符和
规范化的结果名称。若目标文件已经存在，finalization 必须停止并报告冲突，
不覆盖已有简报，也不通过随机改名掩盖周期身份冲突。

### 6.2 内容

简报使用固定章节；没有适用数据的章节明确说明未运行及原因，不能伪造零值：

1. **评测对象**
   - Prompt 名称和仓库相对路径；
   - 运行模式和固定模型名称。
2. **结论**
   - 最终结果；
   - 停止原因；
   - 是否需要修改生产 Prompt；
   - 候选是否达到交付门禁。
3. **测试力度**
   - dev、validation、acceptance 各自案例数；
   - 各 split 重复次数和计划/完成调用数；
   - adapter smoke 是否通过；
   - acceptance 是否按规则运行。
4. **覆盖面**
   - 覆盖的业务类别和关键边界；
   - 机械覆盖矩阵是否完整；
   - near-duplicate 审查结论；
   - 明确排除及其理由；
   - 饱和性结论的用户可读摘要。
5. **结果指标**
   - 通过、解析错误、Schema 错误和业务错误数量；
   - Schema 合法率、单次正确率、稳定案例率；
   - 修复、退化、稳定性退化和未变化案例数量。
6. **失败摘要**
   - 可采取行动的失败类别；
   - 代表案例和字段级差异摘要；
   - 不包含原始模型响应。
7. **Prompt 结果**
   - 保持原样；
   - 候选被拒绝及原因；或
   - 候选通过全部适用门禁、等待/获得交付确认。

简报不包含任何 Token、Authorization、原始响应、内部绝对路径或哈希值。机器
状态、manifest 和 patch manifest 继续保留安全校验所需的哈希，不因用户简报
省略哈希而削弱交付验证。

### 6.3 数据来源

简报只能消费已保存的确定性证据：

- frozen contract、coverage obligations 和案例统计；
- score report；
- development、validation、acceptance comparison；
- runner manifest 的计划/完成槽位统计；
- 已记录的 smoke、覆盖审计、near-duplicate review 和 saturation statement；
- 当前周期的规范化停止原因。

模型不得自由总结原始响应来生成新的事实。生成器对缺失、相互矛盾或不兼容的
证据 fail closed，不生成“正式完成”简报，也不进入交付确认。

## 7. 结果与交付策略

| 正式结果 | 生产 Prompt | `.prompt-evals/<prompt-id>/` | Acceptance |
|---|---:|---:|---|
| `no_change_needed` | 不交付 | 交付 | 不运行 |
| 候选无改善/轮次停止 | 不交付 | 交付 | 不运行 |
| validation 退化/未通过 | 不交付 | 交付 | 不运行 |
| acceptance 失败 | 不交付 | 交付 | 已运行一次 |
| acceptance 通过 | 交付冻结候选 | 交付 | 已运行一次 |

每一种正式结果都必须先展示简报结论、简报路径和计划交付集合，再请求一次明确
确认。确认同时授权该结果对应的同步，以及同步验证成功后的周期资源清理。

用户拒绝或回答含糊时：

- 不生成或应用交付补丁；
- 不修改原工作区；
- 不清理 worktree、分支或状态；
- 保留已提交简报和证据供检查或以后重新发起交付。

acceptance 通过时沿用现有生产 Prompt 语义：获得确认后才把冻结候选替换为
worktree 中的生产 Prompt，更新 `prompt-contract.yaml` 的当前 Prompt 非路径
字段，追加精简历史并创建最终交付 commit。其他结果禁止修改生产 Prompt。

## 8. 交付协议

### 8.1 交付前门禁

生成补丁前必须重新验证：

- 原工作区仍是周期记录的 primary workspace；
- 原工作区 `HEAD == cycle_base_commit`；
- worktree 位于固定 managed root；
- worktree `HEAD` 等于 finalization 记录的最终 commit；
- worktree 干净，没有只存在于其中的未提交文件；
- committed contract 仍指向周期开始时的 canonical Prompt path；
- 简报路径属于当前 prompt ID，且只对应当前正式结果；
- 实际 Git changed paths 与结果派生的白名单完全一致；
- 原工作区所有目标路径没有用户修改或新增冲突。

### 8.2 补丁和同步

交付器从 `cycle_base_commit`、最终 worktree commit 和已提交内容实时生成补丁，
不信任预先持久化的 patch 文本或 manifest。补丁允许：

- 对所有正式结果同步当前 prompt ID 的可交付评测文件；
- 仅对 acceptance 通过结果同步 canonical production Prompt；
- 新增本周期独立简报；
- 更新允许的评测资产和精简历史。

补丁拒绝删除、目录逃逸、其他 prompt ID、`reports/`、`.runtime/`、`__pycache__`、
额外 patch section 或任何无关文件。apply 前执行 `git apply --check` 并对精确
目标建立快照；apply 后验证实际路径和目标内容。失败时恢复快照，确保没有部分
交付。

成功同步后的文件保持 unstaged/uncommitted。Skill 不运行 `git add`、commit、
merge 或 checkout，不改变当前分支和 index。

## 9. 自动清理协议

### 9.1 启动条件

周期清理器只有在以下条件全部成立时才能运行：

- 用户已明确确认本次结果的交付与后续清理；
- 补丁已经应用；
- 交付路径和目标内容已经验证；
- worktree 当前仍对应记录的周期分支和最终 commit；
- worktree 干净；
- 原工作区中的已交付文件仍通过刚完成的验证。

任何同步或验证错误都发生在清理之前，并阻止清理。

### 9.2 顺序

清理必须从原工作区或另一个位于目标 worktree 之外的目录执行：

```text
git worktree remove <exact-cycle-worktree>
-> git worktree prune
-> git branch -D <exact-cycle-branch>
-> delete exact cycle/confirmation/delivery temporary state files
```

约束如下：

- `git worktree remove` 不使用 `--force`；若 Git 报告未提交或未跟踪文件，停止并
  列出风险文件。
- 只允许删除 `WorktreeCycle` 记录且重新验证过的固定 managed-root 路径。
- 只允许删除该周期记录的 `stabilizing-prompts/...` 分支；当前分支、默认分支、
  远端分支或名称/commit 不匹配的分支一律拒绝。
- 使用 `git branch -D` 是因为评测资产以补丁形式同步为原工作区未提交文件，
  周期 asset/finalization commit 不会被 merge。用户的交付确认必须明确覆盖这项
  永久删除可见分支引用的动作。
- 状态文件最后删除，且只删除本周期精确记录的 cycle、confirmation 和 delivery
  临时文件。
- 不删除 `.prompt-evals/<prompt-id>/`，不修改其交付内容，也不清理其他周期。
- 不执行 reflog 过期或 Git 对象垃圾回收；不可达对象由 Git 正常维护周期处理。

### 9.3 部分清理失败

清理不是与 Git 补丁应用同一个原子事务。交付已经验证成功后，清理阶段失败不
回滚原工作区资产。清理器按可恢复性排序：先移除 worktree，再 prune，之后删除
分支，最后删除状态。

- worktree 移除失败：分支和状态均保留；
- prune 失败：分支和状态均保留；
- 分支删除失败：已交付资产不变，状态保留并记录剩余分支；
- 状态删除失败：报告残留的精确状态文件，不影响已交付资产；
- 不继续猜测或扩大清理范围，不使用仓库级破坏性命令补救。

再次清理必须重新检查当前 Git 状态和已完成的交付证据，不能仅凭旧路径重放
命令。

## 10. 状态模型与 CLI

`WorktreeCycle` 或关联的 finalization 状态需要增加：

- 规范化正式结果；
- 简报仓库相对路径；
- finalization commit；
- 结果对应的交付 profile；
- 交付确认状态；
- apply 和目标验证状态；
- cleanup 授权和逐步完成状态；
- 精确的周期状态文件集合。

这些字段仍可包含机器校验所需的哈希；“简报不含哈希”不适用于内部状态。

`manage_worktree.py` 增加受控清理入口，稳定命令形状为：

```text
manage_worktree.py cleanup --state STATE_PATH
```

该命令不接受调用者提供的任意 worktree、branch 或删除路径。所有目标都从经过
验证的周期状态派生。没有“交付已应用且验证成功”和“用户已授权清理”的状态
时，命令 fail closed。

简报生成应使用独立的确定性脚本或模块，输入为受支持的报告和周期状态，输出为
目标 Markdown 文件。它不得读取 raw response 来补全缺失结论。

## 11. Skill 状态机调整

调整后的后半段状态机为：

```text
dev/validation baseline
  -> no-change exit or candidate loop
  -> optional candidate freeze
  -> optional single acceptance activity
  -> scored terminal outcome
  -> finalization summary
  -> evaluation delivery confirmation
  -> result-specific final commit
  -> allowlisted synchronization
  -> verified cycle cleanup
```

`no_change_needed` 不再是跳过交付的直接返回点。acceptance 失败也不再拥有特殊
的“failure-asset-only”旁路；它与其他非成功正式结果共用资产交付 profile。
acceptance 通过继续使用包含生产 Prompt 的成功 profile。

非评分中断保留原停止语义，不进入 `finalization summary`。

## 12. 测试策略

### 12.1 简报单元测试

- 每个正式结果生成唯一的独立 Markdown 文件；
- 固定证据产生确定性内容；
- 简报包含测试力度、覆盖面、指标、停止原因和 Prompt 结论；
- acceptance 未运行时给出规则原因，不显示伪造指标；
- 缺失或矛盾证据 fail closed；
- 简报不包含 SHA/hash 字段、绝对 worktree 路径、Authorization、Token 或原始
  响应；
- Markdown 对用户输入和案例摘要进行安全、稳定的文本渲染。

### 12.2 结果矩阵集成测试

至少覆盖：

- `no_change_needed` 生成简报并请求资产交付确认；
- validation 退化、无改善和轮次耗尽生成简报且不交付 Prompt；
- acceptance 失败生成简报且不交付 Prompt；
- acceptance 通过生成简报并交付冻结 Prompt；
- 每个结果的用户拒绝路径都保留 worktree、分支和状态，原工作区不变；
- 每个结果的用户同意路径都同步预期评测资产；
- 只有 acceptance 通过路径修改生产 Prompt；
- `reports/`、`.runtime/`、raw response 和其他 prompt ID 永不进入补丁。

### 12.3 交付与清理测试

- 同步后的文件保持 unstaged/uncommitted；
- 无关 `.gitignore` 修改、`data/` 或其他脏文件保持原样；
- 目标路径冲突、`HEAD` 漂移、额外 changed path 或 hash 验证失败会回滚同步，且
  不启动清理；
- worktree 不干净时拒绝非强制删除，并保留分支和状态；
- 成功交付后只移除精确周期 worktree，随后 prune、删除精确周期分支和状态；
- 当前分支、默认分支、远端分支和其他 worktree 不受影响；
- 每一个清理步骤的失败都会停止后续危险步骤并保留可诊断状态；
- 清理逻辑不调用 reflog expire 或 Git GC；
- 重试清理时重新验证身份，而不是信任过期路径。

### 12.4 文档与行为契约测试

- `SKILL.md`、`references/worktree-lifecycle.md` 和脚本 CLI 对正式完成、交付 profile、
  简报、拒绝保留及成功后清理保持一致；
- 删除旧的“no-change 不交付”和“所有 worktree 永不自动清理”断言；
- 保留“非评分中断和用户拒绝不清理”的断言；
- CLI help、允许列表和测试 fixture 包含 evaluation summaries，但仍拒绝运行时
  目录。

## 13. 迁移与兼容性

新流程只适用于包含新 finalization/cleanup 状态字段的新周期。已有 worktree、
旧周期状态和已结束分支不自动迁移或清理；用户可以人工检查后处理。

现有 `.prompt-evals/<prompt-id>/` 继续有效。新周期在其
`evaluation-summaries/` 下追加独立文件，不覆盖已有资产或历史简报。若原工作区
已有同名简报或与当前周期资产发生冲突，交付停止并保留 worktree。

## 14. 验收标准

1. 所有正式评分终态都通过统一 finalization，且非评分中断不会误入该流程。
2. 每个正式完成周期生成一份独立、无哈希、面向用户的 Markdown 简报。
3. 每个正式结果都在同步前取得明确交付确认。
4. 用户拒绝时原工作区不变，worktree、分支和周期状态完整保留。
5. 除 acceptance 通过外的其他正式结果只同步
   `.prompt-evals/<prompt-id>/` 的可交付内容。
6. acceptance 通过且用户确认时，同步冻结生产 Prompt 和可交付评测目录。
7. 所有同步结果保持 unstaged/uncommitted，且不影响无关脏文件、当前分支和
   index。
8. 同步或验证失败时精确回滚，且不执行任何周期清理。
9. 同步验证成功后自动、按序清理精确周期 worktree、注册、分支和临时状态。
10. 清理不使用强制 worktree 删除，不运行 reflog 清除或仓库级垃圾回收，也不
    触碰其他周期或分支。
11. `reports/`、`.runtime/`、原始响应、凭据和 Token 永不进入交付资产。
12. 单元、集成、行为契约和文档测试覆盖结果矩阵、拒绝路径、同步回滚和逐步
    清理失败。
