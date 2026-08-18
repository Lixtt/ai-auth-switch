from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.complete import complete_words
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers.claude import ClaudeProvider
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.wrapper import run_with_profile


def _credentials(label: str) -> dict:
    return {
        "claudeAiOauth": {
            "accessToken": f"access-{label}",
            "refreshToken": f"refresh-{label}",
            "expiresAt": 2_000_000_000_000,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
            "rateLimitTier": "default_claude_max_20x",
        }
    }


def _state(email: str, account_uuid: str, **extra) -> dict:
    return {
        **extra,
        "oauthAccount": {
            "accountUuid": account_uuid,
            "emailAddress": email,
            "displayName": email.split("@", 1)[0],
            "organizationUuid": f"org-{account_uuid}",
            "accessToken": "must-not-enter-metadata",
        },
    }


def _write_active(provider: ClaudeProvider, email: str, account_uuid: str) -> None:
    provider.config_dir.mkdir(parents=True, exist_ok=True)
    active = provider.active_auth_path
    if active.exists() or active.is_symlink():
        active.unlink()
    active.write_text(json.dumps(_credentials(account_uuid)), encoding="utf-8")
    provider.config_state_path.write_text(
        json.dumps(_state(email, account_uuid, machineID="machine-1")),
        encoding="utf-8",
    )


class ClaudeProviderTests(unittest.TestCase):
    def test_claude_and_codex_numbered_aliases_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            claude = ClaudeProvider(
                config_dir=root / ".claude",
                login_command=["fake-claude"],
            )
            codex = CodexProvider(
                codex_home=root / ".codex",
                login_command=["fake-codex"],
            )
            codex.active_auth_path.parent.mkdir(parents=True)
            codex.active_auth_path.write_text(
                json.dumps({"email": "codex@example.com"}), encoding="utf-8"
            )
            with store.lock():
                store.save_current(codex)
                store.sync_numbered_aliases(codex)

            _write_active(claude, "claude@example.com", "account-claude")
            with store.lock():
                store.save_current(claude)
                store.sync_numbered_aliases(claude)

            self.assertEqual(store.resolve_alias("codex1").profile, "codex@example.com")
            self.assertEqual(
                store.resolve_alias("claude1").profile,
                "claude@example.com",
            )

    def test_save_infers_email_and_metadata_follows_activation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ClaudeProvider(
                config_dir=root / ".claude",
                login_command=["fake-claude"],
            )
            store = AuthStore(root / "store")

            _write_active(provider, "alice@example.com", "account-alice")
            with store.lock():
                alice = store.save_current(provider)
                store.sync_numbered_aliases(provider)
            self.assertEqual(alice.name, "alice@example.com")
            self.assertTrue(provider.active_auth_path.is_symlink())
            metadata = store.read_profile_metadata(provider, alice.name)
            self.assertEqual(
                metadata["oauthAccount"]["accountUuid"],
                "account-alice",
            )
            self.assertNotIn("accessToken", metadata["oauthAccount"])

            _write_active(provider, "bob@example.com", "account-bob")
            with store.lock():
                bob = store.save_current(provider)
                aliases = store.sync_numbered_aliases(provider)
            self.assertEqual(bob.name, "bob@example.com")
            self.assertEqual(
                [(alias.name, alias.profile) for alias in aliases],
                [
                    ("claude1", "bob@example.com"),
                    ("claude2", "alice@example.com"),
                ],
            )

            with store.lock():
                store.activate(provider, "alice@example.com")
            state = json.loads(provider.config_state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["machineID"], "machine-1")
            self.assertEqual(
                state["oauthAccount"]["emailAddress"],
                "alice@example.com",
            )

    def test_rename_and_remove_move_metadata_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = ClaudeProvider(config_dir=root / ".claude")
            store = AuthStore(root / "store")
            _write_active(provider, "alice@example.com", "account-alice")
            with store.lock():
                store.save_current(provider)
                store.rename(provider, "alice@example.com", "work")
            self.assertIsNotNone(store.read_profile_metadata(provider, "work"))
            self.assertIsNone(
                store.read_profile_metadata(provider, "alice@example.com")
            )

            provider.active_auth_path.unlink()
            with store.lock():
                store.remove(provider, "work")
            metadata_path = store.profile_metadata_path(provider, "work")
            self.assertIsNotNone(metadata_path)
            self.assertFalse(metadata_path.exists())


