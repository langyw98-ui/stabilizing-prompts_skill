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
