from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import ai_auth_switch.hermes_codex_sync as hermes_codex_sync
from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore


def _save_profile(store: AuthStore, provider: CodexProvider, name: str) -> None:
    active = provider.active_auth_path
    if active.exists() or active.is_symlink():
        active.unlink()
    active.write_text(
        json.dumps({"auth_mode": "chatgpt", "email": name}),
        encoding="utf-8",
    )
    with store.lock():
        store.save_current(provider, name)


class LoginRollbackTests(unittest.TestCase):
    def test_failed_login_restores_previous_active_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "before@example.com")
            active = provider.active_auth_path
            self.assertTrue(active.is_symlink())

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with mock.patch("ai_auth_switch.cli._run_login", return_value=1):
                with contextlib.redirect_stdout(io.StringIO()):
                    status = cli_main(base + ["auth", "login", "codex"])
            self.assertEqual(status, 1)
            # The previous active profile is restored after a failed login.
            self.assertTrue(active.is_symlink())
            self.assertEqual(store.current_profile(provider).name, "before@example.com")
            self.assertEqual(
                set(p.name for p in store.list_profiles(provider)),
                {"before@example.com"},
            )

    def test_login_saving_failure_restores_previous_active_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "before@example.com")
            active = provider.active_auth_path

            def fake_login(provider, login_args):
                # Login succeeds and writes a new auth file.
                active.write_text(
                    json.dumps({"auth_mode": "chatgpt", "email": "new@example.com"}),
                    encoding="utf-8",
                )
                return 0

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with mock.patch("ai_auth_switch.cli._run_login", fake_login), mock.patch(
                "ai_auth_switch.cli.AuthStore.save_current",
                side_effect=AiAuthSwitchError("could not save profile"),
            ):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    status = cli_main(base + ["auth", "login", "codex"])
            self.assertEqual(status, 2)
            self.assertIn("could not save profile", err.getvalue())
            # The failed save must not leave a detached active auth behind.
            self.assertTrue(active.is_symlink())
            self.assertEqual(store.current_profile(provider).name, "before@example.com")


class WrapperFailureTests(unittest.TestCase):
    def test_run_with_unknown_profile_reports_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(base + ["run", "codex", "nobody@example.com"])
            self.assertEqual(status, 2)
            self.assertIn("profile not found", err.getvalue())


def _install_fake_hermes_modules(store: dict, suppressed: dict) -> None:
    """Install in-memory stand-ins for Hermes modules into sys.modules."""
    hermes = types.ModuleType("hermes_cli")
    hermes_cli_auth = types.ModuleType("hermes_cli.auth")
    hermes_cli_auth.DEFAULT_CODEX_BASE_URL = "https://api.openai.com/v1"

    @contextlib.contextmanager
    def _auth_store_lock():
        yield

    def _load_auth_store() -> dict:
        return {
            "version": 1,
            "providers": {"openai-codex": {}},
            "credential_pool": {
                "openai-codex": [
                    {"source": "manual:device_code", "access_token": "old"},
                    {"source": "device_code", "access_token": "old2"},
                ]
            },
            "suppressed_sources": dict(suppressed),
        }

    def _save_auth_store(auth_store: dict) -> None:
        store.update(auth_store)

    def _update_config_for_provider(provider: str, base_url: str) -> str:
        return "/tmp/hermes/config.toml"

    hermes_cli_auth._auth_store_lock = _auth_store_lock
    hermes_cli_auth._load_auth_store = _load_auth_store
    hermes_cli_auth._save_auth_store = _save_auth_store
    hermes_cli_auth._update_config_for_provider = _update_config_for_provider

    runtime = types.ModuleType("hermes_cli.codex_runtime_switch")
    runtime.set_runtime = lambda config, mode: config.__setitem__("runtime", mode)

    config_module = types.ModuleType("hermes_cli.config")
    config_module.load_config = lambda: {"model": {"provider": "gpt-5"}}
    config_module.save_config = lambda config: config.__setitem__("saved", True)

    credential_pool = types.ModuleType("agent.credential_pool")
    credential_pool.load_pool = lambda provider: None

    sys.modules["hermes_cli"] = hermes
    sys.modules["hermes_cli.auth"] = hermes_cli_auth
    sys.modules["hermes_cli.codex_runtime_switch"] = runtime
    sys.modules["hermes_cli.config"] = config_module
    sys.modules["agent"] = types.ModuleType("agent")
    sys.modules["agent.credential_pool"] = credential_pool


