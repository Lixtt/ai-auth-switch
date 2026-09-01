from __future__ import annotations

import http.client
import io
import json
import os
import socket
import tempfile
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Thread

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool_responses import (
    PoolResponsesProxy,
    ResponsesProxyConfig,
    _http_server_for_host,
    _models_upstream_url,
    ensure_listener_token,
    request_route_key,
)
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.usage import (
    AccountUsage,
    UsageWindow,
    fetch_profile_usage,
)


class FakeResponse:
    status = 200

    def __init__(self, body: bytes):
        self.headers = {"Content-Type": "text/event-stream"}
        self.body = body
        self.sent = False
        self.closed = False

    def read(self, _size: int = -1) -> bytes:
        if self.sent:
            return b""
        self.sent = True
        return self.body

    def close(self) -> None:
        self.closed = True


def usage(remaining: float) -> AccountUsage:
    return AccountUsage(
        plan_type="pro",
        secondary=UsageWindow(
            used_percent=100 - remaining,
            window_seconds=604800,
            resets_at=2_000_000_000,
        ),
    )


class PoolResponsesTests(unittest.TestCase):
    def test_models_upstream_url_replaces_responses_and_merges_query(self) -> None:
        self.assertEqual(
            _models_upstream_url(
                "https://example.test/backend-api/codex/responses?tenant=one",
                "/v1/models?client_version=0.152.0",
            ),
            "https://example.test/backend-api/codex/models?tenant=one&client_version=0.152.0",
        )
        self.assertEqual(
            _models_upstream_url(
                "http://127.0.0.1:9000/v1/responses/",
                "/models",
            ),
            "http://127.0.0.1:9000/v1/models",
        )

    def test_request_route_key_prefers_stable_thread_metadata(self) -> None:
        body = json.dumps(
            {
                "metadata": {"thread_id": "thread-1"},
                "prompt_cache_key": "project-1",
                "previous_response_id": "response-1",
            }
        ).encode()
        first = request_route_key(body)
        self.assertEqual(first, request_route_key(body))
        self.assertNotIn("thread-1", first)
        self.assertNotEqual(first, request_route_key(body, "thread-2"))

    def test_listener_token_is_private_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token"
            first = ensure_listener_token(path)
            second = ensure_listener_token(path)
            self.assertEqual(first, second)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            target = Path(tmp) / "target"
            target.write_text("secret\n", encoding="utf-8")
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(AiAuthSwitchError, "symlinked"):
                ensure_listener_token(path)

    def test_forward_replaces_local_auth_and_load_balances_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name, email in (("a", "a@example.com"), ("b", "b@example.com")):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps(
                        {
                            "tokens": {
                                "access_token": f"token-{name}",
                                "account_id": f"account-{name}",
                            },
                            "email": email,
                        }
                    ),
                )
            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b'data: {"type":"response.completed"}\n\n')

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(20),
                },
            )
            result = proxy.forward(
                b'{"model":"gpt-5-codex","input":"hello"}',
                {
                    "Authorization": "Bearer local-token",
                    "X-AI-Auth-Switch-Token": "local-token",
                    "X-Test": "keep",
                },
            )
            self.assertEqual(result.status, 200)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer token-a")
            self.assertEqual(requests[0].get_header("Chatgpt-account-id"), "account-a")
            self.assertEqual(requests[0].get_header("X-test"), "keep")
            self.assertEqual(requests[0].get_header("X-ai-auth-switch-token"), None)

    def test_concurrent_streams_use_different_paid_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            entered = Barrier(2)
            release = Event()

            class BlockingResponse(FakeResponse):
                def read(self, _size: int = -1) -> bytes:
                    if not self.sent:
                        self.sent = True
                        release.wait(timeout=5)
                        return self.body
                    return b""

            def opener(request, **_kwargs):
                requests.append(request)
                entered.wait(timeout=5)
                return BlockingResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            results: list[bytes] = []

            def run() -> None:
                results.append(proxy.forward(b"{}", {}).body)

            threads = [Thread(target=run), Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=0.2)
            release.set()
            for thread in threads:
                thread.join(timeout=5)
            self.assertEqual(
                {request.get_header("Authorization") for request in requests},
                {"Bearer token-a", "Bearer token-b"},
            )
            self.assertEqual(results, [b"ok", b"ok"])

    def test_loopback_restriction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            with self.assertRaisesRegex(AiAuthSwitchError, "loopback"):
                PoolResponsesProxy(
                    store,
                    provider,
                    config=ResponsesProxyConfig(host="0.0.0.0"),
                )

    def test_ipv6_loopback_listener_uses_ipv6_address_family(self) -> None:
        if not socket.has_ipv6:
            self.skipTest("IPv6 is unavailable on this host")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )
            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(host="::1", port=0),
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            server = _http_server_for_host("::1", 0, proxy._handler_class())
            try:
                self.assertEqual(server.address_family, socket.AF_INET6)
            finally:
                server.server_close()

    def test_retry_moves_to_another_account_before_response_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            attempts = 0

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                requests.append(request)
                if attempts == 1:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        401,
                        "expired",
                        {"Content-Type": "application/json"},
                        None,
                    )
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward(b"{}", {}, route_key="turn-1")
            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-a", "Bearer token-b"],
            )

    def test_retry_moves_after_retryable_response_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            attempts = 0

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                requests.append(request)
                if attempts == 1:
                    response = FakeResponse(b"retry")
                    response.status = 503
                    return response
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward(b"{}", {})

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-a", "Bearer token-b"],
            )

    def test_retry_moves_after_network_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            attempts = 0

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                requests.append(request)
                if attempts == 1:
                    raise urllib.error.URLError("temporary outage")
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward(b"{}", {})

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-a", "Bearer token-b"],
            )

    def test_retry_recovers_from_incomplete_upstream_stream(self) -> None:
        """A truncated upstream body must not crash the proxy worker."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            attempts = 0

            class TruncatedResponse(FakeResponse):
                def read(self, _size: int = -1) -> bytes:
                    raise http.client.IncompleteRead(b"partial", 3)

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                requests.append(request)
                if attempts == 1:
                    response = TruncatedResponse(b"ignored")
                    # A retryable status is buffered before headers are sent
                    # to the caller, so a truncated body can safely fail over.
                    response.status = 503
                    return response
                return FakeResponse(b"complete")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward(b"{}", {})

            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b"complete")
            self.assertEqual(len(requests), 2)

    def test_incomplete_body_after_headers_is_returned_without_traceback(self) -> None:
        """Once headers are sent, a truncated stream is safely terminated."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )

            class TruncatedResponse(FakeResponse):
                def read(self, _size: int = -1) -> bytes:
                    if not self.sent:
                        self.sent = True
                        return b"partial"
                    raise http.client.IncompleteRead(b"partial", 2)

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=lambda _request, **_kwargs: TruncatedResponse(b"ignored"),
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            chunks: list[bytes] = []
            headers: list[tuple[int, dict[str, str]]] = []

            proxy.stream(
                b"{}",
                {},
                on_headers=lambda status, values: headers.append((status, values)),
                on_chunk=chunks.append,
            )

            self.assertEqual(headers[0][0], 200)
            self.assertEqual(b"".join(chunks), b"partial")

    def test_forward_models_retries_incomplete_catalog_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            attempts = 0

            class TruncatedResponse(FakeResponse):
                def read(self, _size: int = -1) -> bytes:
                    raise http.client.IncompleteRead(b"partial", 3)

            def opener(_request, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    return TruncatedResponse(b"ignored")
                response = FakeResponse(b'{"data":[]}')
                response.headers = {"Content-Type": "application/json"}
                return response

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward_models({})

            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b'{"data":[]}')
            self.assertEqual(attempts, 2)

    def test_unhealthy_sticky_route_migrates_before_upstream_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )

            requests: list[urllib.request.Request] = []
            usage_calls = 0

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b"ok")

            def usage_fetcher(_profiles, **_kwargs):
                nonlocal usage_calls
                usage_calls += 1
                return {"a": usage(90), "b": usage(80)}

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=usage_fetcher,
            )
            initial = proxy.coordinator.reserve(
                proxy.store.list_profiles(provider),
                {"a": usage(90), "b": usage(80)},
                route_key="turn-1",
                owner="pid:100",
                now=10,
            )
            proxy.coordinator.release(initial)
            proxy.coordinator.mark_failure("a", "401", "expired", now=11)
            result = proxy.forward(b"{}", {}, route_key="turn-1")

            self.assertEqual(result.status, 200)
            self.assertEqual(usage_calls, 1)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-b"],
            )
            self.assertEqual(
                proxy.coordinator.load().routes["turn-1"].profile,
                "b",
            )

    def test_healthy_sticky_route_stays_on_bound_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(20),
                    "b": usage(90),
                },
            )
            initial = proxy.coordinator.reserve(
                proxy.store.list_profiles(provider),
                {"a": usage(20), "b": usage(90)},
                route_key="turn-1",
                owner="pid:100",
                now=10,
            )
            self.assertEqual(initial.profile, "b")
            proxy.coordinator.release(initial)

            # Make the bound account look less attractive; stickiness must
            # still win while it remains healthy.
            proxy.usage_fetcher = lambda _profiles, **_kwargs: {
                "a": usage(90),
                "b": usage(1),
            }
            result = proxy.forward(b"{}", {}, route_key="turn-1")

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-b"],
            )

    def test_removed_sticky_profile_migrates_before_upstream_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {"b": usage(80)},
            )
            initial = proxy.coordinator.reserve(
                proxy.store.list_profiles(provider),
                {"a": usage(90), "b": usage(80)},
                route_key="turn-1",
                owner="pid:100",
                now=10,
            )
            self.assertEqual(initial.profile, "a")
            proxy.coordinator.release(initial)
            store.profile_path(provider, "a").unlink()

            result = proxy.forward(b"{}", {}, route_key="turn-1")

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-b"],
            )

    def test_concurrent_sticky_recovery_converges_on_one_migrated_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b", "c"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            entered = Barrier(2)
            release = Event()

            class BlockingResponse(FakeResponse):
                def read(self, _size: int = -1) -> bytes:
                    if not self.sent:
                        self.sent = True
                        entered.wait(timeout=5)
                        release.wait(timeout=5)
                        return self.body
                    return b""

            def opener(request, **_kwargs):
                requests.append(request)
                return BlockingResponse(b"ok")

            usage_map = {
                "a": usage(90),
                "b": usage(80),
                "c": usage(20),
            }
            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: usage_map,
            )
            initial = proxy.coordinator.reserve(
                proxy.store.list_profiles(provider),
                usage_map,
                route_key="turn-1",
                owner=f"pid:{os.getpid()}",
                now=10,
            )
            proxy.coordinator.release(initial)
            proxy.coordinator.mark_failure("a", "401", "expired", now=11)

            results: list[bytes] = []

            def run() -> None:
                results.append(proxy.forward(b"{}", {}, route_key="turn-1").body)

            threads = [Thread(target=run), Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=0.2)
            release.set()
            for thread in threads:
                thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(results, [b"ok", b"ok"])
            self.assertEqual(
                {request.get_header("Authorization") for request in requests},
                {"Bearer token-b"},
            )

    def test_expired_usage_snapshot_migrates_sticky_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            profiles = store.list_profiles(provider)
            cache_dir = store.base_dir / "cache" / "usage" / provider.id
            fetch_profile_usage(
                ((profile.name, profile.path) for profile in profiles),
                cache_dir=cache_dir,
                refresh=True,
                fetcher=lambda _path, **_kwargs: usage(90),
            )

            initial = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(90),
                    "b": usage(80),
                },
                opener=lambda _request, **_kwargs: FakeResponse(b"ok"),
            )
            selected = initial.coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                route_key="turn-1",
                owner="pid:100",
                now=10,
            )
            self.assertEqual(selected.profile, "a")
            initial.coordinator.release(selected)

            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b"ok")

            def expired_fetcher(path, **_kwargs):
                if path.stem == "a":
                    return AccountUsage(error="authentication expired")
                return usage(80)

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda profile_items, **kwargs: fetch_profile_usage(
                    profile_items,
                    cache_dir=cache_dir,
                    refresh=True,
                    fetcher=expired_fetcher,
                    **{
                        key: value
                        for key, value in kwargs.items()
                        if key in {"timeout", "workers"}
                    },
                ),
            )
            result = proxy.forward(b"{}", {}, route_key="turn-1")

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [item.get_header("Authorization") for item in requests],
                ["Bearer token-b"],
            )

    def test_auth_failure_for_rotated_old_token_does_not_strand_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"old-{name}"}}),
                )
            attempts = 0

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    # Simulate Codex rotating the profile while the old token
                    # request is still in flight.
                    store.profile_path(provider, "a").write_text(
                        json.dumps({"tokens": {"access_token": "new-a"}}),
                        encoding="utf-8",
                    )
                    raise urllib.error.HTTPError(
                        request.full_url,
                        401,
                        "expired old token",
                        {},
                        None,
                    )
                return FakeResponse(b"ok")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(90),
                    "b": usage(20),
                },
            )
            first = proxy.forward(b"{}", {})
            self.assertEqual(first.status, 401)
            second = proxy.forward(b"{}", {})
            self.assertEqual(second.status, 200)
            self.assertEqual(attempts, 2)

    def test_retryable_status_is_preserved_when_no_other_account_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )

            def opener(request, **_kwargs):
                raise urllib.error.HTTPError(
                    request.full_url,
                    429,
                    "limited",
                    {"Content-Type": "application/json"},
                    None,
                )

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            result = proxy.forward(b"{}", {})
            self.assertEqual(result.status, 429)

    def test_unhealthy_sticky_route_stays_503_without_any_healthy_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )
            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                return FakeResponse(b"unexpected")

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": AccountUsage(error="authentication expired"),
                },
            )
            initial = proxy.coordinator.reserve(
                proxy.store.list_profiles(provider),
                {"a": usage(90)},
                route_key="turn-1",
                owner="pid:100",
                now=10,
            )
            proxy.coordinator.release(initial)
            proxy.coordinator.mark_failure("a", "401", "expired", now=11)

            with self.assertRaisesRegex(AiAuthSwitchError, "no eligible paid account"):
                proxy.forward(b"{}", {}, route_key="turn-1")
            self.assertEqual(requests, [])

    def test_forbidden_response_enters_cooldown_not_auth_expired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )

            def opener(request, **_kwargs):
                raise urllib.error.HTTPError(
                    request.full_url,
                    403,
                    "forbidden",
                    {"Content-Type": "text/html"},
                    None,
                )

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=0),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            self.assertEqual(proxy.forward(b"{}", {}).status, 403)
            self.assertEqual(proxy.coordinator.load().health["a"].status, "cooldown")

    def test_http_handler_streams_sse_and_requires_local_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )

            class Upstream(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    if self.headers.get("Authorization") != "Bearer token-a":
                        self.send_error(401)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: one\n\n")
                    self.wfile.flush()

                def log_message(self, _format: str, *_args: object) -> None:
                    return

            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
            upstream_thread = Thread(target=upstream.serve_forever, daemon=True)
            upstream_thread.start()
            try:
                proxy = PoolResponsesProxy(
                    store,
                    provider,
                    config=ResponsesProxyConfig(
                        upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}/responses",
                        port=0,
                    ),
                    usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
                )
                local = ThreadingHTTPServer(("127.0.0.1", 0), proxy._handler_class())
                local_thread = Thread(target=local.serve_forever, daemon=True)
                local_thread.start()
                try:
                    url = f"http://127.0.0.1:{local.server_address[1]}/v1/responses"
                    request = urllib.request.Request(
                        url,
                        data=b"{}",
                        headers={
                            "Authorization": f"Bearer {proxy.listener_token}",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(response.read(), b"data: one\n\n")
                finally:
                    local.shutdown()
                    local.server_close()
            finally:
                upstream.shutdown()
                upstream.server_close()

    def test_forward_models_uses_sibling_endpoint_and_profile_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps(
                    {
                        "tokens": {
                            "access_token": "token-a",
                            "account_id": "account-a",
                        }
                    }
                ),
            )
            requests: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                requests.append(request)
                response = FakeResponse(b'{"models":[]}')
                response.headers = {
                    "Content-Type": "application/json",
                    "ETag": 'W/"catalog-a"',
                    "Content-Length": "13",
                    "Connection": "keep-alive",
                    "X-Catalog": "ok",
                }
                return response

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(
                    upstream_url="https://example.test/backend-api/codex/responses?tenant=one",
                    max_retries=0,
                ),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            result = proxy.forward_models(
                {
                    "Authorization": "Bearer local-token",
                    "X-AI-Auth-Switch-Token": proxy.listener_token,
                    "ChatGPT-Account-Id": "wrong-account",
                    "X-Test": "keep",
                },
                request_target="/v1/models?client_version=0.152.0",
            )

            self.assertEqual(result.status, 200)
            self.assertEqual(result.body, b'{"models":[]}')
            self.assertEqual(result.headers["ETag"], 'W/"catalog-a"')
            self.assertNotIn("Content-Length", result.headers)
            self.assertNotIn("Connection", result.headers)
            self.assertEqual(len(requests), 1)
            self.assertEqual(
                requests[0].full_url,
                "https://example.test/backend-api/codex/models?tenant=one&client_version=0.152.0",
            )
            self.assertEqual(requests[0].get_method(), "GET")
            self.assertEqual(requests[0].get_header("Authorization"), "Bearer token-a")
            self.assertEqual(
                requests[0].get_header("Chatgpt-account-id"), "account-a"
            )
            self.assertEqual(requests[0].get_header("X-test"), "keep")
            self.assertEqual(requests[0].get_header("Content-type"), None)

    def test_forward_models_retries_auth_failure_on_another_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                )
            requests: list[urllib.request.Request] = []
            attempts = 0

            def opener(request, **_kwargs):
                nonlocal attempts
                attempts += 1
                requests.append(request)
                if attempts == 1:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        401,
                        "expired",
                        {"Content-Type": "application/json"},
                        io.BytesIO(b'{"error":"expired"}'),
                    )
                return FakeResponse(b'{"models":[]}')

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(max_retries=1),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            result = proxy.forward_models({}, request_target="/v1/models")

            self.assertEqual(result.status, 200)
            self.assertEqual(
                [request.get_header("Authorization") for request in requests],
                ["Bearer token-a", "Bearer token-b"],
            )

    def test_http_handler_serves_models_alias_and_requires_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"tokens": {"access_token": "token-a"}}),
            )
            seen: list[urllib.request.Request] = []

            def opener(request, **_kwargs):
                seen.append(request)
                return FakeResponse(b'{"models":[]}')

            proxy = PoolResponsesProxy(
                store,
                provider,
                config=ResponsesProxyConfig(
                    upstream_url="http://example.test/backend-api/codex/responses",
                    max_retries=0,
                ),
                opener=opener,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            local = ThreadingHTTPServer(("127.0.0.1", 0), proxy._handler_class())
            thread = Thread(target=local.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{local.server_address[1]}"
                unauthorized = urllib.request.Request(f"{base}/v1/models")
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(unauthorized, timeout=5)
                self.assertEqual(error.exception.code, 401)

                request = urllib.request.Request(
                    f"{base}/models?client_version=0.152.0",
                    headers={"Authorization": f"Bearer {proxy.listener_token}"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'{"models":[]}')
                self.assertEqual(seen[0].full_url, "http://example.test/backend-api/codex/models?client_version=0.152.0")
            finally:
                local.shutdown()
                local.server_close()


if __name__ == "__main__":
    unittest.main()
