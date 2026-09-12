"""Small production renderer and structured-output boundary for integration tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict


class Decision(BaseModel):
    """The production structured output consumed by downstream code."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "reject"]
    reason: str


def render_prompt(prompt_path: Path, case: object) -> str:
    """Render the production prompt and stable case metadata into a message."""

    prompt = Path(prompt_path).read_text(encoding="utf-8")
    case_id = getattr(case, "id", "unknown")
    case_input = getattr(case, "input", {})
    prompt_hash = hashlib.sha256(Path(prompt_path).read_bytes()).hexdigest()
    return (
        "renderer=production-renderer\n"
        f"case_id={case_id}\n"
        f"prompt_hash={prompt_hash}\n"
        f"input={json.dumps(case_input, ensure_ascii=False, sort_keys=True)}\n"
        f"prompt={prompt}"
    )


def assemble_call(prompt_path: Path, case: object) -> dict[str, object]:
    """Return the exact production messages and schema used by the adapter."""

    return {
        "messages": [HumanMessage(content=render_prompt(prompt_path, case))],
        "schema": Decision,
    }