class ClaudeWrapperTests(unittest.TestCase):
    def _save_two(self, root: Path) -> tuple[ClaudeProvider, AuthStore]:
        provider = ClaudeProvider(
            config_dir=root / ".claude",
            login_command=["fake-claude"],
        )
        store = AuthStore(root / "store")
        _write_active(provider, "alice@example.com", "account-alice")
        with store.lock():
            store.save_current(provider)
        _write_active(provider, "bob@example.com", "account-bob")
        with store.lock():
            store.save_current(provider)
        (provider.config_dir / "settings.json").write_text(
            '{"theme":"dark"}', encoding="utf-8"
        )
        return provider, store

    def test_run_isolates_auth_shares_state_and_syncs_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, store = self._save_two(root)
            runtime = root / "runtime"
            runtime.mkdir()

            def fake_call(command, *, env):
                self.assertEqual(command, ["fake-claude", "-p", "hello"])
                isolated = Path(env["CLAUDE_CONFIG_DIR"])
                self.assertNotEqual(isolated, provider.config_dir)
                self.assertTrue((isolated / "settings.json").is_symlink())
                auth = json.loads(
                    (isolated / ".credentials.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    auth["claudeAiOauth"]["refreshToken"],
                    "refresh-account-alice",
                )
                state = json.loads(
                    (isolated / ".claude.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    state["oauthAccount"]["emailAddress"],
                    "alice@example.com",
                )
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)

                isolated_auth = isolated / ".credentials.json"
                isolated_auth.unlink()
                isolated_auth.write_text(
                    json.dumps(_credentials("account-alice-refreshed")),
                    encoding="utf-8",
                )
                return 0

            with mock.patch.dict(
                os.environ,
                {"ANTHROPIC_AUTH_TOKEN": "external-override"},
                clear=False,
            ), mock.patch(
                "ai_auth_switch.wrapper._claude_runtime_parent",
                return_value=runtime,
            ), mock.patch(
                "ai_auth_switch.wrapper.subprocess.call",
                side_effect=fake_call,
            ):
                status = run_with_profile(
                    store,
                    provider,
                    "alice@example.com",
                    ["fake-claude", "-p", "hello"],
                )
            self.assertEqual(status, 0)
            refreshed = json.loads(
                store.profile_path(provider, "alice@example.com").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                refreshed["claudeAiOauth"]["refreshToken"],
                "refresh-account-alice-refreshed",
            )
            self.assertEqual(store.current_profile(provider).name, "bob@example.com")

    def test_run_rejects_cross_account_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider, store = self._save_two(root)
            runtime = root / "runtime"
            runtime.mkdir()
            original = store.read_profile_content(provider, "alice@example.com")

            def fake_call(_command, *, env):
                isolated = Path(env["CLAUDE_CONFIG_DIR"])
                auth = isolated / ".credentials.json"
                auth.unlink()
                auth.write_text(
                    json.dumps(_credentials("account-mallory")), encoding="utf-8"
                )
                (isolated / ".claude.json").write_text(
                    json.dumps(_state("mallory@example.com", "account-mallory")),
                    encoding="utf-8",
                )
                return 0

            with mock.patch(
                "ai_auth_switch.wrapper._claude_runtime_parent",
                return_value=runtime,
            ), mock.patch(
                "ai_auth_switch.wrapper.subprocess.call",
                side_effect=fake_call,
            ), self.assertRaises(AiAuthSwitchError):
                run_with_profile(
                    store,
                    provider,
                    "alice@example.com",
                    ["fake-claude"],
                )
            self.assertEqual(
                store.read_profile_content(provider, "alice@example.com"),
                original,
            )
            rejected = list((store.backups_dir(provider) / "rejected").glob("*.json"))
            self.assertEqual(len(rejected), 1)


class ClaudeCliTests(unittest.TestCase):
    def test_failed_login_restores_credentials_and_account_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".claude"
            provider = ClaudeProvider(config_dir=config_dir)
            store = AuthStore(root / "store")
            _write_active(provider, "before@example.com", "account-before")
            with store.lock():
                store.save_current(provider)

            def failed_login(login_provider, _args):
                login_provider.active_auth_path.write_text(
                    json.dumps(_credentials("account-after")), encoding="utf-8"
                )
                login_provider.config_state_path.write_text(
                    json.dumps(_state("after@example.com", "account-after")),
                    encoding="utf-8",
                )
                return 1

            argv = [
                "--store-dir",
                str(store.base_dir),
                "--claude-config-dir",
                str(config_dir),
                "auth",
                "login",
                "claude",
            ]
            with mock.patch(
                "ai_auth_switch.cli._run_login",
                side_effect=failed_login,
            ):
                self.assertEqual(cli_main(argv), 1)
            self.assertEqual(
                store.current_profile(provider).name,
                "before@example.com",
            )
            state = json.loads(provider.config_state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                state["oauthAccount"]["emailAddress"],
                "before@example.com",
            )

    def test_parser_completion_and_login_command(self) -> None:
        self.assertIn("claude", complete_words(["auth", "save", "cl"]))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".claude"
            store_dir = root / "store"

            def fake_login(command, *, env):
                self.assertEqual(command, ["fake-claude", "auth", "login"])
                self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(config_dir))
                self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
                config_dir.mkdir(parents=True, exist_ok=True)
                (config_dir / ".credentials.json").write_text(
                    json.dumps(_credentials("account-alice")), encoding="utf-8"
                )
                (config_dir / ".claude.json").write_text(
                    json.dumps(_state("alice@example.com", "account-alice")),
                    encoding="utf-8",
                )
                return 0

            argv = [
                "--store-dir",
                str(store_dir),
                "--claude-config-dir",
                str(config_dir),
                "auth",
                "login",
                "claude",
            ]
            with mock.patch(
                "ai_auth_switch.providers.claude.default_claude_command",
                return_value=["fake-claude"],
            ), mock.patch.dict(
                os.environ,
                {"ANTHROPIC_AUTH_TOKEN": "external-override"},
                clear=False,
            ), mock.patch(
                "ai_auth_switch.cli.subprocess.call",
                side_effect=fake_login,
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(cli_main(argv), 0)

            provider = ClaudeProvider(config_dir=config_dir)
            store = AuthStore(store_dir)
            self.assertEqual(store.current_profile(provider).name, "alice@example.com")
            self.assertEqual(store.resolve_alias("claude1").profile, "alice@example.com")

    def test_export_import_preserves_claude_identity_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_dir = root / ".claude"
            provider = ClaudeProvider(config_dir=config_dir)
            store = AuthStore(root / "store")
            _write_active(provider, "alice@example.com", "account-alice")
            with store.lock():
                store.save_current(provider)

            export_file = root / "profiles.json"
            base = [
                "--store-dir",
                str(store.base_dir),
                "--claude-config-dir",
                str(config_dir),
            ]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(
                    cli_main(base + ["auth", "export", "claude", "-o", str(export_file)]),
                    0,
                )
            payload = json.loads(export_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(
                payload["profile_metadata"]["claude"]["alice@example.com"]
                ["oauthAccount"]["accountUuid"],
                "account-alice",
            )

            imported = AuthStore(root / "imported")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "--store-dir",
                            str(imported.base_dir),
                            "--claude-config-dir",
                            str(config_dir),
                            "auth",
                            "import",
                            str(export_file),
                        ]
                    ),
                    0,
                )
            metadata = imported.read_profile_metadata(provider, "alice@example.com")
            self.assertEqual(metadata["oauthAccount"]["accountUuid"], "account-alice")


if __name__ == "__main__":
    unittest.main()
