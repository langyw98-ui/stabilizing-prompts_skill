from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_prompt_eval import UsageError
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
    assert transport.call_count == 10
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


def test_verify_rejects_acceptance_before_transport_or_file_read(
    target_repo: Path,
) -> None:
    transport = CountingTransport()
    acceptance = next(target_repo.glob(".prompt-evals/*/acceptance-cases.yaml"))
    acceptance.write_text("this must not be read\n", encoding="utf-8")
    before = acceptance.read_bytes()

    with pytest.raises(UsageError, match="acceptance"):
        run_verify(target_repo, dataset="acceptance", transport=transport)

    assert transport.call_count == 0
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


def test_verify_fake_transport_is_not_reachable_from_eval_config(target_repo: Path) -> None:
    config = next(target_repo.glob(".prompt-evals/*/eval-config.yaml"))
    config.write_text("repeats: 5\ntransport: fake\n", encoding="utf-8")
    transport = CountingTransport()

    result = run_verify(target_repo, dataset="dev", transport=transport)

    assert result.status == "complete"
    assert transport.call_count == 10
