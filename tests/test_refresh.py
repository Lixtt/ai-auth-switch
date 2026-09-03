from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ai_auth_switch.cli import main as cli_main
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool import AUTH_EXPIRED, PoolCoordinator, PoolPolicy
from ai_auth_switch.providers.claude import ClaudeProvider
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.refresh import (
    FAILED,
    LOGIN_REQUIRED,
    REFRESHED,
    REJECTED,
    SKIPPED,
    RefreshResult,
    access_token_expires_at,
    has_refresh_token,
    needs_refresh,
    refresh_profile,
    refresh_profiles,
    supports_refresh,
)
from ai_auth_switch.store import AuthStore, ProfileInfo


def fake_jwt(payload: dict) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"header.{encoded}.signature"


def token_for(email: str, *, exp: int) -> str:
    return fake_jwt({"exp": exp, "https://api.openai.com/profile": {"email": email}})


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def http_error(status: int, body: dict) -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        "https://auth.openai.com/oauth/token",
        status,
        "Bad Request",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )


class RefreshHelperTests(unittest.TestCase):
    def test_expiry_and_refresh_token_are_read_from_the_auth_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": token_for("a@example.com", exp=2000),
                            "refresh_token": "rt-1",
                        }
                    }
                )
            )
            self.assertEqual(access_token_expires_at(path), 2000)
            self.assertTrue(has_refresh_token(path))
            self.assertFalse(needs_refresh(path, skew=0, now=1000))
            self.assertTrue(needs_refresh(path, skew=0, now=2000))
            self.assertTrue(needs_refresh(path, skew=300, now=1800))

    def test_unreadable_token_counts_as_needing_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({"tokens": {"access_token": "not-a-jwt"}}))
            self.assertIsNone(access_token_expires_at(path))
            self.assertTrue(needs_refresh(path, now=1000))
            self.assertFalse(has_refresh_token(path))

    def test_only_codex_supports_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(supports_refresh(CodexProvider(root / ".codex", ["x"])))
            self.assertFalse(supports_refresh(ClaudeProvider(root / ".claude")))


