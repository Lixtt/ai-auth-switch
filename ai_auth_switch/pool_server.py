from __future__ import annotations

import json
import os
import selectors
import shutil
import sys
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TextIO

from ai_auth_switch.app_server_router import AppServerRouteTable
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool import PoolCoordinator, PoolReservation
from ai_auth_switch.pool_backend import BackendProcess
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, fetch_profile_usage


@dataclass(frozen=True)
class PoolServerConfig:
    usage_timeout: float = 5.0
    usage_workers: int = 4
    usage_cache_ttl: float = 60.0
    refresh_usage: bool = False
    backend_timeout: float = 10.0


@dataclass
class _PendingClientRequest:
    client_id: str | int | float | None
    method: str
    reservation: PoolReservation | None = None


@dataclass
class _AggregateRequest:
    client_id: str | int | float | None
    expected: int
    responses: list[dict[str, Any]]


class PoolAppServer:
    """JSONL app-server multiplexer backed by profile-isolated Codex servers."""

    def __init__(
        self,
        store: AuthStore,
        provider: Provider,
        *,
        command: Iterable[str] | None = None,
        config: PoolServerConfig | None = None,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        backend_factory: Callable[..., BackendProcess] = BackendProcess,
        usage_fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
    ):
        self.store = store
        self.provider = provider
        self.config = config or PoolServerConfig()
        self.input_stream = input_stream or sys.stdin
        self.output_stream = output_stream or sys.stdout
        self.command = tuple(command or self._default_command())
        self.backend_factory = backend_factory
        self.usage_fetcher = usage_fetcher
        self.coordinator = PoolCoordinator(store, provider)
        self.routes = AppServerRouteTable()
        self.backends: dict[str, BackendProcess] = {}
        self.pending: dict[tuple[str, str], _PendingClientRequest] = {}
        self.aggregate_pending: dict[tuple[str, str], _AggregateRequest] = {}
        self.reservations: dict[str, PoolReservation] = {}
        self._selector: selectors.BaseSelector | None = None
        self._stopping = False

    def _default_command(self) -> list[str]:
        configured = os.environ.get("AI_AUTH_SWITCH_POOL_CODEX_BIN")
        if configured and configured.strip():
            return [configured]
        resolved = shutil.which("codex")
        return [resolved or "codex"]

    def _write(self, message: dict[str, Any]) -> None:
        self.output_stream.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.output_stream.flush()

    def _profiles_and_usage(self) -> tuple[list[ProfileInfo], dict[str, AccountUsage]]:
        profiles = self.store.list_profiles(self.provider)
        if not profiles:
            raise AiAuthSwitchError("no saved Codex profiles")
        usages = self.usage_fetcher(
            ((profile.name, profile.path) for profile in profiles),
            timeout=self.config.usage_timeout,
            workers=self.config.usage_workers,
            cache_dir=self.store.base_dir / "cache" / "usage" / self.provider.id,
            cache_ttl=self.config.usage_cache_ttl,
            refresh=self.config.refresh_usage,
        )
        return profiles, usages

    def _select_backend(
        self, *, route_key: str | None = None, allow_migrate: bool = False
    ) -> tuple[str, PoolReservation]:
        profiles, usages = self._profiles_and_usage()
        reservation = self.coordinator.reserve(
            profiles,
            usages,
            route_key=route_key,
            allow_migrate=allow_migrate,
            owner=f"pid:{os.getpid()}",
        )
        self._ensure_backend(reservation.profile)
        return reservation.profile, reservation

    def _ensure_backend(self, profile: str) -> BackendProcess:
        backend = self.backends.get(profile)
        if backend is not None and backend.running:
            return backend
        backend = self.backend_factory(
            self.store,
            self.provider,
            profile,
            command=self.command,
            timeout=self.config.backend_timeout,
        )
        backend.start()
        self.backends[profile] = backend
        if self._selector is not None and backend.stdout is not None:
            self._selector.register(backend.stdout, selectors.EVENT_READ, profile)
        return backend

    def _pending_key(self, profile: str, request_id: object) -> tuple[str, str]:
        return profile, f"{type(request_id).__name__}:{request_id!s}"

    def _send_to_backend(
        self,
        profile: str,
        message: dict[str, Any],
        *,
        reservation: PoolReservation | None = None,
        track_response: bool = True,
    ) -> None:
        backend = self._ensure_backend(profile)
        request_id = message.get("id")
        if request_id is not None and track_response:
            self.pending[self._pending_key(profile, request_id)] = (
                _PendingClientRequest(
                    client_id=request_id,
                    method=str(message.get("method", "")),
                    reservation=reservation,
                )
            )
        backend.send(message)

    def _handle_initialize(self, message: dict[str, Any]) -> None:
        if self.routes.control_backend is None:
            profile, reservation = self._select_backend(route_key="__control__")
            self.routes.control_backend = profile
            self.reservations["__control__"] = reservation
        backend = self._ensure_backend(self.routes.control_backend)
        result = backend.initial_result
        self._write({"id": message.get("id"), "result": result})

    def _handle_client_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        # Server-initiated requests are answered by the client with a result
        # (and no method).  Restore the backend-local id before forwarding it.
        if isinstance(request_id, str) and request_id.startswith("pool:"):
            backend, restored = self.routes.route_server_response(message)
            self._send_to_backend(backend, restored, track_response=False)
            return
        if not isinstance(method, str):
            raise AiAuthSwitchError("client app-server message has no method")
        if method == "initialize":
            self._handle_initialize(message)
            return
        if method == "initialized":
            return
        params = message.get("params")
        if method == "thread/start":
            profile, reservation = self._select_backend()
            self.routes.assign_new_thread(request_id, profile)
            self._send_to_backend(profile, message, reservation=reservation)
            return
        plan = self.routes.plan_request(method, params, request_id)
        if plan.kind == "local":
            return
        if plan.kind == "aggregate":
            self._handle_global(message)
            return
        if plan.kind in {"discover", "error"}:
            self._write(
                {
                    "id": request_id,
                    "error": {
                        "code": -32004,
                        "message": "pool has no backend route for this thread",
                    },
                }
            )
            return
        profile = plan.backend or self.routes.control_backend
        if profile is None:
            profile, reservation = self._select_backend()
        else:
            route_key = plan.route_key
            backend = self.backends.get(profile)
            if route_key and (backend is None or not backend.running):
                self.coordinator.mark_failure(
                    profile, "process", "backend unavailable for sticky route"
                )
                profile, reservation = self._select_backend(
                    route_key=route_key, allow_migrate=True
                )
                self.routes.bind_route(route_key, profile)
            else:
                reservation = None
        self._send_to_backend(profile, message, reservation=reservation)

    def _handle_global(self, message: dict[str, Any]) -> None:
        profile = self.routes.control_backend
        backend = self.backends.get(profile) if profile else None
        if profile is None or backend is None or not backend.running:
            previous_reservation = self.reservations.pop("__control__", None)
            if previous_reservation is not None:
                self.coordinator.release(previous_reservation)
            profile, reservation = self._select_backend(
                route_key="__control__", allow_migrate=profile is not None
            )
            self.routes.control_backend = profile
            self.reservations["__control__"] = reservation
        if message.get("method") != "thread/list":
            self._send_to_backend(profile, message)
            return
        backends = [backend for backend in self.backends.values() if backend.running]
        if not backends:
            self._send_to_backend(profile, message)
            return
        client_id = message.get("id")
        aggregate = _AggregateRequest(client_id, len(backends), [])
        for index, backend in enumerate(backends):
            internal_id = f"pool-list:{client_id!s}:{index}:{backend.profile}"
            self.aggregate_pending[self._pending_key(backend.profile, internal_id)] = (
                aggregate
            )
            forwarded = dict(message)
            forwarded["id"] = internal_id
            backend.send(forwarded)

    def _finish_aggregate(self, aggregate: _AggregateRequest) -> dict[str, Any]:
        results = [item.get("result") for item in aggregate.responses]
        result_dicts = [item for item in results if isinstance(item, dict)]
        combined: dict[str, Any] = {}
        for result in result_dicts:
            for key, value in result.items():
                if isinstance(value, list):
                    current = combined.setdefault(key, [])
                    if isinstance(current, list):
                        current.extend(value)
                elif key not in combined:
                    combined[key] = value
        for key in ("data", "threads"):
            values = combined.get(key)
            if isinstance(values, list):
                seen: set[str] = set()
                unique = []
                for item in values:
                    item_id = item.get("id") if isinstance(item, dict) else None
                    marker = str(item_id) if item_id is not None else repr(item)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    unique.append(item)
                combined[key] = unique
        error = next(
            (item.get("error") for item in aggregate.responses if item.get("error")),
            None,
        )
        return (
            {
                "id": aggregate.client_id,
                "error": error,
            }
            if error and not result_dicts
            else {"id": aggregate.client_id, "result": combined}
        )

    def _fail_backend(self, profile: str, message: str) -> None:
        for key, pending in list(self.pending.items()):
            if key[0] != profile:
                continue
            self.pending.pop(key, None)
            self._write(
                {
                    "id": pending.client_id,
                    "error": {"code": -32005, "message": message},
                }
            )
            if pending.reservation is not None:
                self.coordinator.release(pending.reservation)
        for key, aggregate in list(self.aggregate_pending.items()):
            if key[0] != profile:
                continue
            self.aggregate_pending.pop(key, None)
            aggregate.responses.append(
                {
                    "id": aggregate.client_id,
                    "error": {"code": -32005, "message": message},
                }
            )
            aggregate.expected -= 1
            if aggregate.expected == 0:
                self._write(self._finish_aggregate(aggregate))

    def _handle_backend_message(self, profile: str, message: dict[str, Any]) -> None:
        if "id" in message and ("result" in message or "error" in message):
            request_id = message.get("id")
            aggregate = self.aggregate_pending.pop(
                self._pending_key(profile, request_id), None
            )
            if aggregate is not None:
                aggregate.responses.append(message)
                aggregate.expected -= 1
                if aggregate.expected == 0:
                    self._write(self._finish_aggregate(aggregate))
                return
            pending = self.pending.pop(self._pending_key(profile, request_id), None)
            self.routes.record_backend_response(profile, request_id, message)
            self._write(message)
            if pending is not None and pending.reservation is not None:
                self.coordinator.release(pending.reservation)
            if "error" in message:
                error = message.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                kind = str(code) if code in {401, 403} else "backend"
                self.coordinator.mark_failure(profile, kind, str(error))
            else:
                self.coordinator.mark_success(profile)
            return
        if "id" in message and "method" in message:
            forwarded = self.routes.forward_server_request(profile, message)
            self._write(forwarded.message)
            return
        self.routes.record_backend_notification(profile, message)
        self._write(message)

    def run(self) -> int:
        if self.provider.id != "codex":
            raise AiAuthSwitchError("pool app-server currently supports codex only")
        self._selector = selectors.DefaultSelector()
        self._selector.register(self.input_stream, selectors.EVENT_READ, "client")
        try:
            while not self._stopping:
                events = self._selector.select(1.0)
                if not events:
                    continue
                for key, _mask in events:
                    if key.data == "client":
                        line = self.input_stream.readline()
                        if not line:
                            self._stopping = True
                            break
                        if not line.strip():
                            continue
                        try:
                            message = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise AiAuthSwitchError("client sent invalid JSON") from exc
                        if not isinstance(message, dict):
                            raise AiAuthSwitchError(
                                "client sent a non-object JSON message"
                            )
                        self._handle_client_message(message)
                    else:
                        profile = key.data
                        backend = self.backends.get(profile)
                        if backend is None:
                            continue
                        message = backend.read_message(timeout=0.1)
                        if message is None:
                            error_message = f"pool backend {profile} exited"
                            self.coordinator.mark_failure(
                                profile, "process", "backend exited"
                            )
                            self._fail_backend(profile, error_message)
                            with suppress(KeyError, ValueError):
                                self._selector.unregister(backend.stdout)
                            backend.stop()
                            self.backends.pop(profile, None)
                            continue
                        self._handle_backend_message(profile, message)
        finally:
            if self._selector is not None:
                self._selector.close()
            for reservation in list(self.reservations.values()):
                self.coordinator.release(reservation)
            for backend in list(self.backends.values()):
                backend.stop()
            self.backends.clear()
        return 0
