# Worktree Lifecycle

This reference preserves sections 12–15 of the approved design.

## 12. 两种用户运行模式

### 12.1 `tune`

用于在一个专用 worktree 和调优周期中连续建立或复用评测资产、建立基线并自动产生候选 Prompt。缺少有效评测资产时，`tune` 先执行内部初始化阶段；该阶段不是可独立调用或退出后再由另一次 `tune` 恢复的用户模式。

每个 `tune` 周期都必须在目标仓库的 primary workspace 中通过以下
fail-closed setup gate；不能从 linked worktree 或 detached `HEAD` 开始：

```text
resolve primary checkout identity
-> reject linked worktree or detached HEAD
-> derive .worktrees/stabilizing-prompts/<prompt-slug>-<cycle-id>/
-> initialize repository-local exclude
-> verify the final managed-worktree target with git check-ignore
-> create branch and worktree with git worktree add
-> persist WorktreeCycle
-> verify concrete reports/ and .runtime/ paths from the linked worktree
```

The repository-local exclude initialization is automatic, idempotent, byte-
preserving, and atomic. It resolves Git's repository-local exclude path and
adds only the missing anchored rules needed for:

```gitignore
/.worktrees/stabilizing-prompts/
/.prompt-evals/*/reports/
/.prompt-evals/*/.runtime/
```

It never edits, stages, or commits the project `.gitignore`, `.git/config`,
global excludes, or global Git configuration. Both ignore gates use Git's
actual `check-ignore` behavior: the pre-create gate checks the final derived
managed-worktree directory before creating its parent, branch, worktree, or
state, and the post-create gate checks the concrete prompt `reports/` and
`.runtime/` paths before assets, confirmation, or any model call.

The post-create gate is exposed as `manage_worktree.py verify-ignores --state
STATE_PATH`. A pre-create failure leaves no cycle resources; a post-create
failure retains the already-persisted WorktreeCycle, exact worktree, and
cycle branch for diagnosis. Every setup interruption is non-scoring and
retains the diagnostic cycle when one exists; it never falls back to tuning in
the original checkout. Delivery-time identity guards remain separate and run
after model phases, immediately before synchronization.

The final worktree path can only be under the target repository's
`.worktrees/stabilizing-prompts/`; callers cannot provide a replacement path.
The Skill never edits, stages, or commits the target repository `.gitignore`.

Any primary-workspace, detached-HEAD, path, ignore-gate, `git worktree add`, or
WorktreeCycle state-persistence failure stops this cycle with a setup error
before the first model call. Legacy or externally located cycle state is
rejected rather than migrated. Non-scored setup, protocol, and asset
interruptions retain their exact worktree, branch, and state for diagnosis and
manual handling; without finalization and verified-delivery state, they are not
eligible for the `manage_worktree.py cleanup` CLI. Only a scored result with an
affirmative combined delivery/cleanup confirmation and verified delivery can
enter the cleanup contract.

完整流程：

