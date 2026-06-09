from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.sync import (
    OPENAI_CODEX_DEFAULT_PROFILE,
    _hermes_snapshot_path,
    _sync_current_hermes_state_back_to_snapshot,
    sync_codex_dependents,
)
from ai_auth_switch.cli import main as cli_main


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

    def test_permanent_activation_syncs_replaced_active_auth_back_by_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(root / "store")

            for name in ("a@example.com", "b@example.com"):
                active = codex_home / "auth.json"
                if active.exists() or active.is_symlink():
                    active.unlink()
                active.write_text(
                    json.dumps({"auth_mode": "chatgpt", "email": name}),
                    encoding="utf-8",
                )
                with store.lock():
                    store.save_current(provider, name)

            active = codex_home / "auth.json"
            with store.lock():
                store.activate(provider, "a@example.com")

            # Codex may refresh by atomically replacing auth.json, which breaks
            # the profile symlink. The next switch must not leave profile "a"
            # with the old refresh token.
            active.unlink()
            active.write_text(
                json.dumps(
                    {"auth_mode": "chatgpt", "email": "a@example.com", "refreshed": True}
                ),
                encoding="utf-8",
            )

            with store.lock():
                current = store.current_profile(provider)
                self.assertIsNotNone(current)
                self.assertEqual(current.name, "a@example.com")
                store.activate(provider, "b@example.com")

            profile_path = store.profile_path(provider, "a@example.com")
            refreshed = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertTrue(refreshed["refreshed"])
            self.assertTrue(active.is_symlink())
            self.assertEqual(active.resolve(), store.profile_path(provider, "b@example.com"))

    def test_cli_list_without_provider_prints_codex_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store_dir),
                        "--codex-home",
                        str(codex_home),
                        "auth",
                        "list",
                    ]
                )

            self.assertEqual(status, 0)
            output = out.getvalue()
            self.assertIn("no profiles", output)
            self.assertIn(f"auth file not found at {codex_home / 'auth.json'}", output)

    def test_cli_current_reports_unmanaged_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "person@example.com"}),
                encoding="utf-8",
            )

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store_dir),
                        "--codex-home",
                        str(codex_home),
                        "auth",
                        "current",
                        "codex",
                    ]
                )

            self.assertEqual(status, 1)
            output = out.getvalue()
            self.assertIn("not active", output)
            self.assertIn("unmanaged codex auth found", output)
            self.assertIn("person@example.com", output)


    def test_cli_auth_login_requests_independent_hermes_login(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()
            active_auth = codex_home / "auth.json"

            def fake_login(provider, login_args):
                self.assertEqual(provider.id, "codex")
                self.assertEqual(login_args, [])
                active_auth.write_text(
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
                return 0

            sync_calls = []

            def fake_sync(provider, **kwargs):
                sync_calls.append((provider.id, kwargs))
                return []

            out = io.StringIO()
            with mock.patch("ai_auth_switch.cli._run_login", fake_login):
                with mock.patch("ai_auth_switch.cli.sync_codex_dependents", fake_sync):
                    with contextlib.redirect_stdout(out):
                        status = cli_main(
                            [
                                "--store-dir",
                                str(store_dir),
                                "--codex-home",
                                str(codex_home),
                                "auth",
                                "login",
                                "codex",
                            ]
                        )

            self.assertEqual(status, 0)
            self.assertEqual(len(sync_calls), 1)
            provider_id, kwargs = sync_calls[0]
            self.assertEqual(provider_id, "codex")
            self.assertTrue(kwargs["hermes_login"])
            self.assertEqual(kwargs["hermes_profile_name"], "person@example.com")
            self.assertEqual(kwargs["store_dir"], store_dir)
            self.assertIn("saved codex login as person@example.com", out.getvalue())


    def test_sync_current_hermes_state_updates_matching_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store_dir = root / "store"
            hermes_home = root / ".hermes"
            hermes_home.mkdir()

            old_access = fake_jwt(
                {
                    "exp": 4102444700,
                    "https://api.openai.com/profile": {"email": "person@example.com"},
                }
            )
            new_access = fake_jwt(
                {
                    "exp": 4102444800,
                    "https://api.openai.com/profile": {"email": "person@example.com"},
                }
            )
            old_state = {
                "tokens": {"access_token": old_access, "refresh_token": "rt_old"},
                "last_refresh": "2026-01-01T00:00:00Z",
                "label": "person@example.com",
            }
            new_state = {
                "tokens": {"access_token": new_access, "refresh_token": "rt_new"},
                "last_refresh": "2026-01-02T00:00:00Z",
                "label": "person@example.com",
            }
            snapshot_path = _hermes_snapshot_path("person@example.com", store_dir)
            snapshot_path.parent.mkdir(parents=True)
            snapshot_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "provider": "openai-codex",
                        "profile": "person@example.com",
                        "base_url": "https://chatgpt.com/backend-api/codex",
                        "provider_state": old_state,
                        "credential_pool": [
                            {
                                "source": "device_code",
                                "auth_type": "oauth",
                                "access_token": old_access,
                                "refresh_token": "rt_old",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (hermes_home / "auth.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "active_provider": "openai-codex",
                        "providers": {"openai-codex": new_state},
                        "credential_pool": {
                            "openai-codex": [
                                {
                                    "source": "device_code",
                                    "auth_type": "oauth",
                                    "access_token": new_access,
                                    "refresh_token": "rt_new",
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                _sync_current_hermes_state_back_to_snapshot(store_dir=store_dir)

            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.assertEqual(
                snapshot["provider_state"]["tokens"]["refresh_token"],
                "rt_new",
            )
            self.assertEqual(snapshot["credential_pool"][0]["refresh_token"], "rt_new")


    def test_sync_codex_dependents_updates_openclaw_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": fake_jwt(
                                {
                                    "exp": 4102444800,
                                    "https://api.openai.com/profile": {
                                        "email": "person@example.com"
                                    },
                                }
                            ),
                            "refresh_token": "rt_test",
                        },
                    }
                ),
                encoding="utf-8",
            )

            openclaw = root / ".openclaw"
            agent = openclaw / "agents" / "main" / "agent"
            agent.mkdir(parents=True)
            (agent / "auth-profiles.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "profiles": {
                            OPENAI_CODEX_DEFAULT_PROFILE: {
                                "type": "oauth",
                                "provider": "openai-codex",
                            },
                            "vllm:default": {"type": "api_key", "provider": "vllm"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            (agent / "auth-state.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "order": {"openai-codex": ["openai-codex:old"]},
                        "lastGood": {"openai-codex": "openai-codex:old"},
                        "usageStats": {OPENAI_CODEX_DEFAULT_PROFILE: {"errorCount": 3}},
                    }
                ),
                encoding="utf-8",
            )

            provider = CodexProvider(codex_home=codex_home)
            results = sync_codex_dependents(
                provider,
                sync_hermes=False,
                restart_openclaw=False,
                openclaw_state_dir=openclaw,
            )

            self.assertEqual([result.target for result in results], ["openclaw"])
            self.assertTrue(results[0].ok)
            profiles = json.loads((agent / "auth-profiles.json").read_text(encoding="utf-8"))
            state = json.loads((agent / "auth-state.json").read_text(encoding="utf-8"))
            self.assertNotIn(OPENAI_CODEX_DEFAULT_PROFILE, profiles["profiles"])
            self.assertEqual(state["order"]["openai-codex"], [OPENAI_CODEX_DEFAULT_PROFILE])
            self.assertEqual(state["lastGood"]["openai-codex"], OPENAI_CODEX_DEFAULT_PROFILE)
            self.assertNotIn(OPENAI_CODEX_DEFAULT_PROFILE, state["usageStats"])

    def test_cli_auth_sync_updates_openclaw_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "person@example.com"}),
                encoding="utf-8",
            )

            openclaw = root / ".openclaw"
            agent = openclaw / "agents" / "main" / "agent"
            agent.mkdir(parents=True)
            (agent / "auth-state.json").write_text(
                json.dumps({"version": 1, "order": {}, "lastGood": {}}),
                encoding="utf-8",
            )

            out = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "OPENCLAW_STATE_DIR": str(openclaw),
                    "HERMES_AGENT_DIR": str(root / "missing-hermes"),
                },
            ):
                with contextlib.redirect_stdout(out):
                    status = cli_main(
                        [
                            "--codex-home",
                            str(codex_home),
                            "auth",
                            "sync",
                            "codex",
                            "--no-openclaw-restart",
                        ]
                    )

            self.assertEqual(status, 0)
            self.assertIn("hermes: skipped: Hermes install path not found", out.getvalue())
            self.assertIn("openclaw: synced", out.getvalue())
            state = json.loads((agent / "auth-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["lastGood"]["openai-codex"], OPENAI_CODEX_DEFAULT_PROFILE)


if __name__ == "__main__":
    unittest.main()
