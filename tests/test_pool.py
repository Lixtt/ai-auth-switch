from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool import (
    COOLDOWN,
    PoolCoordinator,
    PoolLease,
    PoolPolicy,
    PoolState,
    pool_state_path,
)
from ai_auth_switch.providers.codex import CodexProvider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, UsageWindow


def usage(remaining: float, *, plan: str = "pro") -> AccountUsage:
    return AccountUsage(
        plan_type=plan,
        secondary=UsageWindow(
            used_percent=100 - remaining,
            window_seconds=604800,
            resets_at=2_000_000_000,
        ),
    )


class PoolTests(unittest.TestCase):
    def _setup(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        provider = CodexProvider(root / ".codex", ["fake-codex"])
        store = AuthStore(root / "store")
        profiles = [
            ProfileInfo("a", root / "a.json"),
            ProfileInfo("b", root / "b.json"),
        ]
        coordinator = PoolCoordinator(
            store,
            provider,
            policy=PoolPolicy(stale_lease_seconds=60),
            pid_is_alive=lambda pid: pid == 100,
        )
        return tmp, store, provider, profiles, coordinator

    def test_reservation_uses_capacity_and_sticks_to_route(self) -> None:
        tmp, store, _provider, profiles, coordinator = self._setup()
        with tmp:
            usages = {"a": usage(90), "b": usage(40)}
            first = coordinator.reserve(
                profiles, usages, route_key="thread-1", owner="pid:100", now=10
            )
            self.assertEqual(first.profile, "a")
            second = coordinator.reserve(
                profiles,
                {"a": usage(1), "b": usage(80)},
                route_key="thread-1",
                owner="pid:100",
                now=11,
            )
            self.assertEqual(second.profile, "a")
            coordinator.release(first)
            coordinator.release(second)

    def test_route_can_migrate_when_sticky_backend_is_unhealthy(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup()
        with tmp:
            usages = {"a": usage(90), "b": usage(80)}
            first = coordinator.reserve(
                profiles, usages, route_key="thread-1", owner="pid:100", now=10
            )
            coordinator.release(first)
            coordinator.mark_failure("a", "401", "expired", now=11)
            migrated = coordinator.reserve(
                profiles,
                usages,
                route_key="thread-1",
                owner="pid:100",
                now=12,
                allow_migrate=True,
            )
            self.assertEqual(migrated.profile, "b")
            coordinator.release(migrated)

    def test_sticky_recovery_keeps_route_selected_by_another_worker(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup()
        with tmp:
            first = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                route_key="thread-1",
                owner="pid:100",
                now=10,
            )
            coordinator.release(first)
            coordinator.mark_failure("a", "401", "expired", now=11)

            # Another worker has already migrated the route to b. Recovery
            # must retain that healthy target instead of selecting a again.
            migrated = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                route_key="thread-1",
                owner="pid:100",
                now=12,
                allow_migrate=True,
            )
            coordinator.release(migrated)
            coordinator.mark_success("a", now=12.5)
            recovered = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                route_key="thread-1",
                owner="pid:100",
                now=13,
                allow_migrate=True,
                recover_sticky=True,
            )
            self.assertEqual(recovered.profile, "b")
            coordinator.release(recovered)

    def test_sticky_route_refuses_unhealthy_backend_without_migration(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup()
        with tmp:
            first = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                route_key="thread-1",
                owner="pid:100",
                now=10,
            )
            coordinator.release(first)
            coordinator.mark_failure("a", "401", "expired", now=11)
            with self.assertRaisesRegex(AiAuthSwitchError, "sticky pool route"):
                coordinator.reserve(
                    profiles,
                    {"a": usage(90), "b": usage(80)},
                    route_key="thread-1",
                    owner="pid:100",
                    now=12,
                )

    def test_429_cooldown_expires_and_health_recovers(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup()
        with tmp:
            coordinator.mark_failure("a", "429", "rate limited", now=10)
            state = coordinator.load()
            self.assertEqual(state.health["a"].status, COOLDOWN)
            first = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(20)},
                owner="pid:100",
                now=20,
            )
            self.assertEqual(first.profile, "b")
            coordinator.release(first)
            second = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(20)},
                owner="pid:100",
                now=41,
            )
            self.assertEqual(second.profile, "a")
            coordinator.release(second)

    def test_stale_and_dead_leases_are_pruned(self) -> None:
        tmp, store, provider, profiles, coordinator = self._setup()
        with tmp:
            path = pool_state_path(store, provider)
            state = PoolState(
                leases=[
                    PoolLease("dead", "a", "pid:99", 1, 10),
                    PoolLease("stale", "a", "pid:100", 1, 1),
                ]
            )
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(state.to_dict()), encoding="utf-8")
            reservation = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(80)},
                owner="pid:100",
                now=100,
            )
            self.assertEqual(reservation.profile, "a")
            self.assertEqual(len(coordinator.load().leases), 1)
            coordinator.release(reservation)

    def test_free_and_auth_expired_accounts_are_not_candidates(self) -> None:
        tmp, _store, _provider, profiles, coordinator = self._setup()
        with tmp, self.assertRaisesRegex(AiAuthSwitchError, "no eligible"):
            coordinator.reserve(
                profiles,
                {"a": usage(90, plan="free"), "b": AccountUsage(error="expired")},
                owner="pid:100",
                now=10,
            )

    def test_success_clears_failure_state(self) -> None:
        tmp, _store, _provider, _profiles, coordinator = self._setup()
        with tmp:
            coordinator.mark_failure("a", "401", "expired", now=10)
            coordinator.mark_success("a", now=20)
            health = coordinator.load().health["a"]
            self.assertEqual(health.status, "healthy")
            self.assertEqual(health.failure_count, 0)
            self.assertIsNone(health.last_error)

    def test_auth_failure_can_recover_after_profile_credentials_change(self) -> None:
        tmp, store, provider, _profiles, coordinator = self._setup()
        with tmp:
            # Use real profile files so the coordinator can detect the
            # credential replacement rather than relying on synthetic paths.
            profiles = []
            for name in ("a", "b"):
                path = store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"old-{name}"}}),
                ).path
                profiles.append(ProfileInfo(name, path))
            coordinator.mark_failure("a", "401", "expired", now=10)
            store.profile_path(provider, "a").write_text(
                json.dumps({"tokens": {"access_token": "new-a"}}),
                encoding="utf-8",
            )

            reservation = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(20)},
                owner="pid:100",
                now=11,
            )
            self.assertEqual(reservation.profile, "a")
            coordinator.release(reservation)

    def test_legacy_auth_failure_state_is_not_reenabled_without_change(self) -> None:
        tmp, store, provider, _profiles, coordinator = self._setup()
        with tmp:
            profiles = []
            for name in ("a", "b"):
                path = store.write_profile_content(
                    provider,
                    name,
                    json.dumps({"tokens": {"access_token": f"token-{name}"}}),
                ).path
                profiles.append(ProfileInfo(name, path))
            # Write a legacy-shaped JSON object directly to model
            # pre-fingerprint state.
            pool_path = pool_state_path(store, provider)
            pool_path.parent.mkdir(parents=True, exist_ok=True)
            pool_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "health": {"a": {"status": "auth_expired"}},
                    }
                ),
                encoding="utf-8",
            )

            reservation = coordinator.reserve(
                profiles,
                {"a": usage(90), "b": usage(20)},
                owner="pid:100",
                now=11,
            )
            self.assertEqual(reservation.profile, "b")
            coordinator.release(reservation)
            self.assertIsNotNone(coordinator.load().health["a"].auth_fingerprint)


if __name__ == "__main__":
    unittest.main()
