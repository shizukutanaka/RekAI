"""Every client's declared type covers every field the API actually returns.

RekAI ships three first-party clients — the web app, the JS SDK and the Python
SDK — and each restates the API's response shape by hand. Nothing kept those
restatements honest, and all three had drifted:

- the web app carried none of `finish_reason`, `cache_similarity`, `redacted`,
  `semantic_cache_hits_total` or `parked_providers`, so a truncated answer, an
  answer to a *different* prompt, a redacted answer, an inflated cache hit rate
  and a cooling-down provider were each invisible to the person reading them;
- the JS SDK's `UsageSummary` omitted `retries_total`, `cooldowns_total` and
  `usage_by_client`, so a TypeScript caller got a compile error for fields
  `GET /v1/usage` definitely returns — a type that lies about the payload is
  worse than no type, because it makes correct code fail to build;
- both SDKs' chat result dropped `created`.

A dropped field fails silently: the JSON still parses, the value is just gone.
That is why this is a test and not a convention. It compares live responses from
the real app against each client's declared fields, so adding a field to a
response without teaching the clients about it breaks the build.

The clients are parsed textually — they are TypeScript and a dataclass, not
importable from here. A parse that finds nothing **skips** rather than passes,
so a refactor that defeats the regex shows up as a skip and never as a false
green.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rekai.config import Settings
from rekai.main import create_app

_REPO = Path(__file__).resolve().parents[3]
_WEB = _REPO / "apps" / "web" / "lib" / "api.ts"
_JS_SDK = _REPO / "packages" / "js-sdk" / "src" / "index.d.ts"
_PY_SDK = _REPO / "packages" / "python-sdk" / "src" / "rekai_client" / "client.py"


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(
        create_app(Settings(environment="test", default_provider="echo", rate_limit_enabled=False))
    )


@pytest.fixture(scope="module")
def chat(client: TestClient) -> dict:
    resp = client.post(
        "/v1/chat", json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    )
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture(scope="module")
def embeddings(client: TestClient) -> dict:
    resp = client.post("/v1/embeddings", json={"model": "echo", "input": ["a"]})
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture(scope="module")
def usage(client: TestClient) -> dict:
    return client.get("/v1/usage").json()


@pytest.fixture(scope="module")
def health(client: TestClient) -> dict:
    return client.get("/health").json()


def _ts_fields(path: Path, name: str) -> set[str]:
    """Field names declared on a TypeScript interface, or an empty set."""
    if not path.exists():
        return set()
    match = re.search(rf"interface {name}\s*\{{(.*?)\n\}}", path.read_text(), re.S)
    return set(re.findall(r"^\s+([a-z_]+)\??:", match.group(1), re.M)) if match else set()


def _dataclass_fields(path: Path, name: str) -> set[str]:
    """Field names declared on a Python dataclass, or an empty set."""
    if not path.exists():
        return set()
    match = re.search(
        rf"class {name}[^\n]*:\n(.*?)(?=\n@dataclass|\nclass |\Z)", path.read_text(), re.S
    )
    return set(re.findall(r"^\s{4}([a-z_]+):", match.group(1), re.M)) if match else set()


def _assert_covers(declared: set[str], response: dict, what: str) -> None:
    if not declared:
        pytest.skip(f"could not parse {what} — the declaration moved or was renamed")
    missing = sorted(set(response) - declared)
    assert not missing, (
        f"{what} does not declare {missing}, which the API returns. A client that "
        "drops a field drops it silently; declare it there, or stop returning it."
    )


# --- the web app --------------------------------------------------------------


def test_web_chat_response_is_complete(chat: dict) -> None:
    _assert_covers(_ts_fields(_WEB, "ChatResponse"), chat, "the web app's ChatResponse")


def test_web_embeddings_response_is_complete(embeddings: dict) -> None:
    _assert_covers(
        _ts_fields(_WEB, "EmbeddingsResponse"), embeddings, "the web app's EmbeddingsResponse"
    )


def test_web_usage_summary_is_complete(usage: dict) -> None:
    _assert_covers(_ts_fields(_WEB, "UsageSummary"), usage, "the web app's UsageSummary")


def test_web_health_response_is_complete(health: dict) -> None:
    _assert_covers(_ts_fields(_WEB, "HealthResponse"), health, "the web app's HealthResponse")


# --- the JS SDK ---------------------------------------------------------------


def test_js_sdk_chat_result_is_complete(chat: dict) -> None:
    _assert_covers(_ts_fields(_JS_SDK, "ChatResult"), chat, "the JS SDK's ChatResult")


def test_js_sdk_embeddings_result_is_complete(embeddings: dict) -> None:
    _assert_covers(
        _ts_fields(_JS_SDK, "EmbeddingsResult"), embeddings, "the JS SDK's EmbeddingsResult"
    )


def test_js_sdk_usage_summary_is_complete(usage: dict) -> None:
    _assert_covers(_ts_fields(_JS_SDK, "UsageSummary"), usage, "the JS SDK's UsageSummary")


# --- the Python SDK -----------------------------------------------------------
# `usage()` and `health()` return the raw dict there by design, so there is no
# declared type to drift; only the two dataclasses are checked.


def test_python_sdk_chat_result_is_complete(chat: dict) -> None:
    _assert_covers(_dataclass_fields(_PY_SDK, "ChatResult"), chat, "the Python SDK's ChatResult")


def test_python_sdk_embeddings_result_is_complete(embeddings: dict) -> None:
    _assert_covers(
        _dataclass_fields(_PY_SDK, "EmbeddingsResult"),
        embeddings,
        "the Python SDK's EmbeddingsResult",
    )
