from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
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
    OPENAI_DEFAULT_PROFILE,
    HERMES_CODEX_BRIDGE_SOURCE,
    HERMES_CODEX_CLI_ACCESS_SOURCE,
    sync_codex_dependents,
)
from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.wrapper import run_with_profile


def fake_jwt(payload: dict) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def enc(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{enc(header)}.{enc(payload)}."


class CodexStoreTests(unittest.TestCase):
    def test_existing_profiles_get_numbered_aliases_in_saved_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(root / "store")
            profile_dir = store.provider_dir(provider)
            profile_dir.mkdir(parents=True)

            profiles = (
                ("z-last-alphabetically@example.com", 1_000_000_000),
                ("a-first-alphabetically@example.com", 2_000_000_000),
            )
            for name, modified_ns in profiles:
                path = store.profile_path(provider, name)
                path.write_text(json.dumps({"email": name}), encoding="utf-8")
                os.utime(path, ns=(modified_ns, modified_ns))

            with store.lock():
                listed = store.list_profiles(provider)
                store.sync_numbered_aliases(provider)
                aliases = store.list_aliases()

            self.assertEqual(
                [profile.name for profile in listed],
                [
                    "a-first-alphabetically@example.com",
                    "z-last-alphabetically@example.com",
                ],
            )
            self.assertEqual(
                [(alias.name, alias.profile, alias.command) for alias in aliases],
                [
                    (
                        "codex1",
                        "z-last-alphabetically@example.com",
                        ("fake-codex",),
                    ),
                    (
                        "codex2",
                        "a-first-alphabetically@example.com",
                        ("fake-codex",),
                    ),
                ],
            )

    def test_numbered_aliases_append_and_compact_after_profile_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(root / "store")

            for name in ("z@example.com", "a@example.com", "m@example.com"):
                active = codex_home / "auth.json"
                if active.exists() or active.is_symlink():
                    active.unlink()
                active.write_text(json.dumps({"email": name}), encoding="utf-8")
                with store.lock():
                    store.save_current(provider, name)

            with store.lock():
                self.assertEqual(
                    [alias.profile for alias in store.list_aliases()],
                    ["z@example.com", "a@example.com", "m@example.com"],
                )
                store.set_alias(
                    provider,
                    "codex3",
                    "m@example.com",
                    ["custom-codex"],
                )
                store.set_alias(provider, "middle", "a@example.com", ["fake-codex"])
                store.remove(provider, "a@example.com")
                aliases = store.list_aliases()

            self.assertEqual(
                [(alias.name, alias.profile, alias.command) for alias in aliases],
                [
                    ("codex1", "z@example.com", ("fake-codex",)),
                    ("codex2", "m@example.com", ("custom-codex",)),
                ],
            )
            self.assertIsNone(store.resolve_alias("codex3"))
            self.assertIsNone(store.resolve_alias("middle"))

    def test_profile_rename_keeps_numbered_and_custom_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(root / "store")
            active = codex_home / "auth.json"
            active.write_text(json.dumps({"email": "old@example.com"}), encoding="utf-8")

            with store.lock():
                store.save_current(provider, "old@example.com")
                store.set_alias(provider, "work", "old@example.com", ["fake-codex"])
                renamed = store.rename(provider, "old@example.com", "new@example.com")
                aliases = store.list_aliases()

            self.assertTrue(renamed.active)
            self.assertEqual(store.current_profile(provider).name, "new@example.com")
            self.assertEqual(
                [(alias.name, alias.profile) for alias in aliases],
                [("codex1", "new@example.com"), ("work", "new@example.com")],
            )

    def test_cli_alias_sync_installs_and_removes_numbered_executables(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            bin_dir = root / "bin"
            codex_home.mkdir()
            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(store_dir)
            profile_dir = store.provider_dir(provider)
            profile_dir.mkdir(parents=True)
            for index, name in enumerate(("first@example.com", "second@example.com"), start=1):
                path = store.profile_path(provider, name)
                path.write_text(json.dumps({"email": name}), encoding="utf-8")
                os.utime(path, ns=(index, index))

            target = root / "ai-auth-switch-target"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o700)

            command = [
                "--store-dir",
                str(store_dir),
                "--codex-home",
                str(codex_home),
                "alias",
                "sync",
                "codex",
                "--bin-dir",
                str(bin_dir),
                "--target",
                str(target),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                status = cli_main(command)

            self.assertEqual(status, 0)
            self.assertEqual((bin_dir / "codex1").resolve(), target.resolve())
            self.assertEqual((bin_dir / "codex2").resolve(), target.resolve())

            legacy_target = root / "ai-auth-switch-legacy"
            legacy_target.write_text("#!/bin/sh\n", encoding="utf-8")
            legacy_target.chmod(0o700)
            (bin_dir / "codex1").unlink()
            os.symlink(legacy_target, bin_dir / "codex1")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(command)

            self.assertEqual(status, 0)
            self.assertEqual((bin_dir / "codex1").resolve(), target.resolve())
            self.assertIn("updated automatic alias codex1", out.getvalue())

            with store.lock():
                store.remove(provider, "first@example.com")
            with contextlib.redirect_stdout(io.StringIO()):
                status = cli_main(command)

            self.assertEqual(status, 0)
            self.assertTrue((bin_dir / "codex1").is_symlink())
            self.assertFalse((bin_dir / "codex2").exists())
            self.assertFalse((bin_dir / "codex2").is_symlink())
            self.assertEqual(store.resolve_alias("codex1").profile, "second@example.com")

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
            with mock.patch.object(
                AuthStore,
                "lock",
                side_effect=AssertionError("auth list must not take the global lock"),
            ):
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
            with mock.patch.object(
                AuthStore,
                "lock",
                side_effect=AssertionError("auth current must not take the global lock"),
            ):
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

    def test_cli_alias_set_list_and_run_uses_selected_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(store_dir)

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

            with contextlib.redirect_stdout(io.StringIO()):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store_dir),
                        "--codex-home",
                        str(codex_home),
                        "alias",
                        "set",
                        "codex1",
                        "codex",
                        "a@example.com",
                        "--",
                        "fake-codex",
                    ]
                )
            self.assertEqual(status, 0)

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(["--store-dir", str(store_dir), "alias", "list"])
            self.assertEqual(status, 0)
            self.assertIn("codex1 -> codex:a@example.com -- fake-codex", out.getvalue())

            calls = []

            def fake_call(command, *, env):
                calls.append(command)
                isolated_home = Path(env["CODEX_HOME"])
                self.assertNotEqual(isolated_home, codex_home)
                isolated_auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(isolated_auth["email"], "a@example.com")
                current = store.current_profile(provider)
                self.assertIsNotNone(current)
                self.assertEqual(current.name, "b@example.com")
                return 0

            with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store_dir),
                        "--codex-home",
                        str(codex_home),
                        "alias",
                        "run",
                        "codex1",
                        "--",
                        "-C",
                        "/tmp/workspace",
                    ]
                )

            self.assertEqual(status, 0)
            self.assertEqual(calls, [["fake-codex", "-C", "/tmp/workspace"]])
            self.assertEqual(store.current_profile(provider).name, "b@example.com")

    def test_program_name_alias_dispatches_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(store_dir)

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

            with store.lock():
                store.set_alias(provider, "codex1", "a@example.com", ["fake-codex"])

            calls = []

            def fake_call(command, *, env):
                calls.append(command)
                isolated_home = Path(env["CODEX_HOME"])
                self.assertNotEqual(isolated_home, codex_home)
                isolated_auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(isolated_auth["email"], "a@example.com")
                current = store.current_profile(provider)
                self.assertIsNotNone(current)
                self.assertEqual(current.name, "b@example.com")
                return 0

            with mock.patch.dict(
                os.environ,
                {
                    "AI_AUTH_SWITCH_HOME": str(store_dir),
                    "CODEX_HOME": str(codex_home),
                },
            ):
                with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                    with mock.patch.object(
                        AuthStore,
                        "lock",
                        side_effect=AssertionError("program aliases must not take the global lock"),
                    ):
                        status = cli_main(
                            ["-C", "/tmp/workspace"],
                            program_name="codex1",
                        )

            self.assertEqual(status, 0)
            self.assertEqual(calls, [["fake-codex", "-C", "/tmp/workspace"]])
            self.assertEqual(store.current_profile(provider).name, "b@example.com")

    def test_profile_runs_with_different_accounts_concurrently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            (codex_home / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
            (codex_home / "sessions").mkdir()
            sqlite_home = root / "sqlite"
            sqlite_home.mkdir()

            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(root / "store")
            for name in ("a@example.com", "b@example.com"):
                path = store.profile_path(provider, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"email": name}), encoding="utf-8")

            barrier = threading.Barrier(2, timeout=3)
            seen: dict[str, str] = {}
            errors: list[BaseException] = []

            def fake_call(command, *, env):
                isolated_home = Path(env["CODEX_HOME"])
                auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                seen[command[-1]] = auth["email"]
                self.assertTrue((isolated_home / "config.toml").is_symlink())
                self.assertEqual(
                    (isolated_home / "config.toml").resolve(),
                    (codex_home / "config.toml").resolve(),
                )
                self.assertTrue((isolated_home / "sessions").is_symlink())
                self.assertEqual(env["CODEX_SQLITE_HOME"], str(sqlite_home))
                barrier.wait()
                return 0

            def run(name: str) -> None:
                try:
                    status = run_with_profile(
                        store,
                        provider,
                        name,
                        ["fake-codex", name],
                    )
                    self.assertEqual(status, 0)
                except BaseException as exc:
                    errors.append(exc)

            with mock.patch.dict(
                os.environ,
                {"CODEX_SQLITE_HOME": str(sqlite_home)},
            ):
                with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                    threads = [
                        threading.Thread(target=run, args=(name,))
                        for name in ("a@example.com", "b@example.com")
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)

            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(
                seen,
                {
                    "a@example.com": "a@example.com",
                    "b@example.com": "b@example.com",
                },
            )

    def test_isolated_run_syncs_refreshed_auth_and_replaced_shared_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            config = codex_home / "config.toml"
            config.write_text("model = 'before'\n", encoding="utf-8")
            active = codex_home / "auth.json"
            active.write_text(json.dumps({"email": "default@example.com"}), encoding="utf-8")

            provider = CodexProvider(
                codex_home=codex_home,
                login_command=["fake-codex"],
            )
            store = AuthStore(root / "store")
            profile = store.profile_path(provider, "selected@example.com")
            profile.parent.mkdir(parents=True, exist_ok=True)
            profile.write_text(
                json.dumps({"email": "selected@example.com"}),
                encoding="utf-8",
            )

            def fake_call(command, *, env):
                isolated_home = Path(env["CODEX_HOME"])
                self.assertEqual(env["CODEX_SQLITE_HOME"], str(codex_home))
                isolated_auth = isolated_home / "auth.json"
                isolated_auth.unlink()
                isolated_auth.write_text(
                    json.dumps(
                        {
                            "email": "selected@example.com",
                            "refreshed": True,
                        }
                    ),
                    encoding="utf-8",
                )

                isolated_config = isolated_home / "config.toml"
                isolated_config.unlink()
                isolated_config.write_text("model = 'after'\n", encoding="utf-8")
                return 0

            with mock.patch.dict(os.environ, {"CODEX_SQLITE_HOME": ""}):
                with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                    status = run_with_profile(
                        store,
                        provider,
                        "selected@example.com",
                        ["fake-codex"],
                    )

            self.assertEqual(status, 0)
            refreshed = json.loads(profile.read_text(encoding="utf-8"))
            self.assertTrue(refreshed["refreshed"])
            self.assertEqual(config.read_text(encoding="utf-8"), "model = 'after'\n")
            self.assertEqual(
                json.loads(active.read_text(encoding="utf-8"))["email"],
                "default@example.com",
            )

    def test_cli_alias_install_creates_executable_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            bin_dir = root / "bin"
            codex_home.mkdir()
            provider = CodexProvider(codex_home=codex_home)
            store = AuthStore(store_dir)

            active = codex_home / "auth.json"
            active.write_text(
                json.dumps({"auth_mode": "chatgpt", "email": "a@example.com"}),
                encoding="utf-8",
            )
            with store.lock():
                store.save_current(provider, "a@example.com")
                store.set_alias(provider, "codex1", "a@example.com", ["fake-codex"])

            target = root / "ai-auth-switch-target"
            target.write_text("#!/bin/sh\n", encoding="utf-8")
            target.chmod(0o700)

            with contextlib.redirect_stdout(io.StringIO()):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store_dir),
                        "alias",
                        "install",
                        "codex1",
                        "--bin-dir",
                        str(bin_dir),
                        "--target",
                        str(target),
                    ]
                )

            link = bin_dir / "codex1"
            self.assertEqual(status, 0)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), target.resolve())

    def test_cli_auth_login_syncs_hermes_without_second_login(self) -> None:
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
            self.assertFalse(kwargs["hermes_login"])
            self.assertEqual(kwargs["hermes_profile_name"], "person@example.com")
            self.assertEqual(kwargs["store_dir"], store_dir)
            self.assertIn("saved codex login as person@example.com", out.getvalue())


    def test_sync_codex_dependents_updates_hermes_codex_cli_access_token(self) -> None:
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

            hermes_agent = root / "hermes-agent"
            python = hermes_agent / "venv" / "bin" / "python"
            auth_module = hermes_agent / "hermes_cli" / "auth.py"
            python.parent.mkdir(parents=True)
            auth_module.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            auth_module.write_text("# fake hermes auth module\n", encoding="utf-8")

            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                helper = command[2]
                self.assertNotIn("codex_app_server", helper)
                self.assertIn('set_runtime(config, "auto")', helper)
                self.assertIn("access_token", helper)
                self.assertNotIn("_codex_device_code_login", helper)
                env = kwargs["env"]
                self.assertEqual(env["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"], "person@example.com")
                self.assertEqual(env["AI_AUTH_SWITCH_CODEX_AUTH_PATH"], str(codex_home / "auth.json"))
                self.assertEqual(
                    env["AI_AUTH_SWITCH_HERMES_CLI_ACCESS_SOURCE"],
                    HERMES_CODEX_CLI_ACCESS_SOURCE,
                )
                self.assertEqual(
                    env["AI_AUTH_SWITCH_HERMES_LEGACY_BRIDGE_SOURCE"],
                    HERMES_CODEX_BRIDGE_SOURCE,
                )
                return mock.Mock(
                    returncode=0,
                    stdout='{"status":"synced","config":"/tmp/hermes/config.yaml","runtime":"auto"}\n',
                    stderr="",
                )

            provider = CodexProvider(codex_home=codex_home)
            with mock.patch("ai_auth_switch.sync.subprocess.run", fake_run):
                results = sync_codex_dependents(
                    provider,
                    sync_openclaw=False,
                    hermes_login=True,
                    restart_hermes=False,
                    hermes_agent_dir=hermes_agent,
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual([result.target for result in results], ["hermes"])
            self.assertTrue(results[0].ok)
            self.assertIn(
                "Codex CLI access token active for person@example.com",
                results[0].message,
            )
            self.assertIn("runtime auto", results[0].message)

    def test_sync_codex_dependents_restarts_active_hermes_gateway(self) -> None:
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

            hermes_agent = root / "hermes-agent"
            python = hermes_agent / "venv" / "bin" / "python"
            auth_module = hermes_agent / "hermes_cli" / "auth.py"
            python.parent.mkdir(parents=True)
            auth_module.parent.mkdir(parents=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o700)
            auth_module.write_text("# fake hermes auth module\n", encoding="utf-8")

            calls = []

            def fake_run(command, **kwargs):
                calls.append(command)
                if len(command) >= 3 and command[1] == "-c":
                    return mock.Mock(
                        returncode=0,
                        stdout=(
                            '{"status":"synced","config":"/tmp/hermes/config.yaml",'
                            '"runtime":"auto"}\n'
                        ),
                        stderr="",
                    )
                if len(command) >= 3 and command[2] in {
                    "import-environment",
                    "unset-environment",
                }:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-3:] == ["is-active", "--quiet", "hermes-gateway.service"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                if command[-2:] == ["restart", "hermes-gateway.service"]:
                    return mock.Mock(returncode=0, stdout="", stderr="")
                raise AssertionError(f"unexpected command: {command}")

            provider = CodexProvider(codex_home=codex_home)
            with mock.patch("ai_auth_switch.sync.shutil.which", return_value="/bin/systemctl"):
                with mock.patch.dict(
                    os.environ,
                    {"http_proxy": "http://127.0.0.1:7897"},
                    clear=True,
                ):
                    with mock.patch("ai_auth_switch.sync.subprocess.run", fake_run):
                        results = sync_codex_dependents(
                            provider,
                            sync_openclaw=False,
                            hermes_agent_dir=hermes_agent,
                        )

            self.assertEqual(
                [result.target for result in results],
                ["hermes", "hermes-gateway"],
            )
            self.assertTrue(all(result.ok for result in results))
            self.assertIn(
                ["/bin/systemctl", "--user", "import-environment", "http_proxy"],
                calls,
            )
            self.assertEqual(
                calls[-1],
                ["/bin/systemctl", "--user", "restart", "hermes-gateway.service"],
            )

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

    def test_sync_codex_dependents_updates_openclaw_sqlite_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            access_token = fake_jwt(
                {
                    "exp": 4102444800,
                    "https://api.openai.com/profile": {"email": "person@example.com"},
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acc_access",
                        "chatgpt_plan_type": "pro",
                    },
                }
            )
            id_token = fake_jwt(
                {
                    "email": "person@example.com",
                    "https://api.openai.com/auth": {
                        "chatgpt_account_id": "acc_id",
                        "chatgpt_plan_type": "plus",
                    },
                }
            )
            (codex_home / "auth.json").write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "access_token": access_token,
                            "refresh_token": "rt_test",
                            "id_token": id_token,
                        },
                    }
                ),
                encoding="utf-8",
            )

            openclaw = root / ".openclaw"
            agent = openclaw / "agents" / "main" / "agent"
            agent.mkdir(parents=True)
            sqlite_path = agent / "openclaw-agent.sqlite"
            with sqlite3.connect(sqlite_path) as conn:
                conn.execute(
                    "create table auth_profile_store("
                    "store_key text primary key, store_json text, updated_at integer)"
                )
                conn.execute(
                    "create table auth_profile_state("
                    "state_key text primary key, state_json text, updated_at integer)"
                )
                conn.execute(
                    "insert into auth_profile_store(store_key, store_json, updated_at) "
                    "values('primary', ?, 1)",
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "profiles": {
                                    "openai:old@example.com": {
                                        "type": "oauth",
                                        "provider": "openai",
                                    },
                                    "vllm:default": {"type": "api_key", "provider": "vllm"},
                                },
                            }
                        ),
                    ),
                )
                conn.execute(
                    "insert into auth_profile_state(state_key, state_json, updated_at) "
                    "values('primary', ?, 1)",
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "order": {"openai-codex": ["openai-codex:default"]},
                                "lastGood": {},
                                "usageStats": {OPENAI_DEFAULT_PROFILE: {"errorCount": 3}},
                            }
                        ),
                    ),
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
            self.assertIn("OpenClaw SQLite openai:default", results[0].message)

            with sqlite3.connect(sqlite_path) as conn:
                store = json.loads(
                    conn.execute(
                        "select store_json from auth_profile_store where store_key='primary'"
                    ).fetchone()[0]
                )
                state = json.loads(
                    conn.execute(
                        "select state_json from auth_profile_state where state_key='primary'"
                    ).fetchone()[0]
                )

            profile = store["profiles"][OPENAI_DEFAULT_PROFILE]
            self.assertEqual(profile["type"], "oauth")
            self.assertEqual(profile["provider"], "openai")
            self.assertEqual(profile["access"], access_token)
            self.assertEqual(profile["refresh"], "rt_test")
            self.assertEqual(profile["idToken"], id_token)
            self.assertEqual(profile["email"], "person@example.com")
            self.assertEqual(profile["accountId"], "acc_id")
            self.assertEqual(profile["chatgptPlanType"], "plus")
            self.assertEqual(profile["expires"], 4102444800 * 1000)
            self.assertEqual(state["order"]["openai"], [OPENAI_DEFAULT_PROFILE])
            self.assertEqual(state["lastGood"]["openai"], OPENAI_DEFAULT_PROFILE)
            self.assertNotIn(OPENAI_DEFAULT_PROFILE, state["usageStats"])

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
                    "AI_AUTH_SWITCH_HOME": str(root / "store"),
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
