"""Trusted-proxy client-IP resolution (REKAI_TRUSTED_PROXIES).

Behind a reverse proxy every request's TCP peer is the proxy itself, so the
rate limiter, budgets and audit logs would key on the proxy's address — one
shared bucket for the whole deployment. The resolver honors X-Forwarded-For
only when the direct peer is a configured trusted proxy; anything else would
let a caller that can reach the port forge its client identity.
"""

from __future__ import annotations

import ipaddress

from starlette.requests import Request
from starlette.testclient import TestClient

from rekai.config import Settings
from rekai.main import _resolve_client_ip, _trusted_proxy_nets, create_app

_NETS = (ipaddress.ip_network("10.0.0.0/8"),)


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "method": "GET", "client": (peer, 12345), "headers": headers})


class TestResolveClientIp:
    def test_peer_is_used_when_no_proxy_is_trusted(self) -> None:
        assert _resolve_client_ip(_request("1.2.3.4", "9.9.9.9"), (), False) == "1.2.3.4"

    def test_xff_is_ignored_from_an_untrusted_peer(self) -> None:
        # A caller that can reach the port directly must not be able to forge
        # its identity — rate-limit buckets would be trivially evadable.
        assert _resolve_client_ip(_request("8.8.8.8", "1.2.3.4"), _NETS, False) == "8.8.8.8"

    def test_rightmost_xff_wins_from_a_trusted_peer(self) -> None:
        # The proxy appended the client it saw; rightmost XFF is that client.
        req = _request("10.0.0.9", "1.2.3.4, 5.6.7.8")
        assert _resolve_client_ip(req, _NETS, False) == "5.6.7.8"

    def test_trusted_hops_are_skipped_walking_left(self) -> None:
        # Chain: client -> trusted 10.0.0.2 -> edge. XFF = [client, 10.0.0.2];
        # peer = 10.0.0.9 — skip the trusted hop, take the real client.
        req = _request("10.0.0.9", "1.2.3.4, 10.0.0.2")
        assert _resolve_client_ip(req, _NETS, False) == "1.2.3.4"

    def test_all_trusted_chain_uses_the_leftmost_claim(self) -> None:
        # Every hop is a trusted proxy (CDN + LB): the leftmost entry is the
        # origin the outermost proxy claimed — same rule as nginx real_ip.
        req = _request("10.0.0.9", "10.0.0.1, 10.0.0.2")
        assert _resolve_client_ip(req, _NETS, False) == "10.0.0.1"

    def test_no_xff_header_falls_back_to_the_peer(self) -> None:
        assert _resolve_client_ip(_request("10.0.0.9"), _NETS, False) == "10.0.0.9"

    def test_non_ip_hop_counts_as_untrusted(self) -> None:
        # XFF often carries literal "unknown"; it is not a trusted proxy, so it
        # is returned as-is (it becomes just another bounded bucket key).
        req = _request("10.0.0.9", "unknown, 10.0.0.2")
        assert _resolve_client_ip(req, _NETS, False) == "unknown"

    def test_star_uses_the_rightmost_entry_not_smuggled_left(self) -> None:
        # Under "*" everything looks trusted, so the chain walk can't run —
        # the rightmost entry is what the edge proxy appended (unforgeable),
        # while anything the client smuggled arrives to its left.
        req = _request("8.8.8.8", "smuggled, 1.2.3.4")
        assert _resolve_client_ip(req, (), True) == "1.2.3.4"

    def test_star_without_xff_falls_back_to_peer(self) -> None:
        assert _resolve_client_ip(_request("8.8.8.8"), (), True) == "8.8.8.8"


class TestTrustedProxyNets:
    def test_star_trusts_any_peer(self) -> None:
        nets, trust_any = _trusted_proxy_nets(Settings(environment="test", trusted_proxies="*"))
        assert trust_any and nets == ()
        assert _resolve_client_ip(_request("8.8.8.8", "1.2.3.4"), nets, trust_any) == "1.2.3.4"

    def test_bare_ip_means_a_single_host(self) -> None:
        nets, trust_any = _trusted_proxy_nets(
            Settings(environment="test", trusted_proxies="10.1.2.3")
        )
        assert not trust_any
        assert _resolve_client_ip(_request("10.1.2.3", "1.2.3.4"), nets, trust_any) == "1.2.3.4"
        assert _resolve_client_ip(_request("10.1.2.4", "1.2.3.4"), nets, trust_any) == "10.1.2.4"

    def test_invalid_entries_are_skipped(self) -> None:
        nets, _ = _trusted_proxy_nets(
            Settings(environment="test", trusted_proxies="not-an-ip, 10.0.0.0/8")
        )
        assert nets == (ipaddress.ip_network("10.0.0.0/8"),)


def _open_app(trusted_proxies: str, peer: str = "10.0.0.9", **overrides) -> TestClient:
    settings = Settings(
        environment="test",
        default_provider="echo",
        rate_limit_enabled=True,
        rate_limit_requests=2,
        rate_limit_window_seconds=60,
        trusted_proxies=trusted_proxies,
        **overrides,
    )
    return TestClient(create_app(settings), client=(peer, 50000))


class TestWireRateLimitBehindProxy:
    """Two clients arriving through one proxy must get independent buckets."""

    def test_separate_xff_clients_get_separate_buckets(self) -> None:
        client = _open_app("10.0.0.0/8")
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        # capacity 2: the third request from this client trips the limit.
        client.post("/v1/chat", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
        client.post("/v1/chat", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
        resp = client.post("/v1/chat", json=body, headers={"X-Forwarded-For": "1.1.1.1"})
        assert resp.status_code == 429
        # A different real client behind the SAME proxy keeps a fresh bucket —
        # the whole point of resolving XFF is that they don't share one.
        resp2 = client.post("/v1/chat", json=body, headers={"X-Forwarded-For": "2.2.2.2"})
        assert resp2.status_code == 200

    def test_xff_from_an_untrusted_peer_does_not_create_buckets(self) -> None:
        # No proxies trusted: rotating XFF must not mint new buckets (the peer
        # is the client; forged headers would otherwise evade the limit).
        client = _open_app("")
        body = {"model": "echo", "messages": [{"role": "user", "content": "hi"}]}
        for i, xff in enumerate(("1.1.1.1", "2.2.2.2", "3.3.3.3")):
            resp = client.post("/v1/chat", json=body, headers={"X-Forwarded-For": xff})
            assert resp.status_code == (429 if i == 2 else 200)
