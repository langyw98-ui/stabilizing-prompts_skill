# Stabilizing Prompts 本地排除规则自动初始化设计

日期：2026-09-16  
状态：已批准，待实现

## 1. 背景

`stabilizing-prompts` 的 `tune` 流程在目标仓库内创建专用 linked worktree，
并把模型原始响应、运行 manifest、报告和临时候选分别保存在
`.prompt-evals/<prompt-id>/reports/` 与
`.prompt-evals/<prompt-id>/.runtime/`。

现有流程在创建 worktree 前只验证 `.worktrees/` 已被 Git 忽略。另一方面，
Skill 又要求 `reports/` 和 `.runtime/` 必须位于已忽略路径，并禁止修改目标
仓库的 `.gitignore`。这两个要求之间缺少初始化动作和明确的前置检查状态。

因此，当用户已经审阅并冻结业务契约、案例和适配器后，执行器可能才发现
linked worktree 内的运行目录未被忽略，并要求用户手工修改
`.git/info/exclude`。这形成了一个与业务确认无关的第二次交互，也使已生成的
确认记录对应一个尚未通过基础设施前置条件的周期。

`.worktrees/` 的忽略规则不能替代 linked worktree 内部的运行目录规则：前者
只使主工作区忽略整个 worktree 目录；在 linked worktree 自身的 Git 视图中，
`reports/` 和 `.runtime/` 仍可能显示为未跟踪文件。

## 2. 目标

- `tune` 自动建立本周期所需的仓库本地 Git 排除规则。
- 不修改、暂存、提交或交付目标仓库的 `.gitignore`。
- 不覆盖或重写用户已有的 `.git/info/exclude` 内容。
- 所有 ignore 初始化和验证在评测资产构建、用户确认和第一次模型调用之前完成。
- 用户确认只冻结业务契约、覆盖义务、案例、适配器和评测设置，不再承担基础
  设施配置职责。
- 重复执行初始化保持幂等，不重复追加规则。
- 初始化失败时 fail closed，不回退到原工作区执行调优。

## 3. 非目标

- 不改变 Prompt、Schema、案例、评分、候选轮数或验收语义。
- 不改变 `reports/`、`.runtime/`、manifest 或候选文件的现有逻辑路径。
- 不把运行产物迁移到 worktree 外部。
- 不自动删除既有 worktree、分支、报告或候选。
- 不管理用户的全局 Git excludes 文件。
- 不承诺覆盖阻止 `.git/info/exclude` 生效的项目级否定规则；此类冲突由最终
  `git check-ignore` 验证发现并报告。
- 不放宽 `verify` 只能写入已忽略报告或缓存路径的约束。

## 4. 根因

问题由三项规则演进后未完整衔接造成：

1. 早期设计允许向项目 `.gitignore` 添加评测运行目录规则。
2. worktree 安全加固后来禁止 Skill 修改或交付目标 `.gitignore`。
3. 加固只为 `.worktrees/` 增加了明确的前置 ignore gate；
   `reports/` 和 `.runtime/` 仍以“已经被忽略”为隐含前提，没有初始化命令、
   状态转换或对应的自动化测试。

当前脚本因此只能确定 worktree 目标目录是否被忽略。运行目录要求主要存在于
Skill 文档中，执行器只能在即将写入 manifest、报告或候选时临时解释该要求，
从而产生确认后的额外交互。

## 5. 设计概述

在 `preflight` 与 worktree 创建之间增加一个无用户交互的
`local exclude initialization` 步骤：

```text
preflight
  -> resolve shared repository-local exclude file
  -> idempotently ensure managed exclude rules
  -> verify future worktree target is ignored
  -> create and persist dedicated worktree
  -> verify concrete reports/runtime paths from the linked worktree
  -> contract/cases/adapter
  -> user confirmation
  -> model probe/smoke
```

初始化只更新 Git 的仓库本地、非跟踪配置文件 `.git/info/exclude`。Skill 继续
禁止修改项目 `.gitignore`。所有写入均属于 `tune` 已授权的本地周期初始化，
不单独请求业务确认。

## 6. 管理的排除规则

初始化器确保以下三条规则存在：

```gitignore
/.worktrees/stabilizing-prompts/
/.prompt-evals/*/reports/
/.prompt-evals/*/.runtime/
```

规则采用仓库根锚定形式，避免误伤名称相同但位于其他层级的目录。
`.worktrees/` 只覆盖本 Skill 管理的子目录，不要求忽略用户的其他 worktree
布局。

