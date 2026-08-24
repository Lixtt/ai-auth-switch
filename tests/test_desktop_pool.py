from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

import tomllib

from ai_auth_switch.desktop_pool import (
    POOL_DESKTOP_MARKER,
    DesktopPoolPaths,
    disable_desktop_pool,
    install_desktop_pool,
)
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore


class DesktopPoolTests(unittest.TestCase):
    def test_install_and_disable_are_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            provider.active_auth_path.parent.mkdir(parents=True)
            config = provider.active_auth_path.parent / "config.toml"
            config.write_text('model_provider = "custom"\n', encoding="utf-8")
            paths = DesktopPoolPaths(
                launcher=root / "applications" / "chatgpt.desktop",
                launcher_backup=root / "desktop-pool" / "launcher.backup",
                wrapper=root / "desktop-pool" / "launcher",
                service=root / "systemd" / "ai-auth-switch-desktop-pool.service",
                auto_service_state=root / "desktop-pool" / "auto-service-state",
            )
            paths.launcher.parent.mkdir(parents=True)
            original = "[Desktop Entry]\nExec=/usr/bin/chatgpt %U\nType=Application\n"
            paths.launcher.write_text(original, encoding="utf-8")
            calls: list[list[str]] = []

            def runner(command, **_kwargs):
                calls.append([str(item) for item in command])
                return subprocess.CompletedProcess(command, 0, "", "")

            installed = install_desktop_pool(
                store,
                provider,
                paths=paths,
                runner=runner,
            )
            self.assertIn(
                POOL_DESKTOP_MARKER, paths.launcher.read_text(encoding="utf-8")
            )
            self.assertEqual(paths.wrapper.stat().st_mode & 0o777, 0o700)
            self.assertIn(
                "AI_AUTH_SWITCH_POOL_TOKEN", paths.wrapper.read_text(encoding="utf-8")
            )
            self.assertIn("--store-dir", paths.service.read_text(encoding="utf-8"))
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(parsed["model_provider"], "ai-auth-switch-pool")
            self.assertIsNotNone(installed.config_backup)
            self.assertTrue(any("enable" in call for call in calls))
            self.assertTrue(
                any("ai-auth-switch-desktop-auto.service" in call for call in calls)
            )

            disable_desktop_pool(store, provider, paths=paths, runner=runner)
            self.assertEqual(paths.launcher.read_text(encoding="utf-8"), original)
            self.assertFalse(paths.launcher_backup.exists())
            self.assertEqual(
                tomllib.loads(config.read_text(encoding="utf-8"))["model_provider"],
                "custom",
            )
            self.assertTrue(any("disable" in call for call in calls))
            self.assertTrue(
                any(
                    "enable" in call and "ai-auth-switch-desktop-auto.service" in call
                    for call in calls
                )
            )

    def test_failed_service_enable_rolls_back_launcher_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            provider.active_auth_path.parent.mkdir(parents=True)
            config = provider.active_auth_path.parent / "config.toml"
            original_config = 'model_provider = "custom"\n'
            config.write_text(original_config, encoding="utf-8")
            paths = DesktopPoolPaths(
                launcher=root / "applications" / "chatgpt.desktop",
                launcher_backup=root / "desktop-pool" / "launcher.backup",
                wrapper=root / "desktop-pool" / "launcher",
                service=root / "systemd" / "ai-auth-switch-desktop-pool.service",
                auto_service_state=root / "desktop-pool" / "auto-service-state",
            )
            paths.launcher.parent.mkdir(parents=True)
            original_launcher = "[Desktop Entry]\nExec=/usr/bin/chatgpt %U\n"
            paths.launcher.write_text(original_launcher, encoding="utf-8")

            def runner(command, **_kwargs):
                failed = "enable" in command
                return subprocess.CompletedProcess(
                    command,
                    1 if failed else 0,
                    "",
                    "failed" if failed else "",
                )

            with self.assertRaisesRegex(AiAuthSwitchError, "systemctl"):
                install_desktop_pool(
                    store,
                    provider,
                    paths=paths,
                    runner=runner,
                )
            self.assertEqual(
                paths.launcher.read_text(encoding="utf-8"), original_launcher
            )
            self.assertEqual(config.read_text(encoding="utf-8"), original_config)


if __name__ == "__main__":
    unittest.main()
