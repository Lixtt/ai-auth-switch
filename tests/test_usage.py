from __future__ import annotations

import base64
import contextlib
import io
import json
import tempfile
import time
import unittest
import urllib.error
from datetime import timezone
from pathlib import Path
from unittest import mock

from ai_auth_switch.usage import (
    AccountUsage,
    UsageWindow,
    fetch_profile_usage,
    fetch_usage,
    format_usage,
    parse_usage,
)


def fake_jwt(payload: dict) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"header.{encoded}.signature"


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


class UsageTests(unittest.TestCase):
    def test_fetch_uses_profile_token_and_account_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            token = fake_jwt(
                {"https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"}}
            )
            auth.write_text(json.dumps({"tokens": {"access_token": token}}))
            seen = {}

            def opener(request, *, timeout):
                seen["authorization"] = request.get_header("Authorization")
                seen["account"] = request.get_header("Chatgpt-account-id")
                seen["timeout"] = timeout
                return FakeResponse(
                    {
                        "plan_type": "plus",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 28,
                                "limit_window_seconds": 18000,
                                "reset_at": 1_800_000_000,
                            }
                        },
                    }
                )

            usage = fetch_usage(auth, timeout=2.5, opener=opener)
            self.assertEqual(seen["authorization"], f"Bearer {token}")
            self.assertEqual(seen["account"], "acc-1")
            self.assertEqual(seen["timeout"], 2.5)
            self.assertEqual(
                format_usage(usage, now=1_799_996_275, tz=timezone.utc),
                "plus, 5h 72% left, resets 2027-01-15T08:00:00Z (in 1h 2m)",
            )

    def test_format_usage_shows_each_window_reset_time(self) -> None:
        usage = AccountUsage(
            plan_type="plus",
            primary=UsageWindow(
                used_percent=28, window_seconds=18_000, resets_at=1_800_000_000
            ),
            secondary=UsageWindow(
                used_percent=59,
                window_seconds=604_800,
                resets_at=1_800_086_400,
            ),
        )
        self.assertEqual(
            format_usage(usage, now=1_799_996_275, tz=timezone.utc),
            "plus, 5h 72% left, resets 2027-01-15T08:00:00Z (in 1h 2m), "
            "168h 41% left, resets 2027-01-16T08:00:00Z (in 1d 1h)",
        )

    def test_usage_json_includes_machine_and_display_reset_times(self) -> None:
        from ai_auth_switch.usage import usage_to_dict

        payload = usage_to_dict(
            AccountUsage(primary=UsageWindow(used_percent=1, resets_at=1_800_000_000))
        )
        self.assertEqual(payload["primary"]["resets_at"], 1_800_000_000)
        self.assertEqual(payload["primary"]["resets_at_iso"], "2027-01-15T08:00:00Z")

    def test_parse_usage_supports_two_windows_and_credits(self) -> None:
        usage = parse_usage(
            {
                "plan_type": "team",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 10,
                        "limit_window_seconds": 300,
                    },
                    "secondary_window": {
                        "used_percent": 75.5,
                        "limit_window_seconds": 604800,
                    },
                },
                "credits": {"has_credits": True, "balance": "9.99"},
            }
        )
        self.assertEqual(
            format_usage(usage), "team, 5m 90% left, 168h 24.5% left, credits 9.99"
        )

    def test_parallel_fetch_is_account_isolated_and_failure_tolerant(self) -> None:
        paths = [("slow", Path("slow")), ("fast", Path("fast")), ("bad", Path("bad"))]

        def fetcher(path, *, timeout):
            if path.name == "slow":
                time.sleep(0.05)
            if path.name == "bad":
                raise RuntimeError("boom")
            return AccountUsage(plan_type=path.name)

        started = time.monotonic()
        results = fetch_profile_usage(paths, workers=3, fetcher=fetcher)
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(results["slow"].plan_type, "slow")
        self.assertEqual(results["fast"].plan_type, "fast")
        self.assertEqual(results["bad"].error, "request failed: boom")

    def test_usage_cache_is_reused_and_invalidated_with_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth.json"
            auth.write_text('{"version": 1}')
            calls = []

            def fetcher(path, *, timeout):
                calls.append(path.read_text())
                return AccountUsage(plan_type=f"plan-{len(calls)}")

            kwargs = {"cache_dir": root / "cache", "fetcher": fetcher}
            first = fetch_profile_usage([("one", auth)], **kwargs)
            second = fetch_profile_usage([("one", auth)], **kwargs)
            self.assertEqual(first["one"].plan_type, "plan-1")
            self.assertEqual(second["one"].plan_type, "plan-1")
            self.assertEqual(len(calls), 1)

            auth.write_text('{"version": 2}')
            third = fetch_profile_usage([("one", auth)], **kwargs)
            self.assertEqual(third["one"].plan_type, "plan-2")
            self.assertEqual(len(calls), 2)

    def test_failed_refresh_keeps_last_good_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth.json"
            auth.write_text('{"version": 1}')
            calls = []

            def fetcher(path, *, timeout):
                calls.append(path.read_text())
                if len(calls) >= 2:
                    raise RuntimeError("boom")
                return AccountUsage(plan_type="good")

            kwargs = {
                "cache_dir": root / "cache",
                "fetcher": fetcher,
                "refresh": True,
            }
            first = fetch_profile_usage([("one", auth)], **kwargs)
            self.assertEqual(first["one"].plan_type, "good")

            second = fetch_profile_usage([("one", auth)], **kwargs)
            # A failed refresh must fall back to the last good snapshot rather
            # than stranding a healthy account with a transient error.
            self.assertEqual(second["one"].plan_type, "good")
            self.assertIsNone(second["one"].error)

    def test_authentication_expiry_replaces_last_good_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth.json"
            auth.write_text('{"version": 1}')
            calls = []

            def fetcher(path, *, timeout):
                calls.append(path.read_text())
                if len(calls) == 1:
                    return AccountUsage(plan_type="free")
                return AccountUsage(error="authentication expired")

            kwargs = {
                "cache_dir": root / "cache",
                "fetcher": fetcher,
                "refresh": True,
            }
            first = fetch_profile_usage([("one", auth)], **kwargs)
            self.assertEqual(first["one"].plan_type, "free")

            expired = fetch_profile_usage([("one", auth)], **kwargs)
            self.assertEqual(expired["one"].error, "authentication expired")
            self.assertIsNone(expired["one"].plan_type)
            self.assertEqual(
                format_usage(expired["one"]),
                "usage unavailable: authentication expired",
            )

            # The definitive error is cached, so a normal (non-refresh) list
            # cannot immediately resurrect the stale free-plan snapshot.
            cached = fetch_profile_usage(
                [("one", auth)],
                cache_dir=root / "cache",
                fetcher=fetcher,
            )
            self.assertEqual(cached["one"].error, "authentication expired")
            self.assertIsNone(cached["one"].plan_type)
            self.assertEqual(len(calls), 2)

    def test_raised_http_auth_error_replaces_last_good_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auth = root / "auth.json"
            auth.write_text('{"version": 1}')

            fetch_profile_usage(
                [("one", auth)],
                cache_dir=root / "cache",
                refresh=True,
                fetcher=lambda _path, **_kwargs: AccountUsage(plan_type="pro"),
            )

            def fetcher(_path, **_kwargs):
                raise urllib.error.HTTPError(
                    "https://chatgpt.com/backend-api/wham/usage",
                    401,
                    "expired",
                    {},
                    None,
                )

            expired = fetch_profile_usage(
                [("one", auth)],
                cache_dir=root / "cache",
                refresh=True,
                fetcher=fetcher,
            )
            self.assertEqual(expired["one"].error, "authentication expired")
            self.assertIsNone(expired["one"].plan_type)

    def test_profile_sort_puts_free_after_paid(self) -> None:
        from ai_auth_switch.cli import _profile_sort_key
        from ai_auth_switch.store import ProfileInfo

        aliases = {"free": "codex1", "paid": "codex2"}
        free = ProfileInfo("free", Path("free.json"))
        paid = ProfileInfo("paid", Path("paid.json"))
        usages = {
            "free": AccountUsage(plan_type="free"),
            "paid": AccountUsage(plan_type="pro"),
        }
        # free has the smaller alias index, but paid must still sort first.
        self.assertLess(
            _profile_sort_key(aliases, paid, usages),
            _profile_sort_key(aliases, free, usages),
        )

    def test_cli_list_usage_formats_each_profile(self) -> None:
        from ai_auth_switch.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_dir = root / "store" / "profiles" / "codex"
            profile_dir.mkdir(parents=True)
            (profile_dir / "one.json").write_text("{}")
            with (
                mock.patch(
                    "ai_auth_switch.cli.fetch_profile_usage",
                    return_value={
                        "one": AccountUsage(
                            plan_type="plus",
                            primary=UsageWindow(
                                used_percent=28,
                                window_seconds=18_000,
                                resets_at=1_800_000_000,
                            ),
                        )
                    },
                ),
                mock.patch("builtins.print") as output,
            ):
                status = main(
                    [
                        "--store-dir",
                        str(root / "store"),
                        "--codex-home",
                        str(root / "codex"),
                        "auth",
                        "list",
                        "codex",
                        "--usage",
                    ]
                )
            self.assertEqual(status, 0)
            self.assertTrue(
                any("resets 2027-01-15T" in str(call) for call in output.call_args_list)
            )

    def test_cli_list_json_is_structured_and_does_not_expose_paths(self) -> None:
        from ai_auth_switch.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_dir = root / "store" / "profiles" / "codex"
            profile_dir.mkdir(parents=True)
            (profile_dir / "one.json").write_text('{"email":"one"}')
            output = io.StringIO()
            with (
                mock.patch(
                    "ai_auth_switch.cli.fetch_profile_usage",
                    return_value={"one": AccountUsage(plan_type="pro")},
                ),
                contextlib.redirect_stdout(output),
            ):
                status = main(
                    [
                        "--store-dir",
                        str(root / "store"),
                        "--codex-home",
                        str(root / "codex"),
                        "auth",
                        "list",
                        "codex",
                        "--usage",
                        "--json",
                    ]
                )
            payload = json.loads(output.getvalue())
            entry = payload["providers"]["codex"][0]
            self.assertEqual(status, 0)
            self.assertEqual(entry["name"], "one")
            self.assertEqual(entry["usage"]["plan_type"], "pro")
            self.assertNotIn("path", entry)

    def test_typo_prints_command_help_and_suggestion(self) -> None:
        from ai_auth_switch.cli import main

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = main(["auth", "lsit"])
        rendered = error.getvalue()
        self.assertEqual(status, 2)
        self.assertIn("usage: ai-auth-switch auth", rendered)
        self.assertIn("Did you mean 'list'?", rendered)
        self.assertIn("List profiles.", rendered)

    def test_short_program_name_is_used_in_help(self) -> None:
        from ai_auth_switch.cli import main

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            status = main(["auth", "lsit"], program_name="ais")
        self.assertEqual(status, 2)
        self.assertIn("usage: ais auth", error.getvalue())


if __name__ == "__main__":
    unittest.main()
