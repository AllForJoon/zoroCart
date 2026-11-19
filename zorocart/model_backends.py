"""Configuration for the two Zoro-1 decide servers used by ZoroCart."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from typing import Literal, cast
from urllib.parse import urlsplit

import httpx

from .model import Event, ModelError, ZoroClient


ModelBackendName = Literal["brev", "local"]
MODEL_BACKENDS: tuple[ModelBackendName, ...] = ("brev", "local")
DEFAULT_MODEL_BACKEND: ModelBackendName = "brev"
CHECKPOINT = "bf1b76b9b4df03b05fcbb73444e1cd4db0f566da"
_LABELS = {"brev": "Brev GPU", "local": "로컬"}
_URLS = {"brev": "http://127.0.0.1:30001", "local": "http://127.0.0.1:30002"}


@dataclass(frozen=True)
class ModelBackend:
    name: ModelBackendName
    label: str
    base_url: str = field(repr=False)
    model: str
    api_key: str | None = field(default=None, repr=False)

    def summary(self) -> dict[str, str]:
        """Return the fields suitable for a browser response or run summary."""
        return {"backend": self.name, "label": self.label, "model": self.model}

    def client(
        self,
        *,
        timeout: float = 30.0,
        event: Event | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> ZoroClient:
        return ZoroClient(self.base_url, model=self.model, api_key=self.api_key,
                          timeout=timeout, event=event, transport=transport)


def resolve_model_backend(
    name: str | None = None, *, environ: Mapping[str, str] | None = None,
) -> ModelBackend:
    """Resolve one named configuration without exposing the selected endpoint or key."""
    env = os.environ if environ is None else environ
    selected = name if name is not None else env.get("ZORO_BACKEND", DEFAULT_MODEL_BACKEND)
    if selected not in MODEL_BACKENDS:
        raise ValueError("Zoro-1 실행 환경은 brev 또는 local이어야 합니다.")
    backend = cast(ModelBackendName, selected)
    prefix = f"ZORO_{backend.upper()}"
    url_key = f"{prefix}_URL"
    legacy_url = env.get("ZORO_URL") if backend == "brev" else None
    base_url = env.get(url_key, legacy_url if legacy_url is not None else _URLS[backend]).strip()
    source = "ZORO_URL" if url_key not in env and legacy_url is not None else url_key
    _validate_url(base_url, source)

    old_endpoint = legacy_url.strip() if legacy_url is not None else _URLS[backend]
    legacy_applies = backend == "brev" and _api_root(base_url) == _api_root(old_endpoint)
    legacy_model = env.get("ZORO_MODEL", CHECKPOINT) if legacy_applies else CHECKPOINT
    model_key = f"{prefix}_MODEL"
    model = env.get(model_key, legacy_model).strip()
    if not model:
        source = "ZORO_MODEL" if model_key not in env and legacy_applies and "ZORO_MODEL" in env else model_key
        raise ValueError(f"{source}에 Zoro-1 모델 이름을 설정해 주세요.")

    legacy_token = env.get("ZORO_API_KEY") if legacy_applies else None
    api_key = env.get(f"{prefix}_API_KEY", legacy_token)
    return ModelBackend(backend, _LABELS[backend], base_url, model, (api_key or "").strip() or None)


async def probe_model_backend(
    config: ModelBackend,
    *,
    timeout: float = 12.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """Test generation and typed decisions instead of inferring readiness from the port."""
    try:
        async with asyncio.timeout(timeout), config.client(timeout=timeout, transport=transport) as client:
            evidence = await client.probe()
    except TimeoutError:
        raise ModelError("Zoro-1 생성 및 판단 연결 점검 시간이 초과되었습니다.") from None
    models = evidence["models"]
    return {
        **config.summary(),
        "models": [
            {"id": entry["id"]}
            for entry in models if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ] if isinstance(models, list) else [],
        "chat": evidence["chat"],
        "choice": evidence["choice"],
        "score": evidence["score"],
    }


def _validate_url(url: str, variable: str) -> None:
    message = f"{variable}에 인증 정보나 쿼리가 없는 HTTP(S) 주소를 설정해 주세요."
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError:
        raise ValueError(message) from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.hostname
        or endpoint.username is not None
        or port == 0
        or any(char in url for char in "?#\\")
        or any(char.isspace() or ord(char) < 32 for char in url)
    ):
        raise ValueError(message)


def _api_root(url: str) -> str:
    trimmed = url.rstrip("/")
    return trimmed if trimmed.endswith("/v1") else f"{trimmed}/v1"
