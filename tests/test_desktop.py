from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ai_auth_switch.desktop import (
    DESKTOP_DAEMON_ENV,
    MANAGED_LAUNCHER_MARKER,
    DesktopAutoConfig,
    DesktopAutoState,
    DesktopProcessState,
    active_desktop_threads,
    choose_desktop_profile,
    desktop_auto_cycle,
    desktop_paths,
    detect_desktop_processes,
    disable_desktop_auto,
    install_desktop_auto,
    parse_desktop_processes,
    rotate_desktop_account,
)
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.usage import AccountUsage, UsageWindow


def usage(remaining: float, *, resets_at: int = 2_000_000_000) -> AccountUsage:
    return AccountUsage(
        plan_type="pro",
        secondary=UsageWindow(
            used_percent=100.0 - remaining,
            window_seconds=604_800,
            resets_at=resets_at,
        ),
    )


def save_profiles(
    store: AuthStore,
    provider: CodexProvider,
    names: tuple[str, ...],
    *,
    active: str,
) -> None:
    provider.active_auth_path.parent.mkdir(parents=True, exist_ok=True)
    for name in names:
        store.write_profile_content(
            provider,
            name,
            json.dumps({"auth_mode": "chatgpt", "email": name}),
        )
    with store.lock():
        store.activate(provider, active, backup_existing=False)


class DesktopProcessTests(unittest.TestCase):
    def test_detects_managed_unmanaged_and_stopped_desktop(self) -> None:
        app = "100 1 /usr/lib/chatgpt/ChatGPT\n"
        unmanaged = app + (
            "101 100 /usr/lib/chatgpt/resources/codex "
            "-c features.code_mode_host=true app-server\n"
        )
        managed = app + ("102 100 /usr/lib/chatgpt/resources/codex app-server proxy\n")
        self.assertEqual(parse_desktop_processes(unmanaged).mode, "unmanaged")
        self.assertEqual(parse_desktop_processes(managed).mode, "managed")
        self.assertEqual(parse_desktop_processes("").mode, "stopped")

    def test_running_daemon_identifies_direct_desktop_socket_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            socket_path = codex_home / "app-server-control" / "app-server-control.sock"
            socket_path.parent.mkdir(parents=True)
            socket_path.touch()

            def runner(command, **_kwargs):
                stdout = "100 1 /usr/lib/chatgpt/ChatGPT\n"
                return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

            state = detect_desktop_processes(
                codex_home=codex_home,
                runner=runner,
            )
            self.assertEqual(state.mode, "managed")

    def test_app_server_thread_query_finds_only_active_threads(self) -> None:
        response = {
            "data": [
                {"id": "busy", "status": {"type": "active"}},
                {"id": "idle", "status": {"type": "idle"}},
            ]
        }
        with mock.patch(
            "ai_auth_switch.desktop._app_server_request",
            return_value=response,
        ) as request:
            active = active_desktop_threads(Path("/fake/codex"))
        self.assertEqual(active, ["busy"])
        self.assertEqual(request.call_args.args[1], "thread/list")


