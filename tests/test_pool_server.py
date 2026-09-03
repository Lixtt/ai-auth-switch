from __future__ import annotations

import io
import json
import os
import select
import sys
import tempfile
import unittest
from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool_server import PoolAppServer, PoolServerConfig
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.usage import AccountUsage, UsageWindow


class FakeBackend:
    def __init__(self, store, provider, profile, **_kwargs):
        self.profile = profile
        self.messages = []
        self.running = False
        self.stdout = None
        self.initial_result = {"backend": profile}

    def start(self):
        self.running = True
        return self.initial_result

    def send(self, message):
        self.messages.append(message)

    def read_message(self, **_kwargs):
        return None

    def stop(self):
        self.running = False


FAKE_APP_SERVER = (
    "import json, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    if message.get('method') == 'initialized':\n"
    "        continue\n"
    "    if message.get('method') == 'initialize':\n"
    "        result = {'server': 'fake', 'capabilities': {}}\n"
    "    elif message.get('method') == 'thread/start':\n"
    "        result = {'thread': {'id': 'thread-e2e'}}\n"
    "    elif message.get('id') is not None:\n"
    "        result = {'ok': True}\n"
    "    else:\n"
    "        continue\n"
    "    print(json.dumps({'id': message.get('id'), 'result': result}), flush=True)\n"
)


def usage(remaining: float) -> AccountUsage:
    return AccountUsage(
        plan_type="pro",
        secondary=UsageWindow(
            used_percent=100 - remaining,
            window_seconds=604800,
            resets_at=2_000_000_000,
        ),
    )


