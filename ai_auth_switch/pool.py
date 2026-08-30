from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, ProfileInfo, sha256_file
from ai_auth_switch.usage import AccountUsage, is_free_plan
from ai_auth_switch.utils import atomic_write

HEALTHY = "healthy"
AUTH_EXPIRED = "auth_expired"
COOLDOWN = "cooldown"
DISABLED = "disabled"


@dataclass(frozen=True)
class PoolPolicy:
    cooldown_seconds: float = 30.0
    max_failure_cooldown_seconds: float = 900.0
    stale_lease_seconds: float = 24 * 60 * 60


@dataclass(frozen=True)
class PoolLease:
    id: str
    profile: str
    owner: str
    started_at: float
    last_heartbeat: float
    route_key: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> PoolLease | None:
        if not isinstance(value, dict):
            return None
        required = ("id", "profile", "owner", "started_at", "last_heartbeat")
        if any(key not in value for key in required):
            return None
        if not all(isinstance(value[key], str) and value[key] for key in required[:3]):
            return None
        if not all(
            isinstance(value[key], (int, float))
            for key in ("started_at", "last_heartbeat")
        ):
            return None
        route_key = value.get("route_key")
        if route_key is not None and not isinstance(route_key, str):
            return None
        return cls(
            id=value["id"],
            profile=value["profile"],
            owner=value["owner"],
            started_at=float(value["started_at"]),
            last_heartbeat=float(value["last_heartbeat"]),
            route_key=route_key,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile": self.profile,
            "owner": self.owner,
            "started_at": self.started_at,
            "last_heartbeat": self.last_heartbeat,
            "route_key": self.route_key,
        }


@dataclass(frozen=True)
class PoolRoute:
    key: str
    profile: str
    created_at: float
    updated_at: float

    @classmethod
    def from_dict(cls, key: str, value: object) -> PoolRoute | None:
        if not isinstance(value, dict):
            return None
        profile = value.get("profile")
        created_at = value.get("created_at")
        updated_at = value.get("updated_at")
        if (
            not isinstance(profile, str)
            or not profile
            or not isinstance(created_at, (int, float))
            or not isinstance(updated_at, (int, float))
        ):
            return None
        return cls(key, profile, float(created_at), float(updated_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PoolHealth:
    status: str = HEALTHY
    failure_count: int = 0
    cooldown_until: float | None = None
    last_error: str | None = None
    updated_at: float = 0.0
    auth_fingerprint: str | None = None

    @classmethod
    def from_dict(cls, value: object) -> PoolHealth:
        if not isinstance(value, dict):
            return cls()
        status = value.get("status")
        if status not in {HEALTHY, AUTH_EXPIRED, COOLDOWN, DISABLED}:
            status = HEALTHY
        failure_count = value.get("failure_count")
        if not isinstance(failure_count, int) or failure_count < 0:
            failure_count = 0
        cooldown_until = value.get("cooldown_until")
        if not isinstance(cooldown_until, (int, float)):
            cooldown_until = None
        last_error = value.get("last_error")
        if not isinstance(last_error, str):
            last_error = None
        updated_at = value.get("updated_at")
        if not isinstance(updated_at, (int, float)):
            updated_at = 0.0
        auth_fingerprint = value.get("auth_fingerprint")
        if not isinstance(auth_fingerprint, str) or not auth_fingerprint.strip():
            auth_fingerprint = None
        return cls(
            status=status,
            failure_count=failure_count,
            cooldown_until=float(cooldown_until)
            if cooldown_until is not None
            else None,
            last_error=last_error,
            updated_at=float(updated_at),
            auth_fingerprint=auth_fingerprint,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "failure_count": self.failure_count,
            "cooldown_until": self.cooldown_until,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }
        if self.auth_fingerprint is not None:
            payload["auth_fingerprint"] = self.auth_fingerprint
        return payload


@dataclass
class PoolState:
    leases: list[PoolLease] = field(default_factory=list)
    routes: dict[str, PoolRoute] = field(default_factory=dict)
    health: dict[str, PoolHealth] = field(default_factory=dict)
    selected_at: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: object) -> PoolState:
        if not isinstance(value, dict):
            return cls()
        raw_leases = value.get("leases")
        leases = []
        if isinstance(raw_leases, list):
            for raw in raw_leases:
                lease = PoolLease.from_dict(raw)
                if lease is not None:
                    leases.append(lease)
        raw_routes = value.get("routes")
        routes = {}
        if isinstance(raw_routes, dict):
            for key, raw in raw_routes.items():
                if isinstance(key, str):
                    route = PoolRoute.from_dict(key, raw)
                    if route is not None:
                        routes[key] = route
        raw_health = value.get("health")
        health = (
            {
                key: PoolHealth.from_dict(raw)
                for key, raw in raw_health.items()
                if isinstance(key, str)
            }
            if isinstance(raw_health, dict)
            else {}
        )
        raw_selected = value.get("selected_at")
        selected_at = (
            {
                key: float(timestamp)
                for key, timestamp in raw_selected.items()
                if isinstance(key, str) and isinstance(timestamp, (int, float))
            }
            if isinstance(raw_selected, dict)
            else {}
        )
        return cls(leases=leases, routes=routes, health=health, selected_at=selected_at)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "leases": [lease.to_dict() for lease in self.leases],
            "routes": {
                key: route.to_dict() for key, route in sorted(self.routes.items())
            },
            "health": {
                key: health.to_dict() for key, health in sorted(self.health.items())
            },
            "selected_at": dict(sorted(self.selected_at.items())),
        }


