import logging

import pytest
from fastapi.testclient import TestClient

import rekai.main as main_module
from rekai.auth import client_id
from rekai.config import Settings
from rekai.main import create_app
from rekai.providers import register_provider
from rekai.providers.base import Provider, ProviderError
from rekai.rate_limit import RateLimiter, RedisRateLimiter, build_rate_limiter
from rekai.security import KeyCipher, generate_key, mask_key


def test_upstream_429_retry_after_propagated_to_client() -> None:
    class RateLimitedProvider(Provider):
        name = "upstream_rl"
        requires_key = False

        async def chat(self, request, api_key):
            raise ProviderError("upstream rate limit", status_code=429, retry_after=15)

    register_provider(RateLimitedProvider())
    # retry off (surface immediately) and rate limiting off (isolate the path).
    settings = Settings(
        environment="test",
        default_provider="echo",
        retry_max_attempts=1,
        rate_limit_enabled=False,
    )
    client = TestClient(create_app(settings))
    body = {
        "model": "x",
        "provider": "upstream_rl",
        "messages": [{"role": "user", "content": "hi"}],
    }
    resp = client.post("/v1/chat", json=body)
    assert resp.status_code == 429
    # The upstream's Retry-After is passed through so the client can back off.
    assert resp.headers["Retry-After"] == "15"


def test_cipher_roundtrip() -> None:
    cipher = KeyCipher(generate_key())
    token = cipher.encrypt("sk-secret")
    assert token != "sk-secret"
    assert cipher.decrypt(token) == "sk-secret"


def test_cipher_wrong_key_fails() -> None:
    token = KeyCipher(generate_key()).encrypt("x")
    with pytest.raises(ValueError):
        KeyCipher(generate_key()).decrypt(token)


@pytest.mark.parametrize(
    "key,expected",
    [(None, "<none>"), ("short", "*****"), ("sk-1234567890", "sk-1…7890")],
)
def test_mask_key(key, expected) -> None:
    assert mask_key(key) == expected


def test_rate_limiter_blocks_after_capacity() -> None:
    limiter = RateLimiter(capacity=2, window=60)
    assert limiter.allow("client") is True
    assert limiter.allow("client") is True
    assert limiter.allow("client") is False
    # A different client has its own bucket.
    assert limiter.allow("other") is True


def test_rate_limiter_prunes_idle_buckets() -> None:
    import time

    # Active clients (below capacity) are retained even when the cap is hit, so
    # legitimate traffic keeps its budget.
    limiter = RateLimiter(capacity=5, window=60, max_buckets=10)
    for i in range(11):
        limiter.allow(f"active-{i}")  # each consumes a token -> not full -> kept
    assert len(limiter._buckets) == 11

    # _prune drops fully-refilled (idle) buckets but keeps active ones.
    now = time.time()
    limiter._buckets["idle"] = (5.0, now)  # tokens == capacity -> idle
    limiter._buckets["busy"] = (0.5, now)  # partially spent -> active
    limiter._prune(now)
    assert "idle" not in limiter._buckets
    assert "busy" in limiter._buckets


def test_rate_limiter_retry_after() -> None:
    limiter = RateLimiter(capacity=2, window=60)
    # Tokens available -> no wait.
    assert limiter.retry_after("client") == 0
    limiter.allow("client")
    limiter.allow("client")
    assert limiter.allow("client") is False
    # One token refills every window/capacity = 30s; peek doesn't consume.
    wait = limiter.retry_after("client")
    assert 1 <= wait <= 30
    assert limiter.retry_after("client") == wait


def test_rate_limiter_remaining() -> None:
    limiter = RateLimiter(capacity=3, window=60)
    assert limiter.remaining("client") == 3  # full, non-consuming
    assert limiter.remaining("client") == 3  # still full (peek)
    limiter.allow("client")
    assert limiter.remaining("client") == 2


