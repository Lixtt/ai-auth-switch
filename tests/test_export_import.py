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


class ExportImportTests(unittest.TestCase):
    def test_export_to_file_import_into_fresh_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")
            _save_profile(store, provider, "b@example.com")

            export_file = root / "export.json"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    cli_main(base + ["auth", "export", "codex", "-o", str(export_file)]),
                    0,
                )
            self.assertIn("exported codex profiles", out.getvalue())
            # The export file is private because it contains credentials.
            self.assertEqual(export_file.stat().st_mode & 0o777, 0o600)
            payload = json.loads(export_file.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 2)
            self.assertEqual(
                set(payload["providers"]["codex"]),
                {"a@example.com", "b@example.com"},
            )

            # Import into a fresh store.
            store2_dir = root / "store2"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store2_dir),
                        "--codex-home",
                        str(codex_home),
                        "auth",
                        "import",
                        str(export_file),
                    ]
                )
            self.assertEqual(status, 0)
            self.assertIn("imported codex profile a@example.com", out.getvalue())
            self.assertIn("imported codex profile b@example.com", out.getvalue())
            store2 = AuthStore(store2_dir)
            provider2 = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            names = {p.name for p in store2.list_profiles(provider2)}
            self.assertEqual(names, {"a@example.com", "b@example.com"})
            content = json.loads(
                store2.profile_path(provider2, "a@example.com").read_text(encoding="utf-8")
            )
            self.assertEqual(content["email"], "a@example.com")
            # Imported profiles got numbered aliases.
            self.assertIsNotNone(store2.resolve_alias("codex1"))

    def test_import_skips_existing_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")

            export_file = root / "export.json"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                cli_main(base + ["auth", "export", "codex", "-o", str(export_file)])

            # Re-import: existing profile is skipped.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(base + ["auth", "import", str(export_file)])
            self.assertEqual(status, 0)
            self.assertIn("skipped existing codex profile a@example.com", out.getvalue())
            self.assertNotIn("imported codex profile", out.getvalue())

            # Forcing overwrites.
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli_main(base + ["auth", "import", "--force", str(export_file)])
            self.assertEqual(status, 0)
            self.assertIn("imported codex profile a@example.com", out.getvalue())

    def test_import_rejects_invalid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]

            bad = root / "bad.json"
            bad.write_text("not json", encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(base + ["auth", "import", str(bad)])
            self.assertEqual(status, 2)
            self.assertIn("invalid JSON", err.getvalue())

            empty = root / "empty.json"
            empty.write_text('{"version": 1}', encoding="utf-8")
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli_main(base + ["auth", "import", str(empty)])
            self.assertEqual(status, 2)
            self.assertIn("missing 'providers' object", err.getvalue())

    def test_export_stdout_is_pure_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = cli_main(base + ["auth", "export", "codex"])
            self.assertEqual(status, 0)
            # stdout must parse as JSON so it is safe to pipe.
            payload = json.loads(out.getvalue())
            self.assertIn("a@example.com", payload["providers"]["codex"])
            # The credential warning goes to stderr only.
            self.assertIn("credentials", err.getvalue())

    def test_import_from_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            out = io.StringIO()
            err = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(cli_main(base + ["auth", "export", "codex"]), 0)
            exported = out.getvalue()

            # Import the piped export into a fresh store via stdin ("-").
            store2_dir = root / "store2"
            out2 = io.StringIO()
            with mock.patch("sys.stdin", io.StringIO(exported)), contextlib.redirect_stdout(
                out2
            ):
                status = cli_main(
                    [
                        "--store-dir",
                        str(store2_dir),
                        "--codex-home",
                        str(codex_home),
                        "auth",
                        "import",
                        "-",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertIn("imported codex profile a@example.com", out2.getvalue())
            store2 = AuthStore(store2_dir)
            names = {p.name for p in store2.list_profiles(provider)}
            self.assertEqual(names, {"a@example.com"})


class DefaultBindJsonTests(unittest.TestCase):
    def test_auth_default_and_bind_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex"
            codex_home.mkdir()
            store_dir = root / "store"
            provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
            store = AuthStore(store_dir)
            _save_profile(store, provider, "a@example.com")
            with store.lock():
                store.set_default(provider, "a@example.com")

            base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli_main(base + ["auth", "default", "codex", "--json"]), 0)
            self.assertEqual(
                json.loads(out.getvalue())["providers"]["codex"]["default"],
                "a@example.com",
            )

            project = root / "project"
            project.mkdir()
            with contextlib.redirect_stdout(io.StringIO()):
                cli_main(
                    base + ["auth", "bind", "codex", "a@example.com", "--dir", str(project)]
                )
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(
                    cli_main(
                        base
                        + [
                            "auth",
                            "bind",
                            "codex",
                            "--dir",
                            str(project),
                            "--json",
                        ]
                    ),
                    0,
                )
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["providers"]["codex"]["binding"], "a@example.com")
            self.assertEqual(payload["providers"]["codex"]["dir"], str(project.resolve()))
