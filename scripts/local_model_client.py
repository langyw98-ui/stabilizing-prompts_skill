"""Construct and probe the fixed local model client.

The endpoint and generation settings in this module are part of the Skill's
request contract.  The only value read from disk is the local authorization
token, stored outside the tracked source tree in ``.local/model-credentials.json``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any

from langchain_openai import ChatOpenAI


BASE_URL = "http://192.168.168.230:8000/v1"
MODEL_NAME = "dbirks/Qwen3.8-27B-W4A16-AutoRound"
EXTRA_BODY: dict[str, bool] = {
    "enable_thinking": False,
    "enable_reasoning": False,
    "enable_search": False,
}
TIMEOUT = 30
MAX_RETRIES = 2

# These fields are intentionally absent from the ChatOpenAI constructor.  The
# list is safe to include in manifests because it contains no credential.
OMITTED_SAMPLING_PARAMETERS = (
    "top_p",
    "seed",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "logit_bias",
    "n",
    "max_completion_tokens",
    "reasoning_effort",
    "reasoning",
    "stop_sequences",
)

SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS_PATH = SKILL_ROOT / ".local" / "model-credentials.json"
REDACTED = "[REDACTED]"


class CredentialSetupError(RuntimeError):
    """A local credential is absent, unreadable, or does not match the schema."""


# A generic alias makes the non-scoring setup failure usable by callers that do
# not need to distinguish which fixed-client setup check failed.
SetupError = CredentialSetupError
ClientSetupError = CredentialSetupError


class ModelProbeError(CredentialSetupError):
    """The service did not report the one configured model identity."""


@dataclass(frozen=True, slots=True)
class ModelProbe:
    """The model identity returned by a successful service probe."""

    model: str

    @property
    def model_name(self) -> str:
        """Compatibility spelling for callers that use ChatOpenAI's field name."""

        return self.model

    @property
    def identity(self) -> str:
        return self.model

    @property
    def matched(self) -> bool:
        return self.model == MODEL_NAME


# Values are retained only in process memory so redaction can also handle an
# SDK error that includes the token without an Authorization/Bearer prefix.
_KNOWN_SECRETS: set[str] = set()


def _duplicate_rejecting_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate credential field")
        result[key] = value
    return result


def _authorization_token(credentials_path: Path | None = None) -> str:
    """Read and strictly validate the local-only credential file.

    Error messages deliberately do not include the path contents, parsed JSON,
    or exception text.  This keeps malformed files and SDK setup failures from
    becoming accidental secret-output channels.
    """

    path = Path(credentials_path) if credentials_path is not None else DEFAULT_CREDENTIALS_PATH
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CredentialSetupError("credential file is missing or unreadable") from None

    try:
        payload = json.loads(contents, object_pairs_hook=_duplicate_rejecting_object)
    except (json.JSONDecodeError, TypeError, ValueError, UnicodeError):
        raise CredentialSetupError("credential file is malformed") from None

    if not isinstance(payload, dict) or set(payload) != {"authorization_token"}:
        raise CredentialSetupError(
            "credential file must contain only authorization_token"
        )
    token = payload.get("authorization_token")
    if type(token) is not str or not token.strip():
        raise CredentialSetupError("credential authorization_token must be non-empty")

    _KNOWN_SECRETS.add(token)
    return token


_SENSITIVE_KEY = re.compile(
    r"(?:authorization[_-]?token|api[_-]?key|access[_-]?token|password|secret|token)",
    re.IGNORECASE,
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,;\"']+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)([^\s,;\"']+)")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|authorization[_-]?token|access[_-]?token|token|secret)"
    r"\s*[:=]\s*)([\"']?)([^\s,;\"']+)(\2?)"
)


def _redact_text(value: str) -> str:
    result = value
    # Replace known values first; the generic patterns below cover redaction
    # even when a test or SDK error is produced before client construction.
    for secret in sorted(_KNOWN_SECRETS, key=len, reverse=True):
        if secret:
            result = result.replace(secret, REDACTED)
    result = _AUTHORIZATION_VALUE.sub(rf"\1{REDACTED}", result)
    result = _BEARER_VALUE.sub(rf"\1{REDACTED}", result)
    result = _SENSITIVE_ASSIGNMENT.sub(rf"\1\2{REDACTED}\4", result)
    return result


