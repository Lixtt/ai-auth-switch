from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore


def fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def enc(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc(header)}.{enc(payload)}."


class CodexStoreTests(unittest.TestCase):
    def test_save_current_infers_email_profile_and_activates_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()
            auth = codex_home / "auth.json"
            auth.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": fake_jwt(
                                {
                                    "https://api.openai.com/profile": {
                                        "email": "person@example.com"
                                    }
                                }
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )

            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(store_dir)
            with store.lock():
                profile = store.save_current(provider)

            self.assertEqual(profile.name, "person@example.com")
            self.assertTrue(auth.is_symlink())
            self.assertEqual(auth.resolve(), profile.path)
            self.assertEqual(store.current_profile(provider).name, "person@example.com")

    def test_temporary_activation_restores_previous_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(root / "store")

            for name in ("a@example.com", "b@example.com"):
                auth = codex_home / "auth.json"
                if auth.exists() or auth.is_symlink():
                    auth.unlink()
                auth.write_text(
                    json.dumps({"auth_mode": "chatgpt", "email": name}),
                    encoding="utf-8",
                )
                with store.lock():
                    store.save_current(provider, name)

            with store.lock():
                store.activate(provider, "a@example.com")
                with store.activated_temporarily(provider, "b@example.com"):
                    self.assertEqual(store.current_profile(provider).name, "b@example.com")
                self.assertEqual(store.current_profile(provider).name, "a@example.com")

    def test_temporary_activation_syncs_atomic_auth_replacement_back_to_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(root / "store")

            active = codex_home / "auth.json"
            active.write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "a@example.com"}),
                encoding="utf-8",
            )
            with store.lock():
                store.save_current(provider, "a@example.com")

            with store.lock():
                with store.activated_temporarily(provider, "a@example.com"):
                    active.unlink()
                    active.write_text(
                        json.dumps(
                            {"auth_mode": "chatgpt", "email": "a@example.com", "refreshed": True}
                        ),
                        encoding="utf-8",
                    )

            profile_path = store.profile_path(provider, "a@example.com")
            refreshed = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertTrue(refreshed["refreshed"])


if __name__ == "__main__":
    unittest.main()
