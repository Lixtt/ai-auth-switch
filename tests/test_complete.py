from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.complete import complete_words
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore


class CompletionTests(unittest.TestCase):
    def test_top_level_commands(self) -> None:
        candidates = complete_words([""])
        for expected in ("auth", "alias", "desktop", "pool", "run", "completion"):
            self.assertIn(expected, candidates)
        # Prefix filtering applies to the current word.
        self.assertIn("auth", complete_words(["a"]))
        self.assertIn("run", complete_words(["r"]))

    def test_auth_subcommands_include_default_and_bind(self) -> None:
        candidates = complete_words(["auth", ""])
        for expected in (
            "list",
            "current",
            "save",
            "use",
            "sync",
            "login",
            "rename",
            "remove",
            "default",
            "bind",
            "export",
            "import",
        ):
            self.assertIn(expected, candidates)

    def test_profiles_complete_for_use_default_and_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(
                codex_home=codex_home, login_command=["fake-codex"]
            )
            store = AuthStore(store_dir)
            active = codex_home / "auth.json"
            active.write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "alice@example.com"}),
                encoding="utf-8",
            )
            with store.lock():
                store.save_current(provider)
            active.unlink()
            active.write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "bob@example.com"}),
                encoding="utf-8",
            )
            with store.lock():
                store.save_current(provider)

            base = [
                "--store-dir",
                str(store_dir),
                "--codex-home",
                str(codex_home),
            ]
            for command in ("use", "default", "bind", "remove"):
                candidates = complete_words(base + ["auth", command, "codex", "b"])
                self.assertIn("bob@example.com", candidates, msg=command)
                self.assertNotIn("alice@example.com", candidates, msg=command)
            # The run command completes profiles too, via its optional name.
            candidates = complete_words(base + ["run", "codex", "b"])
            self.assertIn("bob@example.com", candidates)
            self.assertNotIn("alice@example.com", candidates)

    def test_alias_names_complete_for_alias_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(
                codex_home=codex_home, login_command=["fake-codex"]
            )
            store = AuthStore(store_dir)
            active = codex_home / "auth.json"
            active.write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "a@example.com"}),
                encoding="utf-8",
            )
            with store.lock():
                store.save_current(provider)
                store.set_alias(provider, "work", "a@example.com")

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            candidates = complete_words(base + ["alias", "run", "w"])
            self.assertIn("work", candidates)

    def test_remainder_after_double_dash_yields_nothing(self) -> None:
        self.assertEqual(
            complete_words(["run", "codex", "a@example.com", "--", ""]), []
        )

    def test_desktop_subcommands_complete(self) -> None:
        self.assertIn("auto", complete_words(["desktop", ""]))
        self.assertIn("rotate", complete_words(["desktop", ""]))
        self.assertIn("install", complete_words(["desktop", "auto", ""]))
        self.assertIn("status", complete_words(["desktop", "auto", ""]))

    def test_run_auto_option_completes_after_provider(self) -> None:
        self.assertIn("--auto", complete_words(["run", "codex", "--a"]))

    def test_pool_app_server_subcommand_completes(self) -> None:
        self.assertIn("app-server", complete_words(["pool", ""]))
        self.assertIn("responses", complete_words(["pool", ""]))

    def test_desktop_pool_subcommand_completes(self) -> None:
        self.assertIn("install", complete_words(["desktop", "pool", ""]))

    def test_invalid_input_does_not_crash(self) -> None:
        self.assertEqual(complete_words(["auth", "not-a-command", ""]), [])


if __name__ == "__main__":
    unittest.main()