1. 验证 Python Git 工程中的单个 `.md` Prompt、相关文件 Git 状态并完成上述 worktree gate；
2. 追踪渲染、生产 Pydantic Schema、`ChatOpenAI` 结构化调用、下游业务和关键依赖；
3. 若缺少有效评测资产，生成业务契约、开发集、验证集、验收集和覆盖矩阵；
4. 验证固定 Conda `kds` 环境并生成或验证项目适配器；
5. 向用户展示契约、三套完整 Pydantic 预期、固定执行环境与命令、重复次数和门禁并等待确认；
6. 确认后执行连接、模型身份和适配器真实 smoke test，只使用开发案例，不接触验收结果；
7. 提交已确认且通过 smoke test 的评测资产，建立评测周期；
8. 对原 Prompt 运行开发集和验证集基线并保存摘要和失败聚类，验收集暂不运行；
9. 若开发集和验证集的所有计划调用均为 `pass`，形成 `no_change_needed`；不生成候选、不运行验收集，但仍进入统一的结果规范化、简报、精简历史、`prepared_commit` 和交付/清理确认；
10. 若用户掌握基线未复现的真实故障，停止调优，补充有证据的案例并重新执行确认和基线；
11. 读取 `optimization-history.yaml`，按错误类别和业务维度聚类失败；
12. 一次选择一个有证据的失败簇，从原 Prompt 或上一候选生成最小修改候选；
13. 将候选写入 `.runtime/`，不覆盖原 Prompt；
14. 重跑受影响的开发案例，通过后重跑完整开发集；
15. 只有完整开发集不退化时才运行验证集；
16. 根据验证结果选择满足门禁且至少一个核心指标严格改善的最终候选并冻结哈希；
17. 在同一且仅一次的最终验收活动中，分别对原 Prompt 和冻结候选运行验收集；
18. Acceptance 失败形成 `acceptance_failed`，不编辑候选、不重跑验收；Acceptance 通过形成 `acceptance_passed`，两个结果都进入同一 finalization 和 delivery profile 规则；
19. 将正式终态规范化为以下七种之一：`no_change_needed`、`no_strict_improvement`、`validation_failed`、`no_improvement_limit`、`round_limit`、`acceptance_failed`、`acceptance_passed`；setup/protocol/transport/model/confirmation 等中断不属于正式结果；
20. 使用已保存的评分、比较、覆盖和停止证据生成确定性 `evaluation-summaries/YYYY-MM-DD-HHMMSS-<result>.md` 简报，并在交付确认前追加一条 compact `optimization-history.yaml` 记录；两者与当前周期可交付评测资产一起提交为 `prepared_commit`；
21. 展示正式结果、简报路径和结果对应的计划交付集合，然后请求一次明确的 combined delivery/cleanup confirmation；拒绝、含糊或取消时不交付、不修改原工作区、不清理，并保留 prepared evidence、worktree、branch 和 state；
22. 确认后执行 `finalize_cycle.py approve --state STATE_PATH`：asset-only 结果令 `delivery_commit == prepared_commit` 且不创建空提交，只有 `acceptance_passed` 从 `prepared_commit` 创建包含冻结 Prompt 的子 `delivery_commit`；
23. `build-patch` 的有效结果 profile 必须从当前 finalization state 派生；兼容的 `--result success|failure` 参数不能覆盖 state。实时生成 allowlisted patch，apply、目标验证和 delivery-state 写入均成功后，才执行精确 verified cleanup。

默认最多进行 5 轮候选迭代。连续两轮没有指标改善、出现业务契约冲突、适配器失真、模型不可用或达到最大轮数时停止并报告，不能无限循环。

交付和清理是同一次最终用户确认动作的一部分。同步前后通过候选哈希、worktree
提交和补丁内容证明落盘内容就是已经终验的候选，不在同步后重新打开本周期调优。

### 12.3 交付安全边界

交付采用本地单用户、非对抗性威胁模型：保护误操作、并发编辑、过期周期状态、路径错误、意外变更文件、补丁无法应用以及应用/校验失败；不防御能够任意修改 Skill 代码、Git 仓库、周期状态、补丁、manifest 或哈希的本地攻击者，也不承诺消除所有理论 TOCTOU 间隔。实现不使用仓库锁、密码学信任或自定义完整 Git 补丁解析器。

交付时从可信的 `cycle_base_commit`、当前周期 `delivery_commit` 和已提交文件内容
fresh-generate 补丁。Git `--name-status -z`/`--name-only -z`（或等价原生命令）
报告的实际路径必须与由当前 finalization state 派生的结果白名单和生成补丁
section 的路径集合完全一致；额外 section/文件一律停止。持久化 patch、manifest
和 hash 仅作为可选传递产物，不是敌对篡改环境下的信任锚。

build/apply 前重新确认原工作区 `HEAD == cycle_base_commit`、固定 managed-root、
worktree identity、cleanliness、`HEAD == delivery_commit` 以及提交版本的
`prompt-contract.yaml` 仍指向周期开始时的 canonical Prompt path；只有
`acceptance_passed` 可以更新当前 Prompt hash 等非路径字段。随后执行
`git apply --check`，snapshot 精确目标，使用不暂存的 apply，验证实际结果路径和
destination hash。任何构建、应用或验证异常都 rollback 到精确 snapshot，不启动
cleanup。apply 和目标验证成功后，必须先原子写入 `STATE_PATH` 的 delivery/cleanup
状态；该写入失败仍 rollback 到 apply 前快照并保留周期。

成功同步保持原工作区文件 unstaged/uncommitted。只有 combined delivery/cleanup
confirmation 已确认、同步和目标验证成功、且 delivery state 已原子记录时，才进入
verified cleanup；此时按精确 state 删除本周期 worktree、branch 和 state，不影响已
交付评测目录或其他周期资源。

### 12.4 verified cleanup

清理只有在 combined delivery/cleanup confirmation 明确确认、canonical patch 已应用、
目标路径和内容验证成功，且 `STATE_PATH` 已原子记录
`delivery_applied`、`delivery_verified` 和 cleanup authorization 后才开始。任何
setup、protocol、transport、model、confirmation、synchronization 或 verification
失败都保留 worktree、branch 和 state，不启动 cleanup。

稳定入口是：

