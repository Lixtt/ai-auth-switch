from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore, read_binding, resolve_binding


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


class DefaultProfileTests(unittest.TestCase):
    def test_auth_default_set_show_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")
            _save_profile(store, provider, "b@example.com")

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    cli_main(base + ["auth", "default", "codex", "b@example.com"]),
                    0,
                )
            self.assertIn("default codex profile -> b@example.com", out.getvalue())
            self.assertEqual(store.get_default(provider), "b@example.com")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli_main(base + ["auth", "default", "codex"]), 0)
            self.assertIn("default profile -> b@example.com", out.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(base + ["auth", "default", "codex", "--clear"]),
                    0,
                )
            self.assertIsNone(store.get_default(provider))

    def test_auth_default_rejects_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(
                    base + ["auth", "default", "codex", "nobody@example.com"]
                )
            self.assertEqual(status, 2)
            self.assertIn("profile not found", err.getvalue())


class BindingTests(unittest.TestCase):
    def test_auth_bind_set_show_clear_and_ancestor_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")

            project = root / "project" / "sub"
            project.mkdir(parents=True)
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    cli_main(
                        base
                        + [
                            "auth",
                            "bind",
                            "codex",
                            "a@example.com",
                            "--dir",
                            str(project),
                        ]
                    ),
                    0,
                )
            self.assertIn("bound codex profile a@example.com to", out.getvalue())
            self.assertEqual(read_binding(project).get("codex"), "a@example.com")
            # Binding resolves from an ancestor directory too.
            deeper = project / "deeper"
            deeper.mkdir()
            self.assertEqual(resolve_binding("codex", deeper), "a@example.com")

            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    cli_main(
                        base + ["auth", "bind", "codex", "--dir", str(project)]
                    ),
                    0,
                )
            self.assertIn("bound profile -> a@example.com", out.getvalue())

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        base + ["auth", "bind", "codex", "--clear", "--dir", str(project)]
                    ),
                    0,
                )
            self.assertFalse((project / ".ai-auth-switch.json").exists())
            self.assertIsNone(resolve_binding("codex", deeper))

    def test_auth_bind_rejects_unknown_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(
                    base + ["auth", "bind", "codex", "nobody@example.com"]
                )
            self.assertEqual(status, 2)
            self.assertIn("profile not found", err.getvalue())


class RunFallbackTests(unittest.TestCase):
    def test_run_without_profile_or_default_errors_helpfully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(base + ["run", "codex"])
            self.assertEqual(status, 2)
            self.assertIn("no profile specified", err.getvalue())
            self.assertIn("auth default codex", err.getvalue())
            self.assertIn("auth bind codex", err.getvalue())

    def test_run_falls_back_to_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")
            _save_profile(store, provider, "b@example.com")
            with store.lock():
                store.set_default(provider, "b@example.com")

            calls = []

            def fake_call(command, *, env):
                calls.append(command)
                isolated_home = Path(env["CODEX_HOME"])
                isolated_auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(isolated_auth["email"], "b@example.com")
                return 0

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                status = cli_main(base + ["run", "codex"])
            self.assertEqual(status, 0)
            # The CLI builds its own provider, so the command is the resolved
            # Codex executable rather than the test's fake-codex.
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 1)

    def test_run_with_explicit_profile_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")
            _save_profile(store, provider, "b@example.com")
            with store.lock():
                store.set_default(provider, "b@example.com")

            calls = []

            def fake_call(command, *, env):
                calls.append(command)
                isolated_home = Path(env["CODEX_HOME"])
                isolated_auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(isolated_auth["email"], "a@example.com")
                return 0

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                status = cli_main(
                    base + ["run", "codex", "a@example.com", "--", "-C", "/tmp/w"]
                )
            self.assertEqual(status, 0)
            # The explicit profile wins even though a default is set.
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 2)
            self.assertEqual(calls[0][-2:], ["-C", "/tmp/w"])

    def test_run_falls_back_to_directory_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")

            project = root / "project"
            project.mkdir()
            base = [
                "--store-dir",
                str(store_dir),
                "--codex-home",
                str(codex_home),
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        base + ["auth", "bind", "codex", "a@example.com", "--dir", str(project)]
                    ),
                    0,
                )

            calls = []

            def fake_call(command, *, env):
                calls.append(command)
                isolated_home = Path(env["CODEX_HOME"])
                isolated_auth = json.loads(
                    (isolated_home / "auth.json").read_text(encoding="utf-8")
                )
                self.assertEqual(isolated_auth["email"], "a@example.com")
                return 0

            with mock.patch("ai_auth_switch.wrapper.subprocess.call", fake_call):
                with mock.patch("ai_auth_switch.cli.Path.cwd", return_value=project):
                    status = cli_main(base + ["run", "codex"])
            self.assertEqual(status, 0)
            # The CLI builds its own provider, so the command is the resolved
            # Codex executable rather than the test's fake-codex.
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(calls[0]), 1)