class DesktopSelectionTests(unittest.TestCase):
    def test_selects_highest_capacity_then_least_recently_used(self) -> None:
        profiles = [
            type("Profile", (), {"name": name})()
            for name in ("current", "used-recently", "never-used")
        ]
        usages = {
            "current": usage(2),
            "used-recently": usage(80),
            "never-used": usage(80),
        }
        state = DesktopAutoState(selected_at={"used-recently": 100.0})
        decision = choose_desktop_profile(
            profiles,
            usages,
            "current",
            DesktopAutoConfig(),
            state,
        )
        self.assertEqual(decision.profile, "never-used")
        self.assertEqual(decision.current_remaining, 2)
        self.assertEqual(decision.selected_remaining, 80)

    def test_does_not_switch_healthy_current_account_without_force(self) -> None:
        profiles = [
            type("Profile", (), {"name": name})() for name in ("current", "other")
        ]
        usages = {"current": usage(50), "other": usage(90)}
        decision = choose_desktop_profile(
            profiles,
            usages,
            "current",
            DesktopAutoConfig(switch_below_remaining=10),
            DesktopAutoState(),
        )
        self.assertIsNone(decision.profile)
        forced = choose_desktop_profile(
            profiles,
            usages,
            "current",
            DesktopAutoConfig(),
            DesktopAutoState(),
            force=True,
        )
        self.assertEqual(forced.profile, "other")

    def test_excludes_expired_and_exhausted_accounts(self) -> None:
        profiles = [
            type("Profile", (), {"name": name})()
            for name in ("current", "expired", "empty")
        ]
        decision = choose_desktop_profile(
            profiles,
            {
                "current": usage(1),
                "expired": AccountUsage(error="authentication expired"),
                "empty": usage(0),
            },
            "current",
            DesktopAutoConfig(),
            DesktopAutoState(),
        )
        self.assertIsNone(decision.profile)

    def test_desktop_rotation_excludes_free_accounts(self) -> None:
        profiles = [
            type("Profile", (), {"name": name})()
            for name in ("current", "free", "paid")
        ]
        decision = choose_desktop_profile(
            profiles,
            {
                "current": usage(1),
                "free": AccountUsage(
                    plan_type="free",
                    secondary=UsageWindow(used_percent=0),
                ),
                "paid": usage(20),
            },
            "current",
            DesktopAutoConfig(),
            DesktopAutoState(),
        )
        self.assertEqual(decision.profile, "paid")


class DesktopRotationTests(unittest.TestCase):
    def test_stopped_desktop_can_rotate_without_daemon_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            save_profiles(
                store,
                provider,
                ("current@example.com", "other@example.com"),
                active="current@example.com",
            )
            state = DesktopAutoState()

            def fetcher(_profiles, **_kwargs):
                return {
                    "current@example.com": usage(2),
                    "other@example.com": usage(80),
                }

            result = rotate_desktop_account(
                store,
                provider,
                DesktopAutoConfig(),
                state,
                process_state=DesktopProcessState("stopped"),
                codex_bin=Path("/fake/codex"),
                usage_fetcher=fetcher,
                daemon_runner=lambda *_args, **_kwargs: self.fail(
                    "daemon must not restart"
                ),
                now=1234,
            )
            self.assertTrue(result.changed)
            self.assertEqual(result.profile, "other@example.com")
            self.assertEqual(store.current_profile(provider).name, "other@example.com")
            self.assertEqual(state.last_switch_at, 1234)

    def test_managed_desktop_refuses_to_switch_an_active_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            save_profiles(store, provider, ("a", "b"), active="a")
            with self.assertRaisesRegex(AiAuthSwitchError, "active"):
                rotate_desktop_account(
                    store,
                    provider,
                    DesktopAutoConfig(),
                    DesktopAutoState(),
                    process_state=DesktopProcessState("managed"),
                    codex_bin=Path("/fake/codex"),
                    thread_reader=lambda _binary: ["thread-1"],
                )
            self.assertEqual(store.current_profile(provider).name, "a")

    def test_failed_daemon_restart_rolls_back_the_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            save_profiles(store, provider, ("a", "b"), active="a")
            restarts = []

            def fetcher(_profiles, **_kwargs):
                return {"a": usage(1), "b": usage(90)}

            def restart(_binary, _action, **_kwargs):
                restarts.append(True)
                if len(restarts) == 1:
                    raise AiAuthSwitchError("restart failed")

            with self.assertRaisesRegex(AiAuthSwitchError, "restart failed"):
                rotate_desktop_account(
                    store,
                    provider,
                    DesktopAutoConfig(),
                    DesktopAutoState(),
                    process_state=DesktopProcessState("managed"),
                    codex_bin=Path("/fake/codex"),
                    thread_reader=lambda _binary: [],
                    usage_fetcher=fetcher,
                    daemon_runner=restart,
                )
            self.assertEqual(len(restarts), 2)
            self.assertEqual(store.current_profile(provider).name, "a")

    def test_auto_cycle_waits_for_grace_then_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            save_profiles(store, provider, ("a", "b"), active="a")
            state = DesktopAutoState()
            config = DesktopAutoConfig(idle_seconds=60, cooldown_seconds=0)

            def fetcher(_profiles, **_kwargs):
                return {"a": usage(1), "b": usage(90)}

            common = {
                "process_detector": lambda: DesktopProcessState("managed"),
                "thread_reader": lambda _binary: [],
                "codex_bin": Path("/fake/codex"),
                "usage_fetcher": fetcher,
                "daemon_runner": lambda *_args, **_kwargs: None,
            }
            waiting = desktop_auto_cycle(
                store, provider, config, state, now=100, **common
            )
            self.assertFalse(waiting.changed)
            self.assertEqual(state.idle_since, 100)
            switched = desktop_auto_cycle(
                store, provider, config, state, now=161, **common
            )
            self.assertTrue(switched.changed)
            self.assertEqual(store.current_profile(provider).name, "b")