class PoolServerTests(unittest.TestCase):
    def test_stdio_protocol_round_trip_with_isolated_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(provider, "a", '{"email":"a@example.com"}')
            client_read, client_write = os.pipe()
            server_read, server_write = os.pipe()
            client_input = os.fdopen(client_read, "r", encoding="utf-8")
            client_output = os.fdopen(server_write, "w", encoding="utf-8")
            server = PoolAppServer(
                store,
                provider,
                command=(sys.executable, "-c", FAKE_APP_SERVER),
                input_stream=client_input,
                output_stream=client_output,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            thread = __import__("threading").Thread(target=server.run, daemon=True)
            thread.start()

            def request(message: dict) -> dict:
                os.write(client_write, (json.dumps(message) + "\n").encode())
                ready, _, _ = select.select([server_read], [], [], 5)
                self.assertTrue(ready)
                return json.loads(os.read(server_read, 65536).decode().splitlines()[0])

            try:
                initialized = request({"method": "initialize", "id": 1})
                self.assertEqual(initialized["id"], 1)
                started = request({"method": "thread/start", "id": 2, "params": {}})
                self.assertEqual(started["id"], 2)
                self.assertEqual(started["result"]["thread"]["id"], "thread-e2e")
            finally:
                os.close(client_write)
                thread.join(timeout=5)
                client_input.close()
                client_output.close()
                os.close(server_read)

    def test_initialization_thread_start_and_turn_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"email": f"{name}@example.com"}),
                )
            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                command=("fake-codex",),
                config=PoolServerConfig(),
                output_stream=output,
                backend_factory=FakeBackend,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(20),
                },
            )
            server._handle_client_message(
                {"method": "initialize", "id": 1, "params": {}}
            )
            initialize_response = json.loads(output.getvalue().splitlines()[0])
            self.assertEqual(initialize_response["id"], 1)
            backend = server.backends[server.routes.control_backend]
            server._handle_client_message(
                {"method": "thread/start", "id": 2, "params": {}}
            )
            self.assertEqual(backend.messages[-1]["method"], "thread/start")
            server._handle_backend_message(
                backend.profile,
                {"id": 2, "result": {"thread": {"id": "thr-1"}}},
            )
            server._handle_client_message(
                {"method": "turn/start", "id": 3, "params": {"threadId": "thr-1"}}
            )
            self.assertEqual(backend.messages[-1]["method"], "turn/start")

    def test_initialization_recovers_stale_control_route(self) -> None:
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
            usages = {"a": usage(90), "b": usage(80)}
            seed = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                usage_fetcher=lambda _profiles, **_kwargs: usages,
            )
            initial = seed.coordinator.reserve(
                profiles,
                usages,
                route_key="__control__",
                owner=f"pid:{os.getpid()}",
                now=10,
            )
            self.assertEqual(initial.profile, "a")
            seed.coordinator.release(initial)
            seed.coordinator.mark_failure("a", "401", "expired", now=11)

            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                output_stream=output,
                backend_factory=FakeBackend,
                usage_fetcher=lambda _profiles, **_kwargs: usages,
            )
            server._handle_client_message(
                {"method": "initialize", "id": 1, "params": {}}
            )

            response = json.loads(output.getvalue().splitlines()[0])
            self.assertEqual(response["id"], 1)
            self.assertEqual(server.routes.control_backend, "b")
            self.assertIn("b", server.backends)

    def test_global_request_migrates_unhealthy_control_backend(self) -> None:
        """Account/config/model requests must not stay pinned to an expired control account."""
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
            usages = {"a": usage(90), "b": usage(80)}
            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=output,
                usage_fetcher=lambda _profiles, **_kwargs: usages,
            )
            server._handle_client_message({"method": "initialize", "id": 1})
            self.assertEqual(server.routes.control_backend, "a")
            server.coordinator.mark_failure("a", "401", "expired")

            server._handle_client_message({"method": "account/read", "id": 2})

            self.assertEqual(server.routes.control_backend, "b")
            self.assertEqual(server.backends["b"].messages[-1]["method"], "account/read")
            self.assertEqual(server.backends["a"].messages, [])

    def test_client_id_with_pool_prefix_is_not_mistaken_for_server_response(self) -> None:
        """JSON-RPC permits arbitrary string IDs, including the pool prefix."""
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
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=io.StringIO(),
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            server._handle_client_message({"method": "initialize", "id": 1})

            server._handle_client_message(
                {"method": "config/read", "id": "pool:client-request", "params": {}}
            )

            backend = server.backends["a"]
            self.assertEqual(backend.messages[-1]["id"], "pool:client-request")

    def test_replacing_dead_backend_retires_old_instance(self) -> None:
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
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=io.StringIO(),
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            first = server._ensure_backend("a")
            first.running = False
            second = server._ensure_backend("a")

            self.assertIsNot(first, second)
            self.assertTrue(second.running)
            self.assertEqual(server.backends["a"], second)

    def test_backend_send_failure_cleans_pending_reservation(self) -> None:
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

            class FailingBackend(FakeBackend):
                def send(self, message):
                    if message.get("method") == "thread/start":
                        raise AiAuthSwitchError("broken backend pipe")
                    super().send(message)

            server = PoolAppServer(
                store,
                provider,
                backend_factory=FailingBackend,
                output_stream=io.StringIO(),
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            server._handle_client_message({"method": "initialize", "id": 1})

            with self.assertRaisesRegex(AiAuthSwitchError, "broken backend pipe"):
                server._handle_client_message({"method": "thread/start", "id": 2})

            self.assertNotIn(("a", "int:2"), server.pending)
            leases = server.coordinator.load().leases
            self.assertEqual(leases, [])
            # _send_to_backend must leave reporting of the failed request to
            # the run loop; otherwise the client would receive two responses
            # for id=2.
            responses = [
                json.loads(line)
                for line in server.output_stream.getvalue().splitlines()
            ]
            self.assertEqual([item["id"] for item in responses], [1])

    def test_run_returns_scoped_error_after_backend_send_failure(self) -> None:
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

            class FailingBackend(FakeBackend):
                def send(self, message):
                    if message.get("method") == "config/read":
                        raise AiAuthSwitchError("broken backend pipe")
                    super().send(message)

            client_read, client_write = os.pipe()
            server_read, server_write = os.pipe()
            client_input = os.fdopen(client_read, "r", encoding="utf-8")
            client_output = os.fdopen(server_write, "w", encoding="utf-8")
            server = PoolAppServer(
                store,
                provider,
                input_stream=client_input,
                output_stream=client_output,
                backend_factory=FailingBackend,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            thread = __import__("threading").Thread(target=server.run, daemon=True)
            thread.start()

            def request(message: dict) -> dict:
                os.write(client_write, (json.dumps(message) + "\n").encode())
                ready, _, _ = select.select([server_read], [], [], 5)
                self.assertTrue(ready)
                return json.loads(os.read(server_read, 65536).decode().splitlines()[0])

            try:
                self.assertEqual(request({"method": "initialize", "id": 1})["id"], 1)
                error = request({"method": "config/read", "id": 2})
                self.assertEqual(error["id"], 2)
                self.assertEqual(error["error"]["code"], -32005)
                # The pool loop remains alive and can answer a later request
                # rather than exiting on the first broken backend write.
                error = request({"method": "config/read", "id": 3})
                self.assertEqual(error["id"], 3)
                self.assertEqual(error["error"]["code"], -32005)
            finally:
                os.close(client_write)
                thread.join(timeout=5)
                client_input.close()
                client_output.close()
                os.close(server_read)

    def test_backend_server_requests_are_forwarded_and_responses_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(provider, "a", '{"email":"a@example.com"}')
            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                command=("fake-codex",),
                output_stream=output,
                backend_factory=FakeBackend,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            server._handle_client_message(
                {"method": "initialize", "id": 1, "params": {}}
            )
            backend = server.backends["a"]
            server._handle_backend_message(
                "a",
                {"method": "item/commandExecution/requestApproval", "id": 5},
            )
            forwarded = json.loads(output.getvalue().splitlines()[-1])
            self.assertTrue(forwarded["id"].startswith("pool:a:"))
            server._handle_client_message(
                {"id": forwarded["id"], "result": {"decision": "accept"}}
            )
            self.assertEqual(backend.messages[-1]["id"], 5)

    def test_thread_list_aggregates_started_backends(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(provider, name, '{"email":"a@example.com"}')
            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=output,
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            server._handle_client_message({"method": "initialize", "id": 1})
            server._ensure_backend("b")
            server._handle_client_message(
                {"method": "thread/list", "id": 2, "params": {}}
            )
            for profile in ("a", "b"):
                backend = server.backends[profile]
                request = backend.messages[-1]
                server._handle_backend_message(
                    profile,
                    {
                        "id": request["id"],
                        "result": {"data": [{"id": f"{profile}-thread"}]},
                    },
                )
            result = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(result["id"], 2)
            self.assertEqual(
                {item["id"] for item in result["result"]["data"]},
                {"a-thread", "b-thread"},
            )

    def test_dead_thread_backend_migrates_before_next_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            for name in ("a", "b"):
                store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"email": f"{name}@example.com"}),
                )
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=io.StringIO(),
                usage_fetcher=lambda _profiles, **_kwargs: {
                    "a": usage(80),
                    "b": usage(70),
                },
            )
            server._handle_client_message({"method": "initialize", "id": 1})
            server._handle_client_message({"method": "thread/start", "id": 2})
            first = next(
                backend
                for backend in server.backends.values()
                if backend.messages and backend.messages[-1]["method"] == "thread/start"
            )
            server._handle_backend_message(
                first.profile,
                {"id": 2, "result": {"thread": {"id": "thr-1"}}},
            )
            first.running = False
            server._handle_client_message(
                {"method": "turn/start", "id": 3, "params": {"threadId": "thr-1"}}
            )
            migrated = "b" if first.profile == "a" else "a"
            self.assertEqual(server.routes.backend_for_thread("thr-1"), migrated)
            self.assertEqual(
                server.backends[migrated].messages[-1]["method"], "turn/start"
            )

    def test_unhealthy_running_thread_backend_migrates_before_forwarding(self) -> None:
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
            usages = {"a": usage(90), "b": usage(80)}
            server = PoolAppServer(
                store,
                provider,
                backend_factory=FakeBackend,
                output_stream=io.StringIO(),
                usage_fetcher=lambda _profiles, **_kwargs: usages,
            )
            server._handle_client_message({"method": "initialize", "id": 1})
            server._handle_client_message({"method": "thread/start", "id": 2})
            first = next(
                backend
                for backend in server.backends.values()
                if backend.messages and backend.messages[-1]["method"] == "thread/start"
            )
            server._handle_backend_message(
                first.profile,
                {"id": 2, "result": {"thread": {"id": "thr-1"}}},
            )
            server.coordinator.mark_failure(first.profile, "401", "expired")

            server._handle_client_message(
                {"method": "turn/start", "id": 3, "params": {"threadId": "thr-1"}}
            )

            migrated = "b" if first.profile == "a" else "a"
            self.assertEqual(server.routes.backend_for_thread("thr-1"), migrated)
            self.assertEqual(
                server.backends[migrated].messages[-1]["method"], "turn/start"
            )

    def test_backend_failure_completes_pending_client_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(provider, "a", '{"email":"a@example.com"}')
            output = io.StringIO()
            server = PoolAppServer(
                store,
                provider,
                output_stream=output,
                backend_factory=FakeBackend,
                usage_fetcher=lambda _profiles, **_kwargs: {"a": usage(80)},
            )
            server._handle_client_message({"method": "initialize", "id": 1})
            server._handle_client_message({"method": "config/read", "id": 2})
            server._fail_backend("a", "backend exited")
            error = json.loads(output.getvalue().splitlines()[-1])
            self.assertEqual(error["id"], 2)
            self.assertEqual(error["error"]["code"], -32005)

    def test_expired_tokens_are_refreshed_before_usage_is_probed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider, "a", json.dumps({"tokens": {"access_token": "token-a"}})
            )
            order: list[str] = []

            def usage_fetcher(_profiles, **_kwargs):
                order.append("usage")
                return {"a": usage(80)}

            def make_server(auto_refresh: bool) -> PoolAppServer:
                server = PoolAppServer(
                    store,
                    provider,
                    command=("fake-codex",),
                    config=PoolServerConfig(auto_refresh=auto_refresh),
                    input_stream=io.StringIO(),
                    output_stream=io.StringIO(),
                    backend_factory=FakeBackend,
                    usage_fetcher=usage_fetcher,
                )
                server.coordinator.refresh_stale_auth = (
                    lambda profiles, **_kwargs: order.append("refresh") or []
                )
                return server

            make_server(True)._profiles_and_usage()
            self.assertEqual(order, ["refresh", "usage"])

            order.clear()
            make_server(False)._profiles_and_usage()
            self.assertEqual(order, ["usage"])


if __name__ == "__main__":
    unittest.main()