@dataclass(frozen=True)
class PoolCandidate:
    profile: str
    remaining_percent: float
    effective_remaining: float
    active_leases: int
    resets_at: int | None
    health_status: str


@dataclass(frozen=True)
class PoolReservation:
    lease_id: str
    profile: str
    route_key: str | None
    remaining_percent: float
    effective_remaining: float
    resets_at: int | None


def pool_state_path(store: AuthStore, provider: Provider) -> Path:
    return store.base_dir / "pool" / f"{provider.id}.json"


def _usage_capacity(usage: AccountUsage | None) -> tuple[float, int | None] | None:
    if usage is None or usage.error or is_free_plan(usage):
        return None
    windows = [window for window in (usage.primary, usage.secondary) if window]
    if not windows:
        return None
    remaining = min(window.remaining_percent for window in windows)
    if remaining <= 0:
        return None
    resets = [window.resets_at for window in windows if window.resets_at is not None]
    return remaining, min(resets) if resets else None


def _health_eligible(health: PoolHealth, now: float) -> bool:
    if health.status in {AUTH_EXPIRED, DISABLED}:
        return False
    if health.status == COOLDOWN and health.cooldown_until is not None:
        return health.cooldown_until <= now
    return True


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PoolCoordinator:
    """Persistent account-pool routing state without credential material."""

    def __init__(
        self,
        store: AuthStore,
        provider: Provider,
        *,
        policy: PoolPolicy | None = None,
        pid_is_alive: Callable[[int], bool] = _pid_is_alive,
    ):
        self.store = store
        self.provider = provider
        self.policy = policy or PoolPolicy()
        self.pid_is_alive = pid_is_alive
        self.path = pool_state_path(store, provider)

    def load(self) -> PoolState:
        try:
            return PoolState.from_dict(
                json.loads(self.path.read_text(encoding="utf-8"))
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return PoolState()

    def save(self, state: PoolState) -> None:
        atomic_write(
            self.path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
        )

    @staticmethod
    def _profile_auth_fingerprint(profile: ProfileInfo) -> str | None:
        try:
            return sha256_file(profile.path)
        except OSError:
            return None

    def _auth_fingerprint_for_name(self, profile: str) -> str | None:
        return self._profile_auth_fingerprint(
            ProfileInfo(profile, self.store.profile_path(self.provider, profile))
        )

    def _health_after_auth_change(
        self,
        profile: ProfileInfo,
        health: PoolHealth,
        state: PoolState,
        now: float,
    ) -> PoolHealth:
        """Recover an auth-expired profile only after its file changes.

        Authentication failures remain sticky so a bad token is not retried on
        every request. Codex can replace a profile's credentials after a fresh
        login or token refresh, though, and that new credential must be allowed
        back into the pool. Older state files have no fingerprint; for those,
        initialize the fingerprint on first observation while preserving the
        existing expired status.
        """
        if health.status != AUTH_EXPIRED:
            return health
        fingerprint = self._profile_auth_fingerprint(profile)
        if fingerprint is None:
            return health
        if health.auth_fingerprint is None:
            updated = replace(health, auth_fingerprint=fingerprint)
            state.health[profile.name] = updated
            return updated
        if fingerprint == health.auth_fingerprint:
            return health
        updated = PoolHealth(
            status=HEALTHY,
            updated_at=now,
            auth_fingerprint=fingerprint,
        )
        state.health[profile.name] = updated
        state.selected_at.setdefault(profile.name, now)
        return updated

    def _prune(self, state: PoolState, now: float) -> None:
        state.leases = [
            lease
            for lease in state.leases
            if _owner_is_alive(lease.owner, self.pid_is_alive)
            and now - lease.last_heartbeat <= self.policy.stale_lease_seconds
        ]
        for key, health in list(state.health.items()):
            if (
                health.status == COOLDOWN
                and health.cooldown_until is not None
                and health.cooldown_until <= now
            ):
                state.health[key] = PoolHealth(
                    status=HEALTHY,
                    failure_count=health.failure_count,
                    last_error=health.last_error,
                    updated_at=now,
                    auth_fingerprint=health.auth_fingerprint,
                )

    def _candidate_list(
        self,
        profiles: Iterable[ProfileInfo],
        usages: dict[str, AccountUsage],
        state: PoolState,
        now: float,
    ) -> list[PoolCandidate]:
        lease_counts: dict[str, int] = {}
        for lease in state.leases:
            lease_counts[lease.profile] = lease_counts.get(lease.profile, 0) + 1
        candidates = []
        for profile in profiles:
            capacity = _usage_capacity(usages.get(profile.name))
            health = state.health.get(profile.name, PoolHealth())
            health = self._health_after_auth_change(
                profile,
                health,
                state,
                now,
            )
            if capacity is None or not _health_eligible(health, now):
                continue
            remaining, resets_at = capacity
            active_leases = lease_counts.get(profile.name, 0)
            candidates.append(
                PoolCandidate(
                    profile=profile.name,
                    remaining_percent=remaining,
                    effective_remaining=remaining / (active_leases + 1),
                    active_leases=active_leases,
                    resets_at=resets_at,
                    health_status=health.status,
                )
            )
        candidates.sort(
            key=lambda item: (
                -item.effective_remaining,
                -item.remaining_percent,
                state.selected_at.get(item.profile, 0.0),
                item.resets_at if item.resets_at is not None else float("inf"),
                item.profile,
            )
        )
        return candidates

    def reserve(
        self,
        profiles: list[ProfileInfo],
        usages: dict[str, AccountUsage],
        *,
        route_key: str | None = None,
        owner: str | None = None,
        now: float | None = None,
        allow_migrate: bool = False,
        recover_sticky: bool = False,
    ) -> PoolReservation:
        timestamp = time.time() if now is None else now
        owner_id = owner or f"pid:{os.getpid()}"
        lease_id = f"{owner_id}-{time.time_ns()}-{secrets.token_hex(4)}"
        with self.store.lock():
            state = self.load()
            self._prune(state, timestamp)
            candidates = self._candidate_list(profiles, usages, state, timestamp)
            if route_key and recover_sticky:
                # Reservation-stage recovery (used when a persisted sticky
                # route points at an unhealthy account) must be atomic with
                # the route update. If another worker has already migrated
                # the route while this request was fetching usage, keep that
                # newly healthy target instead of selecting a second account.
                route = state.routes.get(route_key)
                sticky = (
                    next(
                        (item for item in candidates if item.profile == route.profile),
                        None,
                    )
                    if route is not None
                    else None
                )
                candidate = sticky or (candidates[0] if candidates else None)
            elif route_key and not allow_migrate:
                route = state.routes.get(route_key)
                if route is not None:
                    sticky = next(
                        (item for item in candidates if item.profile == route.profile),
                        None,
                    )
                    if sticky is not None:
                        candidate = sticky
                    else:
                        raise AiAuthSwitchError(
                            f"sticky pool route {route_key!r} has no healthy account"
                        )
                else:
                    candidate = candidates[0] if candidates else None
            else:
                candidate = candidates[0] if candidates else None
            if candidate is None:
                raise AiAuthSwitchError(
                    "no eligible paid account is available in the pool"
                )
            lease = PoolLease(
                id=lease_id,
                profile=candidate.profile,
                owner=owner_id,
                started_at=timestamp,
                last_heartbeat=timestamp,
                route_key=route_key,
            )
            state.leases.append(lease)
            state.selected_at[candidate.profile] = timestamp
            if route_key:
                previous = state.routes.get(route_key)
                state.routes[route_key] = PoolRoute(
                    key=route_key,
                    profile=candidate.profile,
                    created_at=previous.created_at if previous else timestamp,
                    updated_at=timestamp,
                )
            self.save(state)
        return PoolReservation(
            lease_id=lease_id,
            profile=candidate.profile,
            route_key=route_key,
            remaining_percent=candidate.remaining_percent,
            effective_remaining=candidate.effective_remaining,
            resets_at=candidate.resets_at,
        )

    def heartbeat(
        self, reservation: PoolReservation, *, now: float | None = None
    ) -> None:
        timestamp = time.time() if now is None else now
        with self.store.lock():
            state = self.load()
            for index, lease in enumerate(state.leases):
                if lease.id == reservation.lease_id:
                    state.leases[index] = PoolLease(
                        id=lease.id,
                        profile=lease.profile,
                        owner=lease.owner,
                        started_at=lease.started_at,
                        last_heartbeat=timestamp,
                        route_key=lease.route_key,
                    )
                    self.save(state)
                    return

    def release(self, reservation: PoolReservation) -> None:
        with self.store.lock():
            state = self.load()
            state.leases = [
                lease for lease in state.leases if lease.id != reservation.lease_id
            ]
            self.save(state)

    def clear_route(self, route_key: str) -> None:
        with self.store.lock():
            state = self.load()
            state.routes.pop(route_key, None)
            self.save(state)

    def mark_success(self, profile: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        with self.store.lock():
            state = self.load()
            previous = state.health.get(profile, PoolHealth())
            state.health[profile] = PoolHealth(
                status=HEALTHY,
                failure_count=0,
                last_error=None,
                updated_at=timestamp,
                auth_fingerprint=self._auth_fingerprint_for_name(profile),
            )
            if previous.status != HEALTHY:
                state.selected_at.setdefault(profile, timestamp)
            self.save(state)

    def mark_failure(
        self,
        profile: str,
        kind: str,
        message: str,
        *,
        now: float | None = None,
        auth_fingerprint: str | None = None,
    ) -> None:
        timestamp = time.time() if now is None else now
        with self.store.lock():
            state = self.load()
            previous = state.health.get(profile, PoolHealth())
            count = previous.failure_count + 1
            fingerprint = (
                auth_fingerprint
                if auth_fingerprint is not None
                else self._auth_fingerprint_for_name(profile)
            )
            if kind in {"401", "403", "auth"}:
                health = PoolHealth(
                    status=AUTH_EXPIRED,
                    failure_count=count,
                    last_error=message,
                    updated_at=timestamp,
                    auth_fingerprint=fingerprint,
                )
            elif kind == "disabled":
                health = PoolHealth(
                    status=DISABLED,
                    failure_count=count,
                    last_error=message,
                    updated_at=timestamp,
                    auth_fingerprint=fingerprint,
                )
            else:
                cooldown = min(
                    self.policy.max_failure_cooldown_seconds,
                    self.policy.cooldown_seconds * (2 ** min(count - 1, 5)),
                )
                health = PoolHealth(
                    status=COOLDOWN,
                    failure_count=count,
                    cooldown_until=timestamp + cooldown,
                    last_error=message,
                    updated_at=timestamp,
                    auth_fingerprint=fingerprint,
                )
            state.health[profile] = health
            self.save(state)


def _pid_from_owner(owner: str) -> int:
    if owner.startswith("pid:"):
        try:
            return int(owner[4:])
        except ValueError:
            pass
    return -1


def _owner_is_alive(owner: str, pid_is_alive: Callable[[int], bool]) -> bool:
    pid = _pid_from_owner(owner)
    return pid > 0 and pid_is_alive(pid)
