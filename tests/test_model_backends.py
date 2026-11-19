import asyncio
import json

import httpx
import pytest

from zorocart.model import ModelError
from zorocart.model_backends import CHECKPOINT, probe_model_backend, resolve_model_backend


def test_default_and_explicit_local_use_distinct_ports_and_pinned_model():
    brev = resolve_model_backend(environ={})
    local = resolve_model_backend("local", environ={})
    assert (brev.name, brev.base_url) == ("brev", "http://127.0.0.1:30001")
    assert (local.name, local.base_url) == ("local", "http://127.0.0.1:30002")
    assert brev.model == local.model == CHECKPOINT


def test_legacy_environment_remains_the_brev_default_but_does_not_leak_to_local():
    env = {
        "ZORO_URL": "https://brev.example/api/v1",
        "ZORO_MODEL": "team-zoro",
        "ZORO_API_KEY": "legacy-secret",
        "ZORO_BACKEND": "local",
        "ZORO_LOCAL_URL": "http://127.0.0.1:30003",
        "ZORO_LOCAL_MODEL": "local-zoro",
    }
    local = resolve_model_backend(environ=env)
    assert (local.name, local.model, local.api_key) == ("local", "local-zoro", None)
    brev = resolve_model_backend("brev", environ=env)
    assert (brev.base_url, brev.model, brev.api_key) == (env["ZORO_URL"], "team-zoro", "legacy-secret")
    assert "legacy-secret" not in repr(brev)
    assert "brev.example" not in repr(brev)
    assert brev.summary() == {"backend": "brev", "label": "Brev GPU", "model": "team-zoro"}


def test_named_endpoint_does_not_forward_old_secret_to_a_different_host():
    env = {
        "ZORO_URL": "https://old.example",
        "ZORO_MODEL": "old-model",
        "ZORO_API_KEY": "old-secret",
        "ZORO_BREV_URL": "https://new.example/v1/",
    }
    replacement = resolve_model_backend(environ=env)
    assert replacement.api_key is None
    assert replacement.model == CHECKPOINT
    updated = resolve_model_backend(environ={**env, "ZORO_BREV_API_KEY": "new-secret", "ZORO_BREV_MODEL": "new-model"})
    assert (updated.model, updated.api_key) == ("new-model", "new-secret")
    same_endpoint = resolve_model_backend(environ={**env, "ZORO_BREV_URL": "https://old.example/v1/"})
    assert same_endpoint.api_key == "old-secret"


@pytest.mark.parametrize("endpoint", ["file:///tmp/model", "http://user:secret@localhost", "https://x:99999", "https://x?k=secret"])
def test_bad_endpoint_errors_identify_only_the_environment_variable(endpoint):
    with pytest.raises(ValueError, match="ZORO_LOCAL_URL") as error:
        resolve_model_backend("local", environ={"ZORO_LOCAL_URL": endpoint})
    assert endpoint not in str(error.value)


def test_probe_verifies_both_model_endpoints_and_exposes_only_safe_summary():
    config = resolve_model_backend("local", environ={
        "ZORO_LOCAL_URL": "https://local.example/proxy/v1/",
        "ZORO_LOCAL_API_KEY": "local-secret",
    })
    seen = []

    def serve(request):
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer local-secret"
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": CHECKPOINT, "private": "do not expose"}]})
        assert json.loads(request.content)["model"] == CHECKPOINT
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"model": CHECKPOINT, "choices": [{"message": {"content": "READY"}}]})
        return httpx.Response(200, json={"model": CHECKPOINT, "answers": {
            "choice": {"type": "choice", "choice": "ready", "probabilities": {"ready": 1, "offline": 0}},
            "score": {"type": "score", "probabilities": {"0": 0.1, "1": 0.9}},
        }})

    evidence = asyncio.run(probe_model_backend(config, transport=httpx.MockTransport(serve)))
    assert seen == ["/proxy/v1/models", "/proxy/v1/chat/completions", "/proxy/v1/systemone"]
    assert evidence == {
        "backend": "local", "label": "로컬", "model": CHECKPOINT, "models": [{"id": CHECKPOINT}],
        "chat": "READY", "choice": "ready", "score": pytest.approx(0.9),
    }


def test_chat_only_server_cannot_pass_the_connection_probe():
    config = resolve_model_backend("local", environ={})

    def serve(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": CHECKPOINT}]})
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"model": CHECKPOINT, "choices": [{"message": {"content": "READY"}}]})
        return httpx.Response(404, text="private server message")

    with pytest.raises(ModelError, match="systemone.*404") as error:
        asyncio.run(probe_model_backend(config, transport=httpx.MockTransport(serve)))
    assert "private" not in str(error.value)
