from __future__ import annotations

from collections import Counter
import json
from pathlib import Path

import pytest

from scripts import run_prompt_eval as runner_module
from tests.integration_support import (
    CountingTransport,
    build_target_repo,
    run_verify,
)


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    return build_target_repo(tmp_path / "target-repo", complete_assets=True)


def test_verify_runs_selected_development_dataset_through_real_cli_chain(
    target_repo: Path,
) -> None:
    before = {
        path: path.read_bytes()
        for path in target_repo.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".runtime" not in path.parts
        and "__pycache__" not in path.parts
    }
    transport = CountingTransport()

    result = run_verify(target_repo, dataset="dev", transport=transport)

    assert result.status == "complete"
    assert result.dataset == "dev"
    assert result.mode == "verify"
    assert transport.call_count == 30 * 5
    after = {
        path: path.read_bytes()
        for path in target_repo.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".runtime" not in path.parts
        and "__pycache__" not in path.parts
    }
    assert after == before


@pytest.mark.parametrize("dataset", ["validation", "external"])
def test_verify_accepts_only_non_acceptance_datasets(
    target_repo: Path,
    dataset: str,
) -> None:
    transport = CountingTransport()

    result = run_verify(target_repo, dataset=dataset, transport=transport)

    assert result.status == "complete"
    assert result.mode == "verify"
    assert result.dataset == dataset
    assert transport.call_count > 0


def test_verify_rejects_acceptance_before_dataset_adapter_client_or_model(
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    acceptance = next(target_repo.glob(".prompt-evals/*/acceptance-cases.yaml"))
    acceptance.write_text("this must not be read\n", encoding="utf-8")
    before = acceptance.read_bytes()

    calls: Counter[str] = Counter()

    def fail_if_called(name: str):
        def _fail(*args: object, **kwargs: object) -> object:
            calls[name] += 1
            raise AssertionError(f"acceptance guard called {name}")

        return _fail

    monkeypatch.setattr(runner_module, "_load_cli_cases", fail_if_called("dataset"))
    monkeypatch.setattr(runner_module, "load_adapter", fail_if_called("adapter"))
    monkeypatch.setattr(runner_module, "build_client", fail_if_called("client"))
    monkeypatch.setattr(runner_module, "execute_run", fail_if_called("model"))

    # Call the production CLI entry point with an absent eval root.  The
    # acceptance guard must return before a dataset read, adapter import,
    # client construction, or model invocation.
    code = runner_module.main(
        [
            "--mode",
            "verify",
            "--eval-root",
            str(tmp_path / "does-not-exist"),
            "--prompt",
            str(target_repo / "prompts" / "classify.md"),
            "--dataset",
            "acceptance",
            "--repeats",
            "10",
            "--manifest",
            str(tmp_path / "acceptance.json"),
        ]
    )

    assert code == 2
    assert calls == Counter()
    assert acceptance.read_bytes() == before


def test_verify_does_not_create_candidate_or_modify_canonical_assets(target_repo: Path) -> None:
    tracked_before = {
        path: path.read_bytes()
        for path in target_repo.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".runtime" not in path.parts
        and "__pycache__" not in path.parts
    }

    result = run_verify(target_repo, dataset="dev", transport=CountingTransport())

    assert result.status == "complete"
    assert not list(target_repo.glob(".prompt-evals/*/.runtime/candidate*.md"))
    tracked_after = {
        path: path.read_bytes()
        for path in target_repo.rglob("*")
        if path.is_file()
        and ".git" not in path.parts
        and ".runtime" not in path.parts
        and "__pycache__" not in path.parts
    }
    assert tracked_after == tracked_before


def test_project_transport_setting_is_ignored_by_production_client_path(
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = next(target_repo.glob(".prompt-evals/*/eval-config.yaml"))
    config.write_text("repeats: 5\ntransport: fake\n", encoding="utf-8")
    transport = CountingTransport()

    from scripts import local_model_client

    credentials = tmp_path / "credentials.json"
    credentials.write_text(
        json.dumps({"authorization_token": "test-only-config-token"}),
        encoding="utf-8",
    )
    constructor_calls: list[dict[str, object]] = []

    def fake_chat_openai(**kwargs: object) -> CountingTransport:
        constructor_calls.append(kwargs)
        return transport

    # Leave runner_module.build_client untouched: this exercises the fixed
    # production-client path.  Only the ChatOpenAI constructor is test-injected
    # so no network call or real credential is used.
    monkeypatch.setattr(local_model_client, "ChatOpenAI", fake_chat_openai)
    manifest_path = config.parent / ".runtime" / "verify-config.json"
    code = runner_module.main(
        [
            "--mode",
            "verify",
            "--eval-root",
            str(config.parent),
            "--prompt",
            str(target_repo / "prompts" / "classify.md"),
            "--dataset",
            "dev",
            "--repeats",
            "5",
            "--manifest",
            str(manifest_path),
            "--credentials",
            str(credentials),
        ]
    )

    assert code == 0
    assert len(constructor_calls) == 1
    assert transport.call_count == 30 * 5
    assert "transport" not in constructor_calls[0]
    assert transport.transport_identity == "test-only-counting-transport"
    assert transport.structured_output_kwargs
    assert all(
        kwargs == {"method": "function_calling", "include_raw": True}
        for kwargs in transport.structured_output_kwargs
    )
