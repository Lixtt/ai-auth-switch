from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError

THREAD_ID_METHODS = {
    "thread/resume",
    "thread/read",
    "thread/metadata/update",
    "thread/goal/set",
    "thread/goal/get",
    "thread/goal/clear",
    "thread/compact/start",
    "thread/rollback",
    "thread/inject_items",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
}
GLOBAL_METHODS = {
    "initialize",
    "initialized",
    "account/read",
    "account/logout",
    "account/rateLimits/read",
    "model/list",
    "config/read",
    "configRequirements/read",
    "experimentalFeature/list",
    "permissionProfile/list",
    "thread/list",
}


def thread_route_key(thread_id: str) -> str:
    return f"thread:{thread_id}"


@dataclass(frozen=True)
class RouterPlan:
    kind: str
    backend: str | None = None
    route_key: str | None = None
    aggregate: bool = False


@dataclass(frozen=True)
class BackendServerRequest:
    backend: str
    original_id: str | int | float | None
    forwarded_id: str
    message: dict[str, Any]


@dataclass
class AppServerRouteTable:
    control_backend: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    pending_requests: dict[str, str] = field(default_factory=dict)
    server_requests: dict[str, BackendServerRequest] = field(default_factory=dict)

    def bind_thread(self, thread_id: str, backend: str) -> None:
        if not thread_id or not backend:
            raise ValueError("thread_id and backend are required")
        self.routes[thread_route_key(thread_id)] = backend
        if self.control_backend is None:
            self.control_backend = backend

    def bind_route(self, route_key: str, backend: str) -> None:
        if not route_key or not backend:
            raise ValueError("route_key and backend are required")
        self.routes[route_key] = backend

    def backend_for_thread(self, thread_id: str) -> str | None:
        return self.routes.get(thread_route_key(thread_id))

    def plan_request(
        self,
        method: str,
        params: object,
        request_id: str | int | float | None,
    ) -> RouterPlan:
        if method in {"initialize", "initialized"}:
            return RouterPlan(kind="local")
        if method == "thread/start":
            return RouterPlan(kind="select", route_key=None)
        if method == "thread/fork":
            thread_id = _thread_id(params)
            backend = self.backend_for_thread(thread_id) if thread_id else None
            if backend is None:
                return (
                    RouterPlan(kind="discover", route_key=thread_route_key(thread_id))
                    if thread_id
                    else RouterPlan(kind="error")
                )
            self._remember_request(request_id, backend)
            return RouterPlan(
                kind="backend", backend=backend, route_key=thread_route_key(thread_id)
            )
        if method in THREAD_ID_METHODS:
            thread_id = _thread_id(params)
            backend = self.backend_for_thread(thread_id) if thread_id else None
            if backend is None:
                return (
                    RouterPlan(kind="discover", route_key=thread_route_key(thread_id))
                    if thread_id
                    else RouterPlan(kind="error")
                )
            self._remember_request(request_id, backend)
            return RouterPlan(
                kind="backend", backend=backend, route_key=thread_route_key(thread_id)
            )
        if method == "thread/list":
            return RouterPlan(kind="aggregate", aggregate=True)
        if method in GLOBAL_METHODS:
            return RouterPlan(kind="backend", backend=self.control_backend)
        return RouterPlan(kind="backend", backend=self.control_backend)

    def assign_new_thread(
        self,
        request_id: str | int | float | None,
        backend: str,
    ) -> RouterPlan:
        self._remember_request(request_id, backend)
        return RouterPlan(kind="backend", backend=backend)

    def record_backend_response(
        self,
        backend: str,
        request_id: str | int | float | None,
        response: dict[str, Any],
    ) -> None:
        expected_backend = self.pending_requests.pop(_id_key(request_id), None)
        if expected_backend is not None and expected_backend != backend:
            raise AiAuthSwitchError(
                f"request {request_id!r} returned from unexpected backend {backend!r}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            return
        thread = result.get("thread")
        if isinstance(thread, dict):
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self.bind_thread(thread_id, backend)

    def record_backend_notification(
        self,
        backend: str,
        message: dict[str, Any],
    ) -> None:
        if message.get("method") != "thread/started":
            return
        params = message.get("params")
        thread = params.get("thread") if isinstance(params, dict) else None
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if isinstance(thread_id, str):
            self.bind_thread(thread_id, backend)

    def forward_server_request(
        self,
        backend: str,
        message: dict[str, Any],
    ) -> BackendServerRequest:
        original_id = message.get("id")
        forwarded_id = f"pool:{backend}:{_id_key(original_id)}"
        forwarded = dict(message)
        forwarded["id"] = forwarded_id
        request = BackendServerRequest(
            backend=backend,
            original_id=original_id,
            forwarded_id=forwarded_id,
            message=forwarded,
        )
        self.server_requests[forwarded_id] = request
        return request

    def route_server_response(
        self,
        message: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        forwarded_id = message.get("id")
        if not isinstance(forwarded_id, str):
            raise AiAuthSwitchError("server response has no pool request id")
        request = self.server_requests.pop(forwarded_id, None)
        if request is None:
            raise AiAuthSwitchError(f"unknown pool server request id: {forwarded_id}")
        restored = dict(message)
        restored["id"] = request.original_id
        return request.backend, restored

    def _remember_request(
        self,
        request_id: str | int | float | None,
        backend: str,
    ) -> None:
        if request_id is not None:
            self.pending_requests[_id_key(request_id)] = backend


def _id_key(value: str | int | float | None) -> str:
    return f"{type(value).__name__}:{value!s}"


def _thread_id(params: object) -> str | None:
    if not isinstance(params, dict):
        return None
    value = params.get("threadId")
    return value if isinstance(value, str) and value else None