class RefreshProfileTests(unittest.TestCase):
    def _setup(self, *, exp: int = 1000, email: str = "a@example.com"):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        provider = CodexProvider(root / ".codex", ["fake-codex"])
        store = AuthStore(root / "store")
        store.ensure()
        path = store.profile_path(provider, "a")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "OPENAI_API_KEY": None,
                    "tokens": {
                        "access_token": token_for(email, exp=exp),
                        "refresh_token": "rt-old",
                        "id_token": token_for(email, exp=exp),
                    },
                }
            )
        )
        return tmp, store, provider, path

    def test_successful_refresh_writes_new_tokens_and_keeps_other_keys(self) -> None:
        tmp, store, provider, path = self._setup(exp=1000)
        with tmp:
            new_access = token_for("a@example.com", exp=9000)

            def opener(request, *, timeout):
                params = urllib.parse.parse_qs(request.data.decode())
                self.assertEqual(params["grant_type"], ["refresh_token"])
                self.assertEqual(params["refresh_token"], ["rt-old"])
                return FakeResponse(
                    {"access_token": new_access, "refresh_token": "rt-new"}
                )

            result = refresh_profile(
                store, provider, "a", opener=opener, now=2000
            )
            self.assertEqual(result.status, REFRESHED)
            self.assertTrue(result.changed)
            self.assertTrue(result.rotated)
            self.assertEqual(result.expires_at, 9000)

            data = json.loads(path.read_text())
            self.assertEqual(data["tokens"]["access_token"], new_access)
            self.assertEqual(data["tokens"]["refresh_token"], "rt-new")
            self.assertIn("last_refresh", data)
            # Unrelated keys survive the merge.
            self.assertIn("OPENAI_API_KEY", data)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_valid_token_is_skipped_unless_forced(self) -> None:
        tmp, store, provider, _path = self._setup(exp=9000)
        with tmp:
            calls = []

            def opener(request, *, timeout):
                calls.append(request)
                return FakeResponse(
                    {"access_token": token_for("a@example.com", exp=99000)}
                )

            skipped = refresh_profile(store, provider, "a", opener=opener, now=1000)
            self.assertEqual(skipped.status, SKIPPED)
            self.assertEqual(calls, [])

            forced = refresh_profile(
                store, provider, "a", force=True, opener=opener, now=1000
            )
            self.assertEqual(forced.status, REFRESHED)
            self.assertEqual(len(calls), 1)

    def test_permanent_error_codes_report_login_required(self) -> None:
        for code in (
            "invalid_grant",
            "invalid_refresh_token",
            "refresh_token_invalidated",
            "refresh_token_reused",
        ):
            with self.subTest(code=code):
                tmp, store, provider, path = self._setup(exp=1000)
                with tmp:
                    before = path.read_text()

                    def opener(request, *, timeout):
                        raise http_error(
                            400, {"error": {"code": code, "message": "nope"}}
                        )

                    result = refresh_profile(
                        store, provider, "a", opener=opener, now=2000
                    )
                    self.assertEqual(result.status, LOGIN_REQUIRED)
                    self.assertTrue(result.needs_login)
                    self.assertIn(code, result.message)
                    self.assertEqual(path.read_text(), before)

    def test_transient_error_reports_failure_without_touching_the_profile(self) -> None:
        tmp, store, provider, path = self._setup(exp=1000)
        with tmp:
            before = path.read_text()

            def opener(request, *, timeout):
                raise urllib.error.URLError("connection reset")

            result = refresh_profile(store, provider, "a", opener=opener, now=2000)
            self.assertEqual(result.status, FAILED)
            self.assertFalse(result.needs_login)
            self.assertEqual(path.read_text(), before)

    def test_server_error_without_a_code_is_transient(self) -> None:
        tmp, store, provider, _path = self._setup(exp=1000)
        with tmp:

            def opener(request, *, timeout):
                raise http_error(500, {"error": {"message": "server exploded"}})

            result = refresh_profile(store, provider, "a", opener=opener, now=2000)
            self.assertEqual(result.status, FAILED)

    def test_response_for_another_account_is_rejected_and_preserved(self) -> None:
        tmp, store, provider, path = self._setup(exp=1000, email="a@example.com")
        with tmp:
            before = path.read_text()

            def opener(request, *, timeout):
                return FakeResponse(
                    {
                        "access_token": token_for("intruder@example.com", exp=9000),
                        "id_token": token_for("intruder@example.com", exp=9000),
                    }
                )

            result = refresh_profile(store, provider, "a", opener=opener, now=2000)
            self.assertEqual(result.status, REJECTED)
            self.assertEqual(path.read_text(), before)
            rejected = list((store.backups_dir(provider) / "rejected").glob("*"))
            self.assertTrue(rejected)

    def test_missing_profile_and_missing_refresh_token_are_skipped(self) -> None:
        tmp, store, provider, path = self._setup(exp=1000)
        with tmp:
            missing = refresh_profile(store, provider, "nope", now=2000)
            self.assertEqual(missing.status, SKIPPED)

            path.write_text(
                json.dumps(
                    {"tokens": {"access_token": token_for("a@example.com", exp=1000)}}
                )
            )
            no_token = refresh_profile(store, provider, "a", now=2000)
            self.assertEqual(no_token.status, SKIPPED)

    def test_no_temporary_candidate_files_are_left_behind(self) -> None:
        tmp, store, provider, path = self._setup(exp=1000)
        with tmp:

            def opener(request, *, timeout):
                return FakeResponse(
                    {"access_token": token_for("a@example.com", exp=9000)}
                )

            refresh_profile(store, provider, "a", opener=opener, now=2000)
            leftovers = [p.name for p in path.parent.iterdir() if p.name.startswith(".")]
            self.assertEqual(leftovers, [])

    def test_claude_profiles_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = ClaudeProvider(root / ".claude")
            with self.assertRaises(AiAuthSwitchError):
                refresh_profile(store, provider, "a")

    def test_refresh_profiles_preserves_caller_order(self) -> None:
        tmp, store, provider, _path = self._setup(exp=1000)
        with tmp:
            def refresher(store_, provider_, name, **kwargs):
                return RefreshResult(name, SKIPPED, "stub")

            results = refresh_profiles(
                store, provider, ["c", "a", "b"], refresher=refresher, workers=4
            )
            self.assertEqual([r.profile for r in results], ["c", "a", "b"])