现有更宽规则只要能使三个具体目标通过 `git check-ignore`，即可满足安全要求；
实现不必为语义等价的已有规则再追加固定文本。若无法可靠证明已有规则覆盖，
可以追加上述精确规则，然后以最终 Git 查询结果为准。

## 7. exclude 文件解析与写入

### 7.1 路径解析

初始化器从 primary workspace 调用 Git 解析 repository-local exclude 文件，
不得通过字符串拼接假定其位置。实现使用 Git 报告的路径，并将其解析为当前
仓库的 shared Git metadata 路径，以兼容普通仓库和 Prompt 仓库本身是
submodule 的情况。

linked worktree 不拥有独立的项目资产副本；初始化后的共享规则必须同时对
primary workspace 和新建 linked worktree 生效。创建 worktree 后必须从该
worktree 再次验证，不能只根据文件内容推断生效范围。

### 7.2 保留用户内容

写入遵循以下约束：

- 读取并保留原始文件的全部既有内容。
- 不删除、排序、规范化或改写用户规则和注释。
- 仅在缺少有效覆盖时追加 Skill 管理的规则。
- 若原文件非空且结尾没有换行，先补一个换行再追加。
- 使用固定注释标记管理块，便于审计，但不依赖标记判断规则是否实际生效。
- 再次运行时不得创建第二个管理块或重复规则。
- 使用同目录临时文件和原子替换，避免留下部分写入内容。
- 写入或替换失败时保留原文件，并返回精确错误。

建议的管理块为：

```gitignore
# stabilizing-prompts managed local excludes
/.worktrees/stabilizing-prompts/
/.prompt-evals/*/reports/
/.prompt-evals/*/.runtime/
```

实现可以在已有规则已覆盖全部目标时不写入该块。

### 7.3 权限与安全边界

`.git/info/exclude` 是本地仓库元数据，不是项目资产，不进入提交、交付白名单
或补丁。初始化动作不得改写 `.git/config`、全局 Git 配置或全局 excludes。

如果环境不允许写入 repository-local exclude 文件，`tune` 在任何 worktree、
评测资产、确认记录或模型调用产生之前返回 `setup_error`。错误消息应指出解析
后的目标文件和失败原因，但不把手工命令包装成确认后的新门禁。

## 8. 两阶段验证

### 8.1 创建前验证

完成初始化后，对派生出的最终 worktree 目录执行：

```text
git check-ignore --no-index --quiet --
  .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/
```

检查必须发生在创建目录、分支或 worktree 前。失败时不进行任何周期写入。

### 8.2 创建后验证

worktree 创建并持久化 `WorktreeCycle` 后，从 linked worktree 的 Git 上下文对
本周期的具体路径执行 `git check-ignore --no-index`：

```text
.prompt-evals/<prompt-id>/reports/
.prompt-evals/<prompt-id>/.runtime/
```

只有两个路径均被 Git 确认忽略，流程才能进入
`contract/cases/adapter`。验证不能仅检查固定规则字符串，因为项目中更高优先级
的 `.gitignore` 否定规则可能改变最终结果。

若创建后验证意外失败，返回 `setup_error`，报告具体未通过路径和
`git check-ignore -v` 的诊断信息。此时尚未生成评测资产、确认记录或调用模型。
已创建 worktree 和分支沿用现有失败策略保留供检查，不自动删除。

## 9. 状态机与确认语义

调整后的 `tune` 前半段状态为：

```text
preflight
  -> local exclude initialization
  -> worktree creation and runtime-path verification
  -> contract/cases/adapter
  -> user confirmation
  -> model probe/smoke
```

以下不再构成独立用户门禁：

- 添加 `.worktrees/stabilizing-prompts/` 本地排除规则；
- 添加 `reports/` 或 `.runtime/` 本地排除规则；
- 确认这些规则已由 Git 生效。

contract confirmation record 只能在上述基础设施状态全部通过后创建。确认后不再
因缺少 ignore 规则暂停；若确认后规则被外部修改并导致运行路径不再受保护，
写入运行产物前的防御性复查应返回 `setup_error` 并使当前确认失效，而不是要求
用户补规则后沿用旧确认。

## 10. `verify` 行为

`verify` 不创建 worktree，也不修改 Prompt 或评测资产。为了保持只读工作流的
语义，`verify` 不自动修改 `.git/info/exclude`。

`verify` 在写入报告或缓存前检查选定路径已被 Git 忽略：

- 已忽略：继续执行。
- 未忽略：在第一次模型调用和任何输出写入前返回 `setup_error`。

本次变更消除的是 `tune` 周期中的基础设施交互，不扩大 `verify` 的写权限。

## 11. 错误处理

