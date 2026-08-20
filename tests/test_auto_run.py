from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from ai_auth_switch.auto_run import (
    AutoRunConfig,
    AutoRunSelection,
    AutoRunState,
    RunLease,
    acquire_auto_run_profile,
    auto_run_state_path,
    choose_auto_run_profile,
)
from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, UsageWindow


def usage(remaining: float, *, resets_at: int = 2_000_000_000) -> AccountUsage:
    return AccountUsage(
        plan_type="pro",
        secondary=UsageWindow(
            used_percent=100 - remaining,
            window_seconds=604_800,
            resets_at=resets_at,
        ),
    )


def save_profiles(
    store: AuthStore,
    provider: CodexProvider,
    names: tuple[str, ...],
) -> None:
    provider.active_auth_path.parent.mkdir(parents=True, exist_ok=True)
    for name in names:
        store.write_profile_content(
            provider,
            name,
            json.dumps({"auth_mode": "chatgpt", "email": name}),
        )


class AutoRunSelectionTests(unittest.TestCase):
    def test_active_leases_reduce_effective_capacity(self) -> None:
        profiles = [
            ProfileInfo("large", Path("large")),
            ProfileInfo("small", Path("small")),
        ]
        usages = {"large": usage(90), "small": usage(40)}
        one_large_run = AutoRunState(
            leases=[RunLease("one", 10, "large", 1)],
            selected_at={},
        )
        self.assertEqual(
            choose_auto_run_profile(profiles, usages, one_large_run).profile,
            "large",
        )
        two_large_runs = AutoRunState(
            leases=[
                RunLease("one", 10, "large", 1),
                RunLease("two", 11, "large", 2),
            ],
            selected_at={},
        )
        selected = choose_auto_run_profile(profiles, usages, two_large_runs)
        self.assertEqual(selected.profile, "small")
        self.assertEqual(selected.effective_remaining, 40)

    def test_expired_or_exhausted_profiles_are_excluded(self) -> None:
        profiles = [
            ProfileInfo("expired", Path("expired")),
            ProfileInfo("empty", Path("empty")),
            ProfileInfo("ready", Path("ready")),
        ]
        selected = choose_auto_run_profile(
            profiles,
            {
                "expired": AccountUsage(error="authentication expired"),
                "empty": usage(0),
                "ready": usage(25),
            },
            AutoRunState.empty(),
        )
        self.assertEqual(selected.profile, "ready")

    def test_free_profiles_are_never_automatically_selected(self) -> None:
        profiles = [
            ProfileInfo("free", Path("free")),
            ProfileInfo("paid", Path("paid")),
        ]
        selected = choose_auto_run_profile(
            profiles,
            {
                "free": AccountUsage(
                    plan_type="free",
                    secondary=UsageWindow(used_percent=0),
                ),
                "paid": usage(10),
            },
            AutoRunState.empty(),
        )
        self.assertEqual(selected.profile, "paid")

    def test_lease_is_visible_during_run_and_released_afterward(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = CodexProvider(root / ".codex", ["fake-codex"])
            store = AuthStore(root / "store")
            save_profiles(store, provider, ("a", "b"))

            def fetcher(_profiles, **_kwargs):
                return {"a": usage(20), "b": usage(80)}

            with acquire_auto_run_profile(
                store,
                provider,
                AutoRunConfig(),
                fetcher=fetcher,
                pid=1234,
                now=500,
                pid_is_alive=lambda _pid: True,
            ) as selection:
                self.assertEqual(selection.profile, "b")
                state = json.loads(
                    auto_run_state_path(store, provider).read_text(encoding="utf-8")
                )
                self.assertEqual(len(state["leases"]), 1)
                self.assertEqual(state["leases"][0]["profile"], "b")
            state = json.loads(
                auto_run_state_path(store, provider).read_text(encoding="utf-8")
            )
            self.assertEqual(state["leases"], [])


class AutoRunCliTests(unittest.TestCase):
    def test_run_codex_auto_selects_profile_and_preserves_child_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            provider = CodexProvider(codex_home, ["fake-codex"])
            save_profiles(AuthStore(store_dir), provider, ("a", "b"))
            calls = []

            @contextmanager
            def acquire(_store, _provider, _config):
                yield AutoRunSelection(
                    profile="b",
                    remaining_percent=80,
                    effective_remaining=80,
                    active_leases=0,
                    resets_at=2_000_000_000,
                    lease_id="lease",
                )

            def fake_call(command, *, env):
                calls.append(command)
                selected = json.loads(
                    (Path(env["CODEX_HOME"]) / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(selected["email"], "b")
                return 0

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            stderr = io.StringIO()
            with (
                mock.patch(
                    "ai_auth_switch.auto_run.acquire_auto_run_profile",
                    acquire,
                ),
                mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call),
                contextlib.redirect_stderr(stderr),
            ):
                status = cli_main(
                    base
                    + [
                        "run",
                        "codex",
                        "--auto",
                        "--",
                        "codex",
                        "-C",
                        str(root),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertEqual(calls, [["codex", "-C", str(root)]])
            self.assertIn("auto-selected codex profile b", stderr.getvalue())

    def test_auto_without_child_command_runs_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            store_dir = root / "store"
            provider = CodexProvider(codex_home, ["fake-codex"])
            save_profiles(AuthStore(store_dir), provider, ("a",))
            calls = []

            @contextmanager
            def acquire(_store, _provider, _config):
                yield AutoRunSelection("a", 50, 50, 0, None, "lease")

            def fake_call(command, *, env):
                calls.append(command)
                return 0

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with (
                mock.patch(
                    "ai_auth_switch.auto_run.acquire_auto_run_profile",
                    acquire,
                ),
                mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                status = cli_main(base + ["run", "codex", "--auto"])
            self.assertEqual(status, 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 1)


if __name__ == "__main__":
    unittest.main()