```text
manage_worktree.py cleanup --state STATE_PATH
```

该命令只从单一 current WorktreeCycle 派生精确删除目标，不接受调用者提供的
worktree、branch 或额外路径。首次启动时，在 worktree 外重新验证原工作区身份和
fixed Git cleanliness、精确 managed worktree/registration、cycle branch/ref、
`delivery_commit`、delivery evidence 和 committed contract path，然后执行：

```text
verify fixed Git cleanliness
-> git worktree remove <exact-cycle-worktree>
-> verify exact cycle directory and Git registration are absent
-> remove .worktrees/stabilizing-prompts/ only when empty
-> verify exact cycle branch is unused by every worktree
-> git branch -D <exact-cycle-branch>
-> delete exact STATE_PATH
```

不使用 `git worktree remove --force`、`git worktree prune`、reflog expiry、Git GC、
glob 或递归删除；`.worktrees/` 始终保留，非空的 managed parent 属于其他周期或
用户内容时也保留。每个成功步骤都在进入下一个 destructive step 前原子更新 state。
部分失败不回滚已验证交付，保留 branch/state 和诊断进度并停止后续步骤。重试是
phase-aware 的：重新验证已完成步骤的 postcondition 与当前精确 identity，只对仍
存在的资源运行相应检查；已经验证移除的 worktree 或 branch 不必再次满足首次启动
的存在性/cleanliness gate，也不重复删除。

### 12.2 `verify`

`verify` 是当前工作区中的只读流程，不修改 repository-local exclude。它在任何
输出或模型调用前，以 Git 的实际行为检查具体的 `reports/` 和 `.runtime/`
路径；缺少 ignore 覆盖时返回 `setup_error`，不创建输出。它也必须在打开任何
manifest/case 文件或构造 model client 前拒绝 `--dataset acceptance`，并且绝不
读取或运行 `acceptance-cases.yaml`。

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
- 若开发集和验证集的所有计划调用均通过，形成 `no_change_needed`，不为假设性问题修改 Prompt，也不运行验收集；该正式结果仍生成简报、追加精简历史、提交 `prepared_commit`，并按 assets profile 请求统一交付/清理确认。若用户掌握未被复现的真实故障，先补充并重新确认案例，再重新建立基线。

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

每个正式评分终态都必须从已保存的评分、比较、覆盖和停止证据生成一份确定性
Markdown 简报，写入当前 prompt ID 的
`evaluation-summaries/YYYY-MM-DD-HHMMSS-<result>.md`。简报只描述正式结论、
指标、失败摘要、停止原因和计划 delivery profile；不得包含 commit/file/manifest
hash、内部绝对 worktree 路径、patch 细节、Authorization、Token 或 raw response，
也不得声称已经确认、交付或清理。同名简报不能覆盖或随机改名。

`reports/` 中的原始响应和完整运行报告只保留在 worktree，不提交、不默认同步。
可交付的 `optimization-history.yaml` 按周期追加精简记录，并在简报之后、
delivery confirmation 之前与可交付评测资产一起提交为 `prepared_commit`。
每条 compact entry 严格只包含以下四个字段：
`finished_at_utc`、`result`、`stop_reason` 和按字典序排序的
`failure_categories`。它不保存中间候选全文、原始响应或 Token。新周期必须读取历史记录以避免重复无效策略，但当前已确认的契约和案例始终是业务真值来源。基础设施故障只进入运行报告，不作为 Prompt 优化经验。只有 `acceptance_passed` 的确认路径会在 `prepared_commit` 上创建包含冻结 Prompt 的 child `delivery_commit`；其他正式结果复用 `prepared_commit`，不创建空提交。

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
- 最终验收失败：形成正式结果 `acceptance_failed`，不交付候选 Prompt；它与其他
  asset-only 正式结果共享同一简报、prepared/delivery commit、确认、同步、回滚和
  verified cleanup 路径；
- 每个正式结果都必须在简报和 `prepared_commit` 之后获得一次 combined
  delivery/cleanup confirmation。拒绝、含糊或取消时不生成/应用补丁、不修改原工作区、
  不清理，并保留 prepared evidence、worktree、branch 和 state；
- 非评分 setup/protocol/transport/model-service/identity/asset/confirmation 中断不
  生成正式简报或 `prepared_commit`，并按阶段保留可恢复的 cycle/worktree/state；