class PoolAutoRefreshTests(unittest.TestCase):
    def _setup(self, *, exp: int = 1000):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        provider = CodexProvider(root / ".codex", ["fake-codex"])
        store = AuthStore(root / "store")
        store.ensure()
        paths = []
        for name in ("a", "b"):
            path = store.profile_path(provider, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": token_for(f"{name}@example.com", exp=exp),
                            "refresh_token": f"rt-{name}",
                        }
                    }
                )
            )
            paths.append(ProfileInfo(name, path))
        coordinator = PoolCoordinator(
            store,
            provider,
            policy=PoolPolicy(refresh_retry_backoff_seconds=60.0, refresh_workers=1),
            pid_is_alive=lambda pid: False,
        )
        return tmp, store, provider, paths, coordinator

    def test_expired_profiles_are_refreshed_and_valid_ones_are_left_alone(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            seen = []

            def refresher(store_, provider_, name, **kwargs):
                seen.append(name)
                return RefreshResult(name, REFRESHED, "ok", 9000)

            results = coordinator.refresh_stale_auth(
                profiles, now=2000, refresher=refresher
            )
            self.assertEqual(sorted(seen), ["a", "b"])
            self.assertEqual({r.status for r in results}, {REFRESHED})

            seen.clear()
            fresh = self._setup(exp=99000)
            with fresh[0]:
                fresh[4].refresh_stale_auth(fresh[3], now=2000, refresher=refresher)
            self.assertEqual(seen, [])

    def test_permanent_failure_is_recorded_and_not_retried(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            calls = []

            def refresher(store_, provider_, name, **kwargs):
                calls.append(name)
                return RefreshResult(name, LOGIN_REQUIRED, "refresh_token_reused")

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=refresher)
            self.assertEqual(sorted(calls), ["a", "b"])
            state = coordinator.load()
            self.assertEqual(state.health["a"].status, AUTH_EXPIRED)
            self.assertIsNotNone(state.health["a"].auth_fingerprint)

            # A second pass over the unchanged files costs no network calls.
            calls.clear()
            coordinator.refresh_stale_auth(profiles, now=2100, refresher=refresher)
            self.assertEqual(calls, [])

    def test_a_replaced_credential_is_retried_after_a_login(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:

            def rejecting(store_, provider_, name, **kwargs):
                return RefreshResult(name, LOGIN_REQUIRED, "invalid_refresh_token")

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=rejecting)

            # Simulate `ais auth login` replacing the profile's credentials.
            profiles[0].path.write_text(
                json.dumps(
                    {
                        "tokens": {
                            "access_token": token_for("a@example.com", exp=1500),
                            "refresh_token": "rt-fresh",
                        }
                    }
                )
            )
            calls = []

            def recording(store_, provider_, name, **kwargs):
                calls.append(name)
                return RefreshResult(name, REFRESHED, "ok", 9000)

            coordinator.refresh_stale_auth(profiles, now=2100, refresher=recording)
            self.assertEqual(calls, ["a"])

    def test_transient_failure_backs_off_before_retrying(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            calls = []

            def failing(store_, provider_, name, **kwargs):
                calls.append(name)
                return RefreshResult(name, FAILED, "connection reset")

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=failing)
            self.assertEqual(sorted(calls), ["a", "b"])
            # A transient failure must not mark the account auth-expired.
            self.assertNotIn("a", coordinator.load().health)

            calls.clear()
            coordinator.refresh_stale_auth(profiles, now=2030, refresher=failing)
            self.assertEqual(calls, [])

            coordinator.refresh_stale_auth(profiles, now=2100, refresher=failing)
            self.assertEqual(sorted(calls), ["a", "b"])

    def test_profiles_without_a_refresh_token_are_never_attempted(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            profiles[0].path.write_text(
                json.dumps(
                    {"tokens": {"access_token": token_for("a@example.com", exp=1000)}}
                )
            )
            calls = []

            def refresher(store_, provider_, name, **kwargs):
                calls.append(name)
                return RefreshResult(name, REFRESHED, "ok", 9000)

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=refresher)
            self.assertEqual(calls, ["b"])

    def test_non_codex_providers_do_no_work(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = AuthStore(root / "store")
            provider = ClaudeProvider(root / ".claude")
            coordinator = PoolCoordinator(store, provider)

            def refresher(store_, provider_, name, **kwargs):  # pragma: no cover
                raise AssertionError("claude must not be refreshed")

            results = coordinator.refresh_stale_auth(
                [ProfileInfo("a", root / "a.json")], now=2000, refresher=refresher
            )
            self.assertEqual(results, [])

    def test_a_slow_endpoint_does_not_block_the_request_past_the_budget(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            coordinator.policy = replace(
                coordinator.policy, refresh_budget_seconds=0.2, refresh_workers=2
            )
            release = threading.Event()

            def slow(store_, provider_, name, **kwargs):
                release.wait(5)
                return RefreshResult(name, REFRESHED, "eventually", 9000)

            begin = time.monotonic()
            results = coordinator.refresh_stale_auth(
                profiles, now=2000, refresher=slow
            )
            elapsed = time.monotonic() - begin
            self.assertEqual(results, [])
            self.assertLess(elapsed, 3.0)

            # The stragglers still land, and still record their outcome.
            release.set()
            for _ in range(100):
                if not coordinator._refresh_inflight:
                    break
                time.sleep(0.02)
            self.assertEqual(coordinator._refresh_inflight, set())

    def test_an_inflight_exchange_is_not_started_twice(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            coordinator.policy = replace(
                coordinator.policy, refresh_budget_seconds=0.2, refresh_workers=2
            )
            release = threading.Event()
            calls: list[str] = []

            def slow(store_, provider_, name, **kwargs):
                calls.append(name)
                release.wait(5)
                return RefreshResult(name, REFRESHED, "eventually", 9000)

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=slow)
            self.assertEqual(len(calls), 2)

            # A second request arriving while the first exchange is still open
            # must not dial the same endpoint again.
            coordinator.refresh_stale_auth(profiles, now=2001, refresher=slow)
            self.assertEqual(len(calls), 2)
            release.set()

    def test_a_hung_exchange_is_backed_off_even_before_it_returns(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup(exp=1000)
        with tmp:
            coordinator.policy = replace(
                coordinator.policy,
                refresh_budget_seconds=0.2,
                refresh_workers=2,
                refresh_retry_backoff_seconds=60.0,
            )
            release = threading.Event()

            def slow(store_, provider_, name, **kwargs):
                release.wait(5)
                return RefreshResult(name, FAILED, "timed out")

            coordinator.refresh_stale_auth(profiles, now=2000, refresher=slow)
            self.assertEqual(
                coordinator._refresh_retry_after, {"a": 2060.0, "b": 2060.0}
            )
            release.set()


class AuthRefreshCommandTests(unittest.TestCase):
    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        codex_home = root / ".codex"
        codex_home.mkdir()
        store_dir = root / "store"
        provider = CodexProvider(codex_home=codex_home, login_command=["fake-codex"])
        store = AuthStore(store_dir)
        store.ensure()
        for name in ("a", "b"):
            store.write_profile_content(
                provider,
                name,
                json.dumps(
                    {
                        "tokens": {
                            "access_token": token_for(f"{name}@example.com", exp=1000),
                            "refresh_token": f"rt-{name}",
                        }
                    }
                ),
            )
        base = ["--store-dir", str(store_dir), "--codex-home", str(codex_home)]
        return tmp, store, provider, base

    def _run(self, argv, results):
        def refresh_profiles(store, provider, names, **kwargs):
            return [results[name] for name in names]

        out = io.StringIO()
        with mock.patch("ai_auth_switch.refresh.refresh_profiles", refresh_profiles):
            with contextlib.redirect_stdout(out):
                code = cli_main(argv)
        return code, out.getvalue()

    def test_all_refreshes_every_profile_and_reports_success(self) -> None:
        tmp, _store, _provider, base = self._setup()
        with tmp:
            code, output = self._run(
                base + ["auth", "refresh", "codex", "--all"],
                {
                    "a": RefreshResult("a", REFRESHED, "valid until later", 9000),
                    "b": RefreshResult("b", SKIPPED, "still valid", 9000),
                },
            )
            self.assertEqual(code, 0)
            self.assertIn("refreshed", output)
            self.assertIn("skipped", output)

    def test_dead_refresh_token_exits_nonzero_with_a_login_hint(self) -> None:
        tmp, _store, _provider, base = self._setup()
        with tmp:
            code, output = self._run(
                base + ["auth", "refresh", "codex", "a"],
                {"a": RefreshResult("a", LOGIN_REQUIRED, "reused (refresh_token_reused)")},
            )
            self.assertEqual(code, 1)
            self.assertIn("ais auth login codex", output)
            self.assertIn("  a", output)

    def test_json_output_is_machine_readable(self) -> None:
        tmp, _store, _provider, base = self._setup()
        with tmp:
            code, output = self._run(
                base + ["auth", "refresh", "codex", "a", "--json"],
                {"a": RefreshResult("a", REFRESHED, "ok", 9000, True)},
            )
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertEqual(
                payload["results"],
                [
                    {
                        "profile": "a",
                        "status": REFRESHED,
                        "message": "ok",
                        "expires_at": 9000,
                        "refresh_token_rotated": True,
                    }
                ],
            )

    def test_unknown_profile_is_rejected_before_any_network_call(self) -> None:
        tmp, _store, _provider, base = self._setup()
        with tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(base + ["auth", "refresh", "codex", "nope"])
            self.assertEqual(code, 2)
            self.assertIn("unknown codex profile(s): nope", err.getvalue())

    def test_claude_is_refused(self) -> None:
        tmp, _store, _provider, base = self._setup()
        with tmp:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = cli_main(base + ["auth", "refresh", "claude", "--all"])
            self.assertEqual(code, 2)
            self.assertIn("not supported for claude", err.getvalue())


if __name__ == "__main__":
    unittest.main()
