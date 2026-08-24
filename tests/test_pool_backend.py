from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from ai_auth_switch.pool_backend import BackendProcess
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore

FAKE_BACKEND = (
    "import json, os, sys\n"
    "for line in sys.stdin:\n"
    "    message = json.loads(line)\n"
    "    if message.get('id') is None:\n"
    "        continue\n"
    "    result = {'ok': True, 'runtime_home': os.environ.get('CODEX_HOME')}\n"
    "    if message.get('method') == 'thread/start':\n"
    "        result = {'thread': {'id': 'thr-fake'}}\n"
    "    print(json.dumps({'id': message['id'], 'result': result}), flush=True)\n"
)


class BackendProcessTests(unittest.TestCase):
    def test_isolates_auth_and_completes_jsonl_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            provider = CodexProvider(codex_home, ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(
                provider,
                "a",
                json.dumps({"auth_mode": "chatgpt", "email": "a@example.com"}),
            )
            runtime_parent = root / "runtime"
            runtime_parent.mkdir()
            backend = BackendProcess(
                store,
                provider,
                "a",
                command=(sys.executable, "-c", FAKE_BACKEND),
                runtime_parent=runtime_parent,
            )
            try:
                initialized = backend.start()
                self.assertTrue(initialized["ok"])
                self.assertNotEqual(backend.info.runtime_home, codex_home)
                self.assertTrue(backend.info.auth_path.is_symlink())
                backend.send({"method": "thread/start", "id": 7, "params": {}})
                response = backend.read_message()
                self.assertEqual(response["result"]["thread"]["id"], "thr-fake")
            finally:
                backend.stop()
            self.assertFalse(backend.running)
            self.assertFalse(list(runtime_parent.iterdir()))

    def test_backend_process_can_be_used_as_context_manager(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            provider.active_auth_path.parent.mkdir(parents=True)
            store.write_profile_content(provider, "a", '{"email":"a@example.com"}')
            runtime_parent = root / "runtime"
            runtime_parent.mkdir()
            with BackendProcess(
                store,
                provider,
                "a",
                command=(sys.executable, "-c", FAKE_BACKEND),
                runtime_parent=runtime_parent,
            ) as backend:
                self.assertTrue(backend.running)
            self.assertFalse(backend.running)


if __name__ == "__main__":
    unittest.main()