def redact_secret(value: object) -> object:
    """Return a structurally similar value with credential-bearing data hidden."""

    if isinstance(value, BaseException):
        return _redact_text(str(value))
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            safe_key = _redact_text(key) if isinstance(key, str) else key
            if isinstance(key, str) and _SENSITIVE_KEY.search(key):
                result[safe_key] = REDACTED
            else:
                result[safe_key] = redact_secret(item)
        return result
    if isinstance(value, list):
        return [redact_secret(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secret(item) for item in value)
    if isinstance(value, set):
        return {redact_secret(item) for item in value}
    return value


def safe_error(error: object) -> str:
    """Format an exception or arbitrary error value without credential text."""

    try:
        return str(redact_secret(error))
    except Exception:
        return REDACTED


def safe_client_config() -> dict[str, object]:
    """Return the complete fixed request configuration without its API key."""

    return {
        "base_url": BASE_URL,
        "model": MODEL_NAME,
        "temperature": 0.0,
        "timeout": TIMEOUT,
        "max_retries": MAX_RETRIES,
        "extra_body": dict(EXTRA_BODY),
        "omitted_sampling_parameters": list(OMITTED_SAMPLING_PARAMETERS),
    }


def build_client(credentials_path: Path | None = None) -> ChatOpenAI:
    """Build a ChatOpenAI client with immutable Skill-owned settings."""

    return ChatOpenAI(
        api_key=_authorization_token(credentials_path),
        base_url=BASE_URL,
        model=MODEL_NAME,
        temperature=0.0,
        timeout=TIMEOUT,
        max_retries=MAX_RETRIES,
        extra_body=dict(EXTRA_BODY),
    )


def _response_identities(value: object) -> list[str]:
    """Extract model IDs from common OpenAI object and test-double shapes."""

    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        if "id" in value:
            return [value["id"]] if isinstance(value["id"], str) else []
        for key in ("model", "model_name"):
            if key in value:
                return [value[key]] if isinstance(value[key], str) else []
        if "data" in value:
            return _response_identities(value["data"])
        return []
    for key in ("id", "model", "model_name"):
        candidate = getattr(value, key, None)
        if isinstance(candidate, str):
            return [candidate]
    data = getattr(value, "data", None)
    if data is not None:
        return _response_identities(data)
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        identities: list[str] = []
        for item in value:
            identities.extend(_response_identities(item))
        return identities
    return []


def _require_expected_identity(identities: list[str]) -> str:
    if not identities:
        raise ModelProbeError("model probe returned an empty model identity")
    if len(identities) == 1:
        identity = identities[0]
        if not identity or identity != MODEL_NAME:
            raise ModelProbeError("model probe returned an unexpected model identity")
        return identity
    if MODEL_NAME in identities:
        return MODEL_NAME
    raise ModelProbeError("model probe returned an unexpected model identity")


def _probe_from_models(owner: object) -> str | None:
    models = getattr(owner, "models", None)
    if models is None:
        return None
    retrieve = getattr(models, "retrieve", None)
    if callable(retrieve):
        try:
            response = retrieve(MODEL_NAME)
        except Exception:
            raise ModelProbeError("model probe request failed") from None
        return _require_expected_identity(_response_identities(response))
    list_models = getattr(models, "list", None)
    if callable(list_models):
        try:
            response = list_models()
        except Exception:
            raise ModelProbeError("model probe request failed") from None
        return _require_expected_identity(_response_identities(response))
    return None


def probe_model(client: object) -> ModelProbe:
    """Probe the service and require the exact configured model identity."""

    owners: list[object] = []
    for candidate in (
        getattr(client, "root_client", None),
        client,
        getattr(client, "client", None),
    ):
        if candidate is not None and all(candidate is not previous for previous in owners):
            owners.append(candidate)

    for owner in owners:
        identity = _probe_from_models(owner)
        if identity is not None:
            return ModelProbe(identity)

    for name in ("get_model_identity", "model_identity"):
        probe = getattr(client, name, None)
        if callable(probe):
            try:
                identity = probe()
            except Exception:
                raise ModelProbeError("model probe request failed") from None
            return ModelProbe(_require_expected_identity(_response_identities(identity)))

    # This fallback supports lightweight ChatOpenAI-compatible test doubles
    # that expose only the identity returned by their probe operation.  A real
    # ChatOpenAI instance reaches the OpenAI ``models`` endpoint above.
    for name in ("model_name", "model"):
        identity = getattr(client, name, None)
        if isinstance(identity, str):
            return ModelProbe(_require_expected_identity([identity]))

    raise ModelProbeError("model probe returned no model identity")


__all__ = [
    "BASE_URL",
    "MODEL_NAME",
    "EXTRA_BODY",
    "TIMEOUT",
    "MAX_RETRIES",
    "OMITTED_SAMPLING_PARAMETERS",
    "SKILL_ROOT",
    "DEFAULT_CREDENTIALS_PATH",
    "CredentialSetupError",
    "ClientSetupError",
    "SetupError",
    "ModelProbeError",
    "ModelProbe",
    "build_client",
    "probe_model",
    "safe_client_config",
    "redact_secret",
    "safe_error",
]
