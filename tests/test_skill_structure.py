from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_required_skill_files_exist():
    expected = {
        "SKILL.md",
        "agents/openai.yaml",
        "references/business-contract.md",
        "references/case-schema.md",
        "references/adapter-contract.md",
        "references/evaluation-method.md",
        "references/worktree-lifecycle.md",
        "tests/test_skill_instructions.py",
    }
    assert {
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in ROOT.rglob("*")
        if path.is_file()
    } >= expected


def test_skill_frontmatter_and_modes():
    text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: stabilizing-prompts\n")
    assert "## tune" in text
    assert "## verify" in text
    assert "acceptance-cases.yaml" in text


def test_references_are_routed_from_the_workflow():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for name in (
        "business-contract.md",
        "case-schema.md",
        "adapter-contract.md",
        "evaluation-method.md",
        "worktree-lifecycle.md",
    ):
        assert name in skill


def test_workflow_documents_only_fixed_runtime_inputs():
    skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert ".local/model-credentials.json" in skill
    assert "temperature=0.0" in skill
    assert "repository-local exclude" in skill
    assert "evaluation-summaries" in skill
    assert "verify-ignores --state STATE_PATH" in skill
    assert "cleanup --state STATE_PATH" in skill
    assert "verified cleanup" in skill
    reference = (ROOT / "references" / "worktree-lifecycle.md").read_text(
        encoding="utf-8"
    )
    history = reference.split("`reports/` 中的原始响应", 1)[1].split(
        "## 15. 错误处理", 1
    )[0]
    assert "每条 compact entry 严格只包含以下四个字段" in history
    for field in (
        "`finished_at_utc`",
        "`result`",
        "`stop_reason`",
        "`failure_categories`",
    ):
        assert field in history
    for obsolete in (
        "周期、基线 Prompt、数据集、配置和 manifest 哈希",
        "失败簇、案例 ID、错误分类和字段差异",
        "每轮修改意图、指标变化、修复与回归案例和淘汰原因",
        "最终停止原因和尚未解决的问题",
        "经用户确认、应在下一周期转入开发集的真实故障",
    ):
        assert obsolete not in history
