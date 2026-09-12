from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import local_model_client as module


def _credentials(tmp_path: Path, token: str = "unit-test-secret") -> Path:
    path = tmp_path / ".local" / "model-credentials.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"authorization_token": token}), encoding="utf-8"
    )
    return path


def test_fixed_client_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_chat_openai(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(module, "ChatOpenAI", fake_chat_openai)
    module.build_client(_credentials(tmp_path))

    assert captured["api_key"] == "unit-test-secret"
    assert captured["base_url"] == "http://192.168.8.17:8000/v1"
    assert captured["model"] == "dbirks/Qwen3.8-27B-W4A16-AutoRound"
    assert captured["temperature"] == 0.0
    assert captured["timeout"] == 30
    assert captured["max_retries"] == 2
    assert captured["extra_body"] == {
        "enable_thinking": False,
        "enable_reasoning": False,
        "enable_search": False,
    }
    assert "top_p" not in captured and "seed" not in captured


def test_safe_config_and_exception_text_never_contain_token() -> None:
    sentinel = "unit-test-secret"

    assert sentinel not in json.dumps(module.safe_client_config())
    assert sentinel not in module.safe_error(
        RuntimeError(f"Authorization: Bearer {sentinel}")
    )


@pytest.mark.parametrize(
    "contents",
    [
        "not-json",
        "[]",
        '{"authorization_token": ""}',
        '{"authorization_token": "   "}',
        '{"authorization_token": 123}',
        '{"authorization_token": "ok", "other": "nope"}',
    ],
)
def test_invalid_credentials_are_setup_errors(
    tmp_path: Path, contents: str
) -> None:
    path = tmp_path / "model-credentials.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(module.CredentialSetupError, match="credential"):
        module.build_client(path)


def test_missing_or_unreadable_credentials_are_setup_errors(tmp_path: Path) -> None:
    with pytest.raises(module.CredentialSetupError, match="credential"):
        module.build_client(tmp_path / "missing.json")


@pytest.mark.parametrize("identity", [None, "", "other-model"])
def test_probe_requires_exact_non_empty_model_identity(identity: object) -> None:
    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(
                retrieve=lambda _model: SimpleNamespace(id=identity)
            )
        )
    )

    with pytest.raises(module.ModelProbeError, match="model"):
        module.probe_model(client)


def test_probe_returns_matching_model_identity() -> None:
    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(
                retrieve=lambda _model: SimpleNamespace(id=module.MODEL_NAME)
            )
        )
    )

    probe = module.probe_model(client)

    assert probe.model == module.MODEL_NAME
    assert probe.matched is True


def test_probe_falls_back_to_list_after_retrieve_failure() -> None:
    calls: list[str] = []

    def retrieve(_model: str) -> object:
        calls.append("retrieve")
        raise RuntimeError("retrieve unavailable")

    def list_models() -> list[SimpleNamespace]:
        calls.append("list")
        return [SimpleNamespace(id=module.MODEL_NAME)]

    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(retrieve=retrieve, list=list_models)
        )
    )

    probe = module.probe_model(client)

    assert calls == ["retrieve", "list"]
    assert probe.model == module.MODEL_NAME
    assert probe.matched is True


def test_probe_retrieve_and_list_fail_with_sanitized_error() -> None:
    sentinel = "retrieve-list-secret"

    def retrieve(_model: str) -> object:
        raise RuntimeError(f"Authorization: Bearer {sentinel}")

    def list_models() -> object:
        raise RuntimeError(f"Authorization: Bearer {sentinel}")

    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(retrieve=retrieve, list=list_models)
        )
    )

    with pytest.raises(module.ModelProbeError) as exc_info:
        module.probe_model(client)

    assert str(exc_info.value) == "model probe request failed"
    assert sentinel not in str(exc_info.value)


def test_probe_rejects_retrieve_identity_without_list_fallback() -> None:
    sentinel = "unexpected-retrieve-identity"
    calls: list[str] = []

    def retrieve(_model: str) -> SimpleNamespace:
        calls.append("retrieve")
        return SimpleNamespace(id=sentinel)

    def list_models() -> list[SimpleNamespace]:
        calls.append("list")
        return [SimpleNamespace(id=module.MODEL_NAME)]

    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(retrieve=retrieve, list=list_models)
        )
    )

    with pytest.raises(module.ModelProbeError) as exc_info:
        module.probe_model(client)

    assert type(exc_info.value) is module.ModelProbeError
    assert calls == ["retrieve"]
    assert str(exc_info.value) == "model probe returned an unexpected model identity"
    assert sentinel not in str(exc_info.value)


def test_probe_rejects_unexpected_list_identity_after_retrieve_failure() -> None:
    sentinel = "unexpected-list-identity"
    calls: list[str] = []

    def retrieve(_model: str) -> object:
        calls.append("retrieve")
        raise RuntimeError("retrieve unavailable")

    def list_models() -> list[SimpleNamespace]:
        calls.append("list")
        return [SimpleNamespace(id=sentinel)]

    client = SimpleNamespace(
        root_client=SimpleNamespace(
            models=SimpleNamespace(retrieve=retrieve, list=list_models)
        )
    )

    with pytest.raises(module.ModelProbeError) as exc_info:
        module.probe_model(client)

    assert type(exc_info.value) is module.ModelProbeError
    assert calls == ["retrieve", "list"]
    assert str(exc_info.value) == "model probe returned an unexpected model identity"
    assert sentinel not in str(exc_info.value)


def test_redact_secret_handles_nested_values_and_headers() -> None:
    value = {
        "authorization_token": "unit-test-secret",
        "message": "Authorization: Bearer unit-test-secret",
        "nested": ["api_key=unit-test-secret"],
    }

    redacted = module.redact_secret(value)

    assert "unit-test-secret" not in json.dumps(redacted)
    assert redacted["authorization_token"] == "[REDACTED]"