- 无法从可信周期状态重新生成补丁、实际路径不等于选定白名单路径、存在额外 patch section/文件，或提交契约重定向 canonical Prompt path：停止，不采用持久化 patch、manifest 或 hash 替代 fresh generation；
- 补丁预检冲突、原工作区 `HEAD` 漂移、目标文件变化或新增路径冲突：停止，不自动合并或覆盖；
- `git apply --check`、补丁应用、实际路径验证或哈希校验异常：恢复同步前保存的精确目标状态，不启动 cleanup；
- apply/验证成功但 delivery 状态无法原子写入 `STATE_PATH`：仍恢复 apply 前的精确目标快照，不宣告交付成功，不启动 cleanup，并保留 cycle 供重试；
- 用户取消：保留原工作区和生产 Prompt，不进行最终同步；已进入 cleanup 的部分失败不回滚已验证交付，按 phase-aware 规则保留 state。

## Workflow and delivery CLI contract

The internal `bootstrap` work is not a separate command. It runs after
worktree creation and before baseline construction in one `tune` cycle, using
the same `WorktreeCycle` and immutable `cycle_base_commit`. The cycle is
created with:

```text
manage_worktree.py create --repo PATH --prompt-id ID --state PATH [--branch BRANCH]
```

After the user confirms the contract/cases/adapter and the fixed local-model
probe succeeds, the confirmed evaluation assets are committed in that
worktree. Candidate files stay in `.runtime/`; reports and raw responses stay
in ignored directories.

The setup and defensive runtime checks are exposed as:

```text
manage_worktree.py verify-ignores --state STATE_PATH
```

At the end of the cycle, each of the seven formal results requires the same
explicit combined delivery/cleanup confirmation. The `assets` profile
(`no_change_needed`, `no_strict_improvement`, `validation_failed`,
`no_improvement_limit`, `round_limit`, and `acceptance_failed`) synchronizes
only the current prompt ID's confirmed evaluation assets, summary, and compact
history. The `success` profile (`acceptance_passed`) may additionally include
the frozen production Prompt. Every result's summary and history are prepared
before this decision, so a refusal retains the prepared commit and exact cycle
for inspection or a later decision.

Fresh delivery artifacts are generated with:

```text
manage_worktree.py build-patch --state PATH --out PATCH --out-manifest PATCH_JSON --result success|failure
manage_worktree.py apply-patch --state PATH --patch PATCH --patch-manifest PATCH_JSON
```

The finalization commands are:

```text
finalize_cycle.py prepare --state STATE_PATH --result RESULT --finished-at UTC --evidence SUMMARY_JSON [--candidate FROZEN_CANDIDATE --candidate-hash SHA256]
finalize_cycle.py approve --state STATE_PATH
```

`prepare` normalizes the formal result, writes the deterministic
`evaluation-summaries/YYYY-MM-DD-HHMMSS-<result>.md`, appends one compact
history entry, and records the resulting `prepared_commit`. `approve` records
the combined confirmation and resolves `delivery_commit`: asset-only results
reuse `prepared_commit` without an empty child commit, while
`acceptance_passed` alone creates one child commit with the frozen candidate.

`build-patch` derives the effective delivery profile and Prompt allowlist from
the current finalization state and committed canonical path. Its compatibility
`--result success|failure` spelling is accepted during this version but cannot
override that state. It must compare Git's actual changed paths and patch paths
exactly with the allowlist; reports, runtime candidates, unrelated files,
deletions, extra sections, and non-pass Prompt changes stop delivery. It
regenerates from the trusted cycle base, resolved delivery commit, and
committed content at delivery time; persisted patch text, manifest fields, and
hashes are transport artifacts, not adversarial trust anchors.

Immediately before apply, re-check the original `HEAD == cycle_base_commit`,
the fixed managed-root and cycle worktree/branch identity, cleanliness,
`HEAD == delivery_commit`, and the committed contract's canonical Prompt path.
Run `git apply --check`, snapshot exact targets, apply unstaged, and verify
paths and destination hashes. If apply, verification, or the atomic delivery
state update fails, roll back the exact pre-apply snapshot, report rollback,
and retain the worktree, branch, and state; never start cleanup.

After successful apply, destination verification, and the atomic state update,
run the verified cleanup command:

```text
manage_worktree.py cleanup --state STATE_PATH
```

Cleanup has first-start gates for fixed cleanliness, exact managed worktree and
registration, cycle branch/ref, `delivery_commit`, and recorded delivery
evidence. It removes only the exact cycle worktree, verifies its directory and
registration are gone, removes the managed parent only when empty, verifies
the branch is unused, deletes the cycle-created branch, and finally deletes
the exact state file. Every destructive phase atomically checkpoints state.
Cleanup retries are phase-aware and revalidate saved postconditions; already
removed worktrees or branches are not deleted again and do not repeat the
first-start existence gates. The `.worktrees/` container and all other cycles
remain untouched.
