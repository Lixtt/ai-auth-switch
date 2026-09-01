from __future__ import annotations

import json
import os
import selectors
import shutil
import sys
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, TextIO

from ai_auth_switch.app_server_router import GLOBAL_METHODS, AppServerRouteTable
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool import PoolCoordinator, PoolReservation, _health_eligible
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
        if route_key and not allow_migrate:
            reservation = self.coordinator.reserve(
                profiles,
                usages,
                route_key=route_key,
                allow_migrate=allow_migrate,
                recover_sticky=True,
                owner=f"pid:{os.getpid()}",
            )
        else:
            reservation = self.coordinator.reserve(
                profiles,
                usages,
                route_key=route_key,
                allow_migrate=allow_migrate,
                owner=f"pid:{os.getpid()}",
            )
        try:
            self._ensure_backend(reservation.profile)
        except BaseException:
            # A backend startup failure must not leave a persistent lease that
            # suppresses capacity for future app-server requests.
            self.coordinator.release(reservation)
            raise
        return reservation.profile, reservation

    def _ensure_backend(self, profile: str) -> BackendProcess:
        backend = self.backends.get(profile)
        if backend is not None and backend.running:
            return backend
        if backend is not None:
            # A backend can exit between selector iterations (or a caller can
            # observe it before the EOF event is delivered).  Retire the old
            # object before replacing it so its temporary CODEX_HOME and
            # process resources are not leaked and its stale stdout is not
            # left registered with the selector.
            self._retire_backend(profile, "backend was no longer running")
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

    def _backend_is_healthy(self, profile: str) -> bool:
        health = self.coordinator.load().health.get(profile)
        return health is None or _health_eligible(health, time.time())

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
        pending_key: tuple[str, str] | None = None
        if request_id is not None and track_response:
            pending_key = self._pending_key(profile, request_id)
            self.pending[pending_key] = _PendingClientRequest(
                client_id=request_id,
                method=str(message.get("method", "")),
                reservation=reservation,
            )
        try:
            backend.send(message)
        except BaseException as exc:
            # Do not leave an invisible lease or pending request behind when
            # a backend exits between selection and the write.  The caller can
            # return a scoped error while the next request reselects a worker.
            # Retiring the process also removes any other requests pinned to
            # it and prevents a broken-but-still-running object from being
            # reused on the next request.
            # Remove the failed request *before* retirement.  _retire_backend
            # emits errors for all remaining pending requests; leaving this
            # one in the map would make the run loop emit a duplicate response
            # when it reports the send exception to the client.
            pending = (
                self.pending.pop(pending_key, None)
                if pending_key is not None
                else None
            )
            if pending is not None and pending.reservation is not None:
                self.coordinator.release(pending.reservation)
            elif reservation is not None:
                self.coordinator.release(reservation)
            with suppress(Exception):
                self._retire_backend(profile, f"send failed: {exc}")
            raise

    def _handle_initialize(self, message: dict[str, Any]) -> None:
        control = self.routes.control_backend
        if control is None or not self._backend_is_healthy(control):
            previous_reservation = self.reservations.pop("__control__", None)
            if previous_reservation is not None:
                self.coordinator.release(previous_reservation)
            profile, reservation = self._select_backend(
                route_key="__control__",
                allow_migrate=control is not None,
            )
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
            # Only IDs generated by ``forward_server_request`` belong to the
            # pool's server-request namespace.  Client request IDs are valid
            # arbitrary strings, including values beginning with ``pool:``;
            # treating those as internal IDs would either misroute a normal
            # request or terminate the multiplexer on an unknown response.
            if request_id in self.routes.server_requests:
                backend, restored = self.routes.route_server_response(message)
                self._send_to_backend(backend, restored, track_response=False)
                return
            if "method" not in message:
                # An unsolicited response has no destination.  Ignore it and
                # keep the app-server session alive; this is preferable to
                # tearing down a user's connection because of an unrelated
                # client-generated ID.
                return
        if not isinstance(method, str):
            raise AiAuthSwitchError("client app-server message has no method")
        if method == "initialize":
            self._handle_initialize(message)
            return
        if method == "initialized":
            return
        # ``GLOBAL_METHODS`` (apart from initialize/initialized, handled
        # above, and thread/list, which is an aggregate operation) must use
        # the current control route.  Calling _handle_global here lets it
        # detect an expired/dead control backend and atomically migrate to a
        # healthy profile before forwarding account/config/model requests.
        if method in GLOBAL_METHODS and method != "thread/list":
            self._handle_global(message)
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
            backend_healthy = (
                backend is not None
                and backend.running
                and self._backend_is_healthy(profile)
            )
            if route_key and not backend_healthy:
                if backend is None or not backend.running:
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
        if (
            profile is None
            or backend is None
            or not backend.running
            or not self._backend_is_healthy(profile)
        ):
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

    def _retire_backend(
        self,
        profile: str,
        message: str,
        *,
        mark_failure: bool = True,
    ) -> None:
        """Stop and forget one backend without taking down the pool.

        Backend processes are independent account workers.  A malformed read,
        broken pipe, or ordinary process exit should fail only requests pinned
        to that worker and allow subsequent requests to select another account.
        Keeping this cleanup in one place also prevents stale selectors and
        temporary runtime directories when a dead backend is replaced before
        its EOF event is observed.
        """
        backend = self.backends.pop(profile, None)
        if backend is None:
            return
        if mark_failure:
            self.coordinator.mark_failure(profile, "process", message)
        self._fail_backend(profile, f"pool backend {profile} failed: {message}")
        stdout = backend.stdout
        if self._selector is not None and stdout is not None:
            with suppress(KeyError, ValueError):
                self._selector.unregister(stdout)
        with suppress(Exception):
            backend.stop()

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
                        try:
                            self._handle_client_message(message)
                        except Exception as exc:
                            # A single backend write/selection failure must
                            # not terminate the shared pool process.  Return a
                            # JSON-RPC error for this request and let later
                            # requests select a healthy account.
                            request_id = message.get("id")
                            if request_id is not None:
                                self._write(
                                    {
                                        "id": request_id,
                                        "error": {
                                            "code": -32005,
                                            "message": str(exc),
                                        },
                                    }
                                )
                    else:
                        profile = key.data
                        backend = self.backends.get(profile)
                        if backend is None:
                            continue
                        try:
                            message = backend.read_message(timeout=0.1)
                        except AiAuthSwitchError as exc:
                            self._retire_backend(profile, str(exc))
                            continue
                        if message is None:
                            self._retire_backend(profile, "backend exited")
                            continue
                        try:
                            self._handle_backend_message(profile, message)
                        except AiAuthSwitchError as exc:
                            # A protocol error from one account must not make
                            # all other account workers unavailable.
                            self._retire_backend(profile, str(exc))
                            continue
        finally:
            if self._selector is not None:
                self._selector.close()
            for reservation in list(self.reservations.values()):
                self.coordinator.release(reservation)
            for backend in list(self.backends.values()):
                backend.stop()
            self.backends.clear()
        return 0