class _FakeRedis:
    """Just enough of redis.asyncio for the rate limiter: INCR/EXPIRE/GET."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    async def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("redis down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.ttls[key] = seconds

    async def get(self, key: str) -> str | None:
        if self.fail:
            raise ConnectionError("redis down")
        return str(self.counts[key]) if key in self.counts else None


async def test_redis_rate_limiter_blocks_after_capacity() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is False
    # A different client has its own counter.
    assert await limiter.allow("other") is True
    # The window counter got a TTL so it can't accumulate forever.
    assert all(ttl > 0 for ttl in fake.ttls.values())


async def test_redis_rate_limiter_counts_are_shared_via_the_store() -> None:
    # Two limiter instances (≈ two workers) over one Redis see one budget.
    fake = _FakeRedis()
    worker_a = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    worker_b = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await worker_a.allow("client") is True
    assert await worker_b.allow("client") is True
    assert await worker_a.allow("client") is False
    assert await worker_b.allow("client") is False


async def test_redis_rate_limiter_remaining_and_retry_after() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=2, window=60, client=fake)
    assert await limiter.remaining("client") == 2
    assert await limiter.retry_after("client") == 0
    await limiter.allow("client")
    await limiter.allow("client")
    assert await limiter.remaining("client") == 0
    # Blocked until the fixed window rolls over.
    assert 1 <= await limiter.retry_after("client") <= 60


async def test_redis_rate_limiter_fails_open_when_redis_is_down() -> None:
    fake = _FakeRedis()
    limiter = RedisRateLimiter("redis://unused", capacity=1, window=60, client=fake)
    fake.fail = True
    # Redis outage degrades to "no rate limiting", not "no service".
    assert await limiter.allow("client") is True
    assert await limiter.allow("client") is True
    assert await limiter.remaining("client") == 1
    assert await limiter.retry_after("client") == 0


def test_build_rate_limiter_is_local_without_redis() -> None:
    settings = Settings(environment="test", rate_limit_enabled=True)
    assert build_rate_limiter(settings, 60, 60).label == "local"


def test_endpoint_sets_ratelimit_headers() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=5,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat", json=body)
    assert resp.status_code == 200
    assert resp.headers["X-RateLimit-Limit"] == "5"
    # One token consumed by this request -> 4 remain.
    assert resp.headers["X-RateLimit-Remaining"] == "4"


def test_options_preflight_not_rate_limited() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    # Consume the only token, then a CORS preflight must still pass (not 429).
    assert client.post("/v1/chat", json={"model": "echo", "messages": []}).status_code != 429
    pre = client.options(
        "/v1/chat",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert pre.status_code != 429


def test_endpoint_429_sets_retry_after() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=1,
        rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    assert client.post("/v1/chat", json=body).status_code == 200
    blocked = client.post("/v1/chat", json=body, headers={"Origin": "http://localhost:3000"})
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    assert blocked.json()["error"] == "rate_limited"
    # CORS is outermost, so even this short-circuit 429 is browser-readable.
    assert blocked.headers["access-control-allow-origin"] == "*"
    # Retry-After is exposed to browser JS (not CORS-safelisted by default).
    assert "retry-after" in blocked.headers["access-control-expose-headers"].lower()


def test_client_budget_exceeded_returns_402() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-a",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
    )
    client = TestClient(create_app(settings))
    try:
        # Simulate prior spend past the cap (echo itself is free, so seed it directly).
        main_module.metrics.record_client_usage(client_id("sk-budget-a"), tokens=100, cost_usd=1.0)
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post(
            "/v1/chat",
            json=body,
            headers={
                "Authorization": "Bearer sk-budget-a",
                "Origin": "http://localhost:3000",
            },
        )
        assert resp.status_code == 402
        assert resp.json()["error"] == "budget_exceeded"
        assert resp.headers["X-Budget-Remaining"] == "0"
        # CORS is outermost, so this short-circuit 402 is browser-readable too.
        assert "x-budget-remaining" in resp.headers["access-control-expose-headers"].lower()
    finally:
        main_module.metrics.seed({})


def test_client_budget_allows_requests_under_the_cap() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-b",
        rate_limit_enabled=False,
        client_budget_usd=10.0,
    )
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-b"})
        assert resp.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_is_per_client() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-budget-over,sk-budget-under",
        rate_limit_enabled=False,
        client_budget_usd=0.5,
    )
    client = TestClient(create_app(settings))
    try:
        main_module.metrics.record_client_usage(
            client_id("sk-budget-over"), tokens=100, cost_usd=1.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        over = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-over"}
        )
        under = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-budget-under"}
        )
        assert over.status_code == 402
        assert under.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_unset_disables_check() -> None:
    settings = Settings(environment="test", default_provider="echo", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    try:
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat", json=body).status_code == 200
    finally:
        main_module.metrics.seed({})


def test_client_budget_overrides_parses_key_amount_pairs() -> None:
    settings = Settings(client_budgets_usd="sk-a:5.00, sk-b:20.5")
    assert settings.client_budget_overrides == {"sk-a": 5.00, "sk-b": 20.5}


def test_client_budget_overrides_skips_malformed_entries() -> None:
    settings = Settings(client_budgets_usd="sk-a:oops, no-colon-here, :5.00, sk-b:1.0")
    assert settings.client_budget_overrides == {"sk-b": 1.0}


def test_client_budget_override_beats_global_default() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        api_keys="sk-override-tight,sk-override-loose",
        rate_limit_enabled=False,
        client_budget_usd=100.0,  # generous global default
        client_budgets_usd="sk-override-tight:0.5",  # this one key gets a tight cap
    )
    client = TestClient(create_app(settings))
    try:
        main_module.metrics.record_client_usage(
            client_id("sk-override-tight"), tokens=100, cost_usd=1.0
        )
        main_module.metrics.record_client_usage(
            client_id("sk-override-loose"), tokens=100, cost_usd=1.0
        )
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        tight = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-override-tight"}
        )
        loose = client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-override-loose"}
        )
        # Same $1.0 spend: the overridden key is over its $0.5 cap, the other
        # is still well under the $100 global default.
        assert tight.status_code == 402
        assert loose.status_code == 200
    finally:
        main_module.metrics.seed({})


def test_metrics_open_by_default_even_with_gateway_auth() -> None:
    settings = Settings(environment="test", api_keys="sk-metrics", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    assert client.get("/metrics").status_code == 200


def test_metrics_require_auth_rejects_missing_key() -> None:
    settings = Settings(
        environment="test",
        api_keys="sk-metrics",
        rate_limit_enabled=False,
        metrics_require_auth=True,
    )
    client = TestClient(create_app(settings))
    resp = client.get("/metrics")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_metrics_require_auth_allows_valid_key() -> None:
    settings = Settings(
        environment="test",
        api_keys="sk-metrics",
        rate_limit_enabled=False,
        metrics_require_auth=True,
    )
    client = TestClient(create_app(settings))
    resp = client.get("/metrics", headers={"Authorization": "Bearer sk-metrics"})
    assert resp.status_code == 200
    assert "rekai_requests_total" in resp.text


def test_metrics_require_auth_is_noop_without_configured_keys() -> None:
    # Nothing to check a Bearer token against, so /metrics stays open — same
    # fallback behaviour as /v1/* with no api_keys configured.
    settings = Settings(environment="test", rate_limit_enabled=False, metrics_require_auth=True)
    client = TestClient(create_app(settings))
    assert client.get("/metrics").status_code == 200


def test_admin_routes_absent_without_admin_key() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False)
    client = TestClient(create_app(settings))
    # Not registered at all — a plain 404, not a 401 (no admin surface to probe).
    assert client.get("/admin/keys").status_code == 404


def test_admin_rejects_missing_or_wrong_key() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    assert client.get("/admin/keys").status_code == 401
    resp = client.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_admin_add_disabled_without_dynamic_keys_enabled() -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    resp = client.post(
        "/admin/keys",
        json={"key": "sk-new"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "dynamic_keys_disabled"


def test_admin_list_keys_masks_static_and_dynamic() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        api_keys="sk-rekai-abc123",
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-dyn-longenough"}, headers=headers)
    body = client.get("/admin/keys", headers=headers).json()
    assert body["static"] == ["sk-r…c123"]
    assert body["dynamic"] == ["sk-d…ough"]
    # The raw keys are never returned anywhere.
    assert "sk-rekai-abc123" not in str(body)
    assert "sk-dyn-longenough" not in str(body)


def test_admin_add_key_grants_gateway_access() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        # No static api_keys: gateway auth is driven entirely by admin-added keys.
    )
    client = TestClient(create_app(settings))
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}

    # Before the key is added, gateway auth is on (dynamic_keys_enabled) and
    # nothing is allowed yet.
    assert client.post("/v1/chat", json=body).status_code == 401

    add = client.post(
        "/admin/keys",
        json={"key": "sk-runtime-key"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    assert add.status_code == 201
    assert add.json() == {"status": "added", "key": "sk-r…-key"}

    resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"})
    assert resp.status_code == 200


def test_admin_revoke_key_removes_gateway_access() -> None:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    admin_headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-runtime-key"}, headers=admin_headers)
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"}
        ).status_code
        == 200
    )

    revoke = client.delete("/admin/keys/sk-runtime-key", headers=admin_headers)
    assert revoke.status_code == 200
    assert revoke.json() == {"status": "revoked", "key": "sk-r…-key"}

    assert (
        client.post(
            "/v1/chat", json=body, headers={"Authorization": "Bearer sk-runtime-key"}
        ).status_code
        == 401
    )


def test_admin_revoke_unknown_key_returns_404() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    resp = client.delete(
        "/admin/keys/sk-never-added", headers={"Authorization": "Bearer sk-admin-1"}
    )
    assert resp.status_code == 404


def test_dynamic_keys_encrypted_at_rest_still_grant_access() -> None:
    from rekai.security import generate_key

    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
        dynamic_keys_encryption_key=generate_key(),
    )
    client = TestClient(create_app(settings))
    client.post(
        "/admin/keys",
        json={"key": "sk-encrypted-demo"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
    resp = client.post("/v1/chat", json=body, headers={"Authorization": "Bearer sk-encrypted-demo"})
    assert resp.status_code == 200


@pytest.fixture
def admin_audit_log(caplog):
    """Attach caplog's handler directly to the ``rekai.admin`` logger.

    create_app() -> configure_logging() clears the *root* logger's handlers on
    every call (by design, so repeated app creation in a long-lived process
    doesn't stack handlers) — which also strips pytest's caplog handler if it
    was attached there. Attaching directly to the named logger sidesteps that.
    """
    logger = logging.getLogger("rekai.admin")
    logger.addHandler(caplog.handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)


def test_admin_audit_log_records_auth_failure(admin_audit_log) -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    client.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "auth_failed"
    assert records[0].path == "/admin/keys"
    # The wrong key itself is never logged, only the fact that auth failed.
    assert "wrong" not in admin_audit_log.text


def test_admin_audit_log_records_add_with_masked_key(admin_audit_log) -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    client.post(
        "/admin/keys",
        json={"key": "sk-audit-secret-value"},
        headers={"Authorization": "Bearer sk-admin-1"},
    )
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "add_key"
    assert records[0].key == "sk-a…alue"
    # The raw key is never written to the audit log.
    assert "sk-audit-secret-value" not in admin_audit_log.text


def test_admin_audit_log_records_revoke_and_not_found(admin_audit_log) -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        dynamic_keys_enabled=True,
    )
    client = TestClient(create_app(settings))
    admin_headers = {"Authorization": "Bearer sk-admin-1"}
    client.post("/admin/keys", json={"key": "sk-to-revoke"}, headers=admin_headers)

    client.delete("/admin/keys/sk-to-revoke", headers=admin_headers)
    client.delete("/admin/keys/sk-never-existed", headers=admin_headers)

    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    actions = [r.admin_action for r in records]
    assert "revoke_key" in actions
    assert "revoke_key_not_found" in actions


def test_admin_audit_log_records_list(admin_audit_log) -> None:
    settings = Settings(environment="test", rate_limit_enabled=False, admin_key="sk-admin-1")
    client = TestClient(create_app(settings))
    client.get("/admin/keys", headers={"Authorization": "Bearer sk-admin-1"})
    records = [r for r in admin_audit_log.records if r.name == "rekai.admin"]
    assert len(records) == 1
    assert records[0].admin_action == "list_keys"


def test_admin_rate_limit_blocks_after_capacity() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_requests=2,
        admin_rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    assert client.get("/admin/keys", headers=headers).status_code == 200
    assert client.get("/admin/keys", headers=headers).status_code == 200
    blocked = client.get("/admin/keys", headers=headers)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1


def test_admin_rate_limit_counts_failed_auth_attempts() -> None:
    # Unlike the tenant gateway-auth gate (auth checked before rate limiting,
    # so a guesser can't burn a real tenant's budget), the admin gate counts
    # every attempt — right or wrong key — since the threat here is
    # brute-forcing the one shared secret, not fairness between tenants.
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_requests=2,
        admin_rate_limit_window_seconds=60,
    )
    client = TestClient(create_app(settings))
    wrong = {"Authorization": "Bearer wrong-guess"}
    assert client.get("/admin/keys", headers=wrong).status_code == 401
    assert client.get("/admin/keys", headers=wrong).status_code == 401
    # The 3rd attempt is rate limited even with the *correct* key now, because
    # the two wrong guesses already consumed the shared IP budget.
    resp = client.get("/admin/keys", headers={"Authorization": "Bearer sk-admin-1"})
    assert resp.status_code == 429


def test_admin_rate_limit_can_be_disabled() -> None:
    settings = Settings(
        environment="test",
        rate_limit_enabled=False,
        admin_key="sk-admin-1",
        admin_rate_limit_enabled=False,
        admin_rate_limit_requests=1,
    )
    client = TestClient(create_app(settings))
    headers = {"Authorization": "Bearer sk-admin-1"}
    for _ in range(5):
        assert client.get("/admin/keys", headers=headers).status_code == 200
