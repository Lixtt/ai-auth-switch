from __future__ import annotations

import json
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
    ensure_listener_token,
    request_route_key,
)
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.usage import AccountUsage, UsageWindow


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


if __name__ == "__main__":
    unittest.main()
