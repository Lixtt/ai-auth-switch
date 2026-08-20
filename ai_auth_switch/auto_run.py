from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, fetch_profile_usage, is_free_plan
from ai_auth_switch.utils import atomic_write


@dataclass(frozen=True)
class AutoRunConfig:
    usage_timeout: float = 5.0
    usage_workers: int = 4
    usage_cache_ttl: float = 60.0
    refresh_usage: bool = False


@dataclass(frozen=True)
class RunLease:
    id: str
    pid: int
    profile: str
    started_at: float

    @classmethod
    def from_dict(cls, value: object) -> RunLease | None:
        if not isinstance(value, dict):
            return None
        lease_id = value.get("id")
        pid = value.get("pid")
        profile = value.get("profile")
        started_at = value.get("started_at")
        if (
            not isinstance(lease_id, str)
            or not lease_id
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(profile, str)
            or not profile
            or not isinstance(started_at, (int, float))
        ):
            return None
        return cls(lease_id, pid, profile, float(started_at))

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "pid": self.pid,
            "profile": self.profile,
            "started_at": self.started_at,
        }


@dataclass
class AutoRunState:
    leases: list[RunLease]
    selected_at: dict[str, float]

    @classmethod
    def empty(cls) -> AutoRunState:
        return cls(leases=[], selected_at={})

    @classmethod
    def from_dict(cls, value: object) -> AutoRunState:
        if not isinstance(value, dict):
            return cls.empty()
        raw_leases = value.get("leases")
        leases = []
        if isinstance(raw_leases, list):
            for raw in raw_leases:
                lease = RunLease.from_dict(raw)
                if lease is not None:
                    leases.append(lease)
        raw_selected = value.get("selected_at")
        selected_at = (
            {
                name: float(timestamp)
                for name, timestamp in raw_selected.items()
                if isinstance(name, str) and isinstance(timestamp, (int, float))
            }
            if isinstance(raw_selected, dict)
            else {}
        )
        return cls(leases=leases, selected_at=selected_at)

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "leases": [lease.to_dict() for lease in self.leases],
            "selected_at": dict(sorted(self.selected_at.items())),
        }


@dataclass(frozen=True)
class AutoRunSelection:
    profile: str
    remaining_percent: float
    effective_remaining: float
    active_leases: int
    resets_at: int | None
    lease_id: str


@dataclass(frozen=True)
class _Candidate:
    profile: str
    remaining_percent: float
    effective_remaining: float
    active_leases: int
    resets_at: int | None
    last_selected_at: float


def auto_run_state_path(store: AuthStore, provider: Provider) -> Path:
    return store.base_dir / "scheduler" / f"{provider.id}-auto-run.json"


def _read_state(path: Path) -> AutoRunState:
    try:
        return AutoRunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return AutoRunState.empty()


def _write_state(path: Path, state: AutoRunState) -> None:
    atomic_write(path, json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n")


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _prune_leases(
    state: AutoRunState,
    *,
    current_pid: int,
    pid_is_alive: Callable[[int], bool],
) -> None:
    state.leases = [
        lease
        for lease in state.leases
        if lease.pid != current_pid and pid_is_alive(lease.pid)
    ]


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


def choose_auto_run_profile(
    profiles: list[ProfileInfo],
    usages: dict[str, AccountUsage],
    state: AutoRunState,
) -> _Candidate:
    lease_counts: dict[str, int] = {}
    for lease in state.leases:
        lease_counts[lease.profile] = lease_counts.get(lease.profile, 0) + 1
    candidates = []
    for profile in profiles:
        capacity = _usage_capacity(usages.get(profile.name))
        if capacity is None:
            continue
        remaining, resets_at = capacity
        active_leases = lease_counts.get(profile.name, 0)
        candidates.append(
            _Candidate(
                profile=profile.name,
                remaining_percent=remaining,
                effective_remaining=remaining / (active_leases + 1),
                active_leases=active_leases,
                resets_at=resets_at,
                last_selected_at=state.selected_at.get(profile.name, 0.0),
            )
        )
    if not candidates:
        raise AiAuthSwitchError(
            "no saved paid Codex account has both valid authentication and usable quota"
        )
    candidates.sort(
        key=lambda candidate: (
            -candidate.effective_remaining,
            -candidate.remaining_percent,
            candidate.last_selected_at,
            candidate.resets_at if candidate.resets_at is not None else float("inf"),
            candidate.profile,
        )
    )
    return candidates[0]


@contextmanager
def acquire_auto_run_profile(
    store: AuthStore,
    provider: Provider,
    config: AutoRunConfig | None = None,
    *,
    fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
    pid: int | None = None,
    now: float | None = None,
    pid_is_alive: Callable[[int], bool] = _pid_is_alive,
) -> Iterator[AutoRunSelection]:
    if provider.id != "codex":
        raise AiAuthSwitchError("automatic run selection currently supports codex only")
    policy = config or AutoRunConfig()
    profiles = store.list_profiles(provider)
    if not profiles:
        raise AiAuthSwitchError("no saved Codex profiles")
    usages = fetcher(
        ((profile.name, profile.path) for profile in profiles),
        timeout=policy.usage_timeout,
        workers=policy.usage_workers,
        cache_dir=store.base_dir / "cache" / "usage" / provider.id,
        cache_ttl=policy.usage_cache_ttl,
        refresh=policy.refresh_usage,
    )
    process_id = os.getpid() if pid is None else pid
    timestamp = time.time() if now is None else now
    path = auto_run_state_path(store, provider)
    lease_id = f"{process_id}-{time.time_ns()}-{secrets.token_hex(4)}"
    with store.lock():
        state = _read_state(path)
        _prune_leases(
            state,
            current_pid=process_id,
            pid_is_alive=pid_is_alive,
        )
        candidate = choose_auto_run_profile(profiles, usages, state)
        lease = RunLease(
            id=lease_id,
            pid=process_id,
            profile=candidate.profile,
            started_at=timestamp,
        )
        state.leases.append(lease)
        state.selected_at[candidate.profile] = timestamp
        _write_state(path, state)
    selection = AutoRunSelection(
        profile=candidate.profile,
        remaining_percent=candidate.remaining_percent,
        effective_remaining=candidate.effective_remaining,
        active_leases=candidate.active_leases,
        resets_at=candidate.resets_at,
        lease_id=lease_id,
    )
    try:
        yield selection
    finally:
        with store.lock():
            state = _read_state(path)
            state.leases = [lease for lease in state.leases if lease.id != lease_id]
            _write_state(path, state)