以下情况均返回非评分 `setup_error`：

- 无法通过 Git 解析 repository-local exclude 文件；
- exclude 文件不可读、不可写或无法原子替换；
- 创建前 worktree 目标未被最终规则忽略；
- 创建后 `reports/` 或 `.runtime/` 具体路径未被最终规则忽略；
- Git ignore 查询本身异常；
- 初始化过程中发现目标 Git metadata 路径不属于当前仓库；
- 确认后防御性复查发现规则漂移。

错误必须包含失败阶段、具体仓库相对路径和可安全展示的 Git 诊断。不得继续生成
候选、写原始响应、回退到原工作区、修改 `.gitignore` 或改用全局 excludes。

## 12. 测试要求

### 12.1 初始化单元测试

- 空 `.git/info/exclude` 会得到一个完整管理块。
- 已有用户内容逐字保留。
- 缺少结尾换行时正确分隔追加内容。
- 第二次初始化不改变文件内容。
- 已有等价或更宽规则时不重复追加。
- 只有部分规则存在时补齐缺失覆盖。
- 写入或替换失败时原文件保持不变并返回 `setup_error`。
- 不修改目标仓库 `.gitignore`、`.git/config` 或全局配置。

### 12.2 Git 行为测试

- 新建周期的 managed worktree 目标通过创建前 ignore gate。
- linked worktree 内两个具体运行目录均通过 ignore gate。
- 项目 `.gitignore` 中的否定规则导致最终未忽略时 fail closed。
- submodule 形式的 Prompt 仓库解析并更新自身 shared Git metadata。
- 路径包含空格和 Windows 路径分隔符时仍使用安全的 Git 参数传递。

### 12.3 集成与顺序测试

- 缺少全部规则的仓库可在无额外用户交互的情况下进入业务确认。
- 初始化完成后，manifest、原始响应和候选不会出现在 worktree 的
  `git status --short` 中。
- 所有 ignore 初始化和验证事件早于资产构建、确认记录和 transport call。
- 初始化失败时不存在 worktree、分支、确认记录或 transport call；创建后验证
  失败的场景只允许保留已创建 worktree 和分支。
- 用户确认后正常流程不会再因初始 ignore 配置缺失暂停。
- delivery patch 和 allowlist 始终排除 `.gitignore`、`.git/info/exclude`、
  `reports/` 和 `.runtime/`。
- `verify` 对未忽略输出路径保持只读失败行为。

### 12.4 文档契约测试

- `SKILL.md` 明确说明 `tune` 自动初始化 repository-local excludes。
- `SKILL.md` 不再要求用户在运行 `tune` 前手工建立 ignore rule。
- `worktree-lifecycle.md` 的状态顺序包含初始化和两阶段验证。
- 文档继续明确禁止修改、暂存、提交或交付目标 `.gitignore`。
- 用户确认章节不包含 ignore 配置或手工 Git 命令。

## 13. 涉及文件

预计实现至少修改：

- `scripts/manage_worktree.py`：本地 exclude 初始化、原子写入和两阶段验证接口；
- `SKILL.md`：更新 `tune` 状态机、worktree gate 和确认语义；
- `references/worktree-lifecycle.md`：更新生命周期和失败行为；
- `tests/test_manage_worktree.py`：初始化、幂等、Git 优先级和异常测试；
- `tests/test_behavior_contract.py`：用户可观察行为与调用顺序；
- `tests/test_integration_tune.py`：无手工 ignore 设置的完整 tune 前半段；
- `tests/test_skill_instructions.py`：文档契约和禁止修改 `.gitignore` 的回归测试；
- 必要的 fixture 初始化代码：移除仅为满足旧手工前提而写入的排除规则。

具体函数拆分属于实现计划，不在本设计中固定。

## 14. 验收标准

实现完成后必须同时满足：

1. 一个没有 stabilizing-prompts ignore 规则、但其他前置条件有效的测试仓库，
   执行 `tune` 时无需用户手工编辑任何 ignore 文件。
2. `.git/info/exclude` 中用户原有内容保持不变，只出现必要且不重复的新增规则。
3. 目标仓库 `.gitignore` 的内容和 Git 状态在整个周期中不因初始化改变。
4. 用户看到业务确认前，worktree、`reports/` 和 `.runtime/` 的具体路径均已由
   Git 验证为 ignored。
5. 业务确认后不会再出现要求添加 ignore 规则的独立交互。
6. manifest、原始响应、报告和候选不会被纳入资产提交或交付补丁。
7. 任一初始化或验证失败都发生在第一次模型调用之前，并提供可诊断的
   `setup_error`。