class DesktopInstallTests(unittest.TestCase):
    def test_install_and_disable_restore_existing_user_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            paths = desktop_paths(
                store,
                home=root,
                config_home=root / "config",
                data_home=root / "data",
                system_launcher=root / "system-chatgpt.desktop",
            )
            original = (
                "[Desktop Entry]\nName=Custom ChatGPT\nExec=chatgpt --custom %U\n"
            )
            paths.launcher.parent.mkdir(parents=True)
            paths.launcher.write_text(original, encoding="utf-8")

            install_desktop_auto(
                store,
                provider,
                Path("/usr/bin/ais"),
                DesktopAutoConfig(idle_seconds=30),
                paths=paths,
                enable=False,
                supports_daemon=True,
            )
            launcher = paths.launcher.read_text(encoding="utf-8")
            self.assertIn(f"Exec=env {DESKTOP_DAEMON_ENV}=1", launcher)
            self.assertIn(MANAGED_LAUNCHER_MARKER, launcher)
            self.assertEqual(json.loads(paths.config.read_text())["idle_seconds"], 30)
            self.assertIn('desktop" "auto" "run', paths.service.read_text())

            def systemctl(command, **_kwargs):
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            disable_desktop_auto(store, paths=paths, runner=systemctl)
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), original)
            self.assertFalse(paths.service.exists())
            self.assertFalse(paths.launcher_backup.exists())

    def test_enabled_install_seeds_and_starts_managed_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            paths = desktop_paths(
                store,
                home=root,
                config_home=root / "config",
                data_home=root / "data",
                system_launcher=root / "system-chatgpt.desktop",
            )
            paths.system_launcher.write_text(
                "[Desktop Entry]\nName=ChatGPT\nExec=chatgpt %U\n",
                encoding="utf-8",
            )
            binary = root / "desktop-codex"
            binary.write_text("binary", encoding="utf-8")
            binary.chmod(0o700)
            daemon_calls = []
            systemctl_calls = []

            def daemon_runner(*args, **kwargs):
                daemon_calls.append((args, kwargs))

            def systemctl(command, **_kwargs):
                systemctl_calls.append(command)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.dict(
                os.environ,
                {"AI_AUTH_SWITCH_DESKTOP_CODEX_BIN": str(binary)},
            ):
                install_desktop_auto(
                    store,
                    provider,
                    Path("/usr/bin/ais"),
                    DesktopAutoConfig(),
                    paths=paths,
                    enable=True,
                    supports_daemon=True,
                    runner=systemctl,
                    daemon_runner=daemon_runner,
                )
            managed = (
                provider.active_auth_path.parent
                / "packages"
                / "standalone"
                / "current"
                / "codex"
            )
            self.assertTrue(managed.is_symlink())
            self.assertEqual(managed.resolve(), binary.resolve())
            self.assertEqual(daemon_calls[0][0], (binary, "start"))
            self.assertEqual(
                daemon_calls[0][1]["codex_home"],
                provider.active_auth_path.parent,
            )
            self.assertTrue(
                any(
                    call[-3:]
                    == ["enable", "--now", "ai-auth-switch-desktop-auto.service"]
                    for call in systemctl_calls
                )
            )


if __name__ == "__main__":
    unittest.main()
