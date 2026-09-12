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
    assert "does not auto-delete the worktree" in skill
