# Adapter Contract

This reference preserves section 9 of the approved design.

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

通用 runner 负责调用槽位、固定 `ChatOpenAI` 调用、原始 `AIMessage` 保存、错误分类和完整 Pydantic 对象比较。适配器 smoke test 必须证明原 Prompt 能通过该边界完成一次真实渲染、function calling 和生产 Schema 实例化。

如果工程调用链无法注入候选 Prompt 或固定客户端，Skill 可以在评测目录生成最薄的兼容层，但必须继续直接导入生产渲染器和 Schema。无法等价复现时应停止并说明差异，不能给出通过结论。

## Adapter state and CLI boundary

The adapter is created or checked during `contract/cases/adapter` in the same
cycle/worktree as `bootstrap` and `tune`. It consumes the frozen contract,
canonical Prompt path, production renderer/call assembly, Schema reference,
and a selected `EvalCase`; it produces `{messages, schema}` and a redacted
smoke result. It must not read candidates as production assets, parse a model
response, or choose a model.

The first real call occurs only after the contract confirmation gate. The
post-confirmation smoke path uses `build_client()`/`probe_model()` from
`scripts/local_model_client.py` and the runner's production adapter boundary.
It uses one development case; it never loads acceptance cases. Missing
credentials, a fixed-model identity mismatch, renderer failure, or a
structured-response protocol failure stops the cycle and is not converted into
a Prompt score.

For ordinary evaluation the adapter is consumed through:

```text
run_prompt_eval.py --eval-root PATH --prompt PATH --dataset dev|validation|acceptance|external --repeats N --manifest PATH [--mode tune|verify]
```

The runner defaults to `--mode tune`; verification passes `--mode verify` and
cannot select the acceptance dataset. This boundary is enforced before the
adapter is imported or a model client is constructed.

The selected `PATH` may be a runtime candidate, but the renderer, message
assembly, Schema, fixed request configuration, and call-slot identity must be
identical to the baseline. Candidates remain under `.runtime/` until an
acceptance-passing candidate receives the separate delivery confirmation.
