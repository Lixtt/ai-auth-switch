from __future__ import annotations

import io
import json
import os
import select
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