class HermesCodexSyncTests(unittest.TestCase):
    def test_seeds_access_token_and_clears_stale_pool_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agent_dir = root / "hermes-agent"
            auth_path = root / "auth.json"
            auth_path.write_text(
                json.dumps(
                    {
                        "tokens": {"access_token": "tok-123"},
                        "last_refresh": "2026-01-01T00:00:00Z",
                    }
                ),
                encoding="utf-8",
            )
            store: dict = {}
            suppressed: dict = {"openai-codex": "whatever"}
            _install_fake_hermes_modules(store, suppressed)

            env = {
                "AI_AUTH_SWITCH_HERMES_AGENT_DIR": str(agent_dir),
                "AI_AUTH_SWITCH_HERMES_PROFILE_NAME": "person@example.com",
                "AI_AUTH_SWITCH_CODEX_AUTH_PATH": str(auth_path),
                "AI_AUTH_SWITCH_HERMES_CLI_ACCESS_SOURCE": "manual:codex-cli-access-token",
                "AI_AUTH_SWITCH_HERMES_LEGACY_BRIDGE_SOURCE": "manual:codex-cli-bridge",
                "AI_AUTH_SWITCH_HERMES_DEFAULT_MODEL": "gpt-5.5",
            }
            out = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=False):
                with contextlib.redirect_stdout(out):
                    self.assertEqual(hermes_codex_sync.main(), 0)

            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "synced")
            self.assertEqual(payload["profile"], "person@example.com")
            self.assertEqual(payload["runtime"], "auto")

            pool = store["credential_pool"]["openai-codex"]
            self.assertEqual(len(pool), 1)
            entry = pool[0]
            self.assertEqual(entry["access_token"], "tok-123")
            self.assertEqual(entry["source"], "manual:codex-cli-access-token")
            self.assertEqual(entry["label"], "Codex CLI (person@example.com)")
            self.assertEqual(entry["priority"], 0)
            self.assertEqual(entry["last_refresh"], "2026-01-01T00:00:00Z")
            self.assertEqual(store["active_provider"], "openai-codex")
            # The stale device-code entries were removed.
            self.assertNotIn("device_code", {e["source"] for e in pool})
            self.assertNotIn("suppressed_sources", store)

    def test_refuses_missing_access_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth_path = root / "auth.json"
            auth_path.write_text(json.dumps({"tokens": {}}), encoding="utf-8")
            _install_fake_hermes_modules({}, {})
            env = {
                "AI_AUTH_SWITCH_HERMES_AGENT_DIR": str(root / "hermes-agent"),
                "AI_AUTH_SWITCH_HERMES_PROFILE_NAME": "p@example.com",
                "AI_AUTH_SWITCH_CODEX_AUTH_PATH": str(auth_path),
                "AI_AUTH_SWITCH_HERMES_CLI_ACCESS_SOURCE": "manual:codex-cli-access-token",
                "AI_AUTH_SWITCH_HERMES_LEGACY_BRIDGE_SOURCE": "manual:codex-cli-bridge",
                "AI_AUTH_SWITCH_HERMES_DEFAULT_MODEL": "gpt-5.5",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                with self.assertRaises(SystemExit) as ctx:
                    hermes_codex_sync.main()
            self.assertIn("missing access_token", str(ctx.exception))
