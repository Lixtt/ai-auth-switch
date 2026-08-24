from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool import PoolCoordinator, PoolReservation
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, fetch_profile_usage
from ai_auth_switch.utils import (
    atomic_write,
    extract_account_id_from_jwt,
    set_private_permissions,
)

DEFAULT_RESPONSES_UPSTREAM = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 8765
MAX_REQUEST_BYTES = 32 * 1024 * 1024
RETRYABLE_STATUS_CODES = {401, 403, 408, 409, 429, 500, 502, 503, 504}
LOCAL_AUTH_HEADER = "X-AI-Auth-Switch-Token"
LOCAL_ROUTE_HEADER = "X-AI-Auth-Switch-Route"


@dataclass(frozen=True)
class ResponsesProxyConfig:
    upstream_url: str = DEFAULT_RESPONSES_UPSTREAM
    host: str = DEFAULT_LISTEN_HOST
    port: int = DEFAULT_LISTEN_PORT
    usage_timeout: float = 5.0
    usage_workers: int = 4
    usage_cache_ttl: float = 60.0
    refresh_usage: bool = False
    max_retries: int = 2
    request_timeout: float = 120.0
    token_file: Path | None = None


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def default_token_path(store: AuthStore) -> Path:
    return store.base_dir / "pool" / "responses.token"


def ensure_listener_token(path: Path) -> str:
    if path.is_symlink():
        raise AiAuthSwitchError(f"refusing symlinked pool token file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    set_private_permissions(path.parent)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if value and all(
        char.isascii() and char.isalnum() or char in "-_" for char in value
    ):
        set_private_permissions(path)
        return value
    value = secrets.token_urlsafe(32)
    atomic_write(path, value + "\n")
    set_private_permissions(path)
    return value


def _is_loopback(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return all(
            ip_address(result[4][0]).is_loopback
            for result in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        )
    except OSError:
        return False


def _profile_auth(path: Path) -> tuple[str | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    token = tokens.get("access_token") or data.get("access_token")
    account_id = tokens.get("account_id") or data.get("account_id")
    clean_token = token.strip() if isinstance(token, str) and token.strip() else None
    clean_account = (
        account_id.strip()
        if isinstance(account_id, str) and account_id.strip()
        else None
    )
    if clean_account is None and clean_token is not None:
        clean_account = extract_account_id_from_jwt(clean_token)
    return clean_token, clean_account


def _request_headers(
    headers: dict[str, str], token: str, account_id: str | None
) -> dict[str, str]:
    excluded = {
        "authorization",
        "content-length",
        "connection",
        "host",
        LOCAL_AUTH_HEADER.casefold(),
        LOCAL_ROUTE_HEADER.casefold(),
    }
    forwarded = {
        key: value for key, value in headers.items() if key.casefold() not in excluded
    }
    forwarded["Authorization"] = f"Bearer {token}"
    if account_id:
        forwarded["ChatGPT-Account-Id"] = account_id
    forwarded.setdefault("Accept", "text/event-stream, application/json")
    forwarded.setdefault("Content-Type", "application/json")
    return forwarded


def request_route_key(body: bytes, supplied: str | None = None) -> str | None:
    value: object = supplied.strip() if supplied and supplied.strip() else None
    if value is None:
        try:
            payload = json.loads(body)
        except (ValueError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            metadata = payload.get("metadata")
            candidates = (
                metadata.get("thread_id") if isinstance(metadata, dict) else None,
                payload.get("prompt_cache_key"),
                payload.get("conversation_id"),
                payload.get("previous_response_id"),
            )
            value = next(
                (
                    candidate
                    for candidate in candidates
                    if isinstance(candidate, str) and candidate
                ),
                None,
            )
    if not isinstance(value, str):
        return None
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"request:{digest}"


class PoolResponsesProxy:
    def __init__(
        self,
        store: AuthStore,
        provider: Provider,
        *,
        config: ResponsesProxyConfig | None = None,
        opener: Callable[..., Any] = urllib.request.urlopen,
        usage_fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
    ):
        if provider.id != "codex":
            raise AiAuthSwitchError("Responses pool currently supports codex only")
        self.store = store
        self.provider = provider
        self.config = config or ResponsesProxyConfig()
        if not _is_loopback(self.config.host):
            raise AiAuthSwitchError(
                "pool Responses listener must be loopback-only "
                "(127.0.0.1, ::1, or localhost)"
            )
        if not self.config.upstream_url.startswith(("http://", "https://")):
            raise AiAuthSwitchError("pool Responses upstream URL must be HTTP or HTTPS")
        self.opener = opener
        self.usage_fetcher = usage_fetcher
        self.coordinator = PoolCoordinator(store, provider)
        self.token_path = self.config.token_file or default_token_path(store)
        self.listener_token = ensure_listener_token(self.token_path)
        self._httpd: ThreadingHTTPServer | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._httpd is None:
            return None
        host, port = self._httpd.server_address[:2]
        return host, port

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

    def _reserve(
        self, route_key: str | None, *, migrate: bool = False
    ) -> PoolReservation:
        profiles, usages = self._profiles_and_usage()
        return self.coordinator.reserve(
            profiles,
            usages,
            route_key=route_key,
            allow_migrate=migrate,
            owner=f"pid:{os.getpid()}",
        )

    def _open_upstream(
        self,
        body: bytes,
        headers: dict[str, str],
        reservation: PoolReservation,
    ) -> Any:
        token, account_id = _profile_auth(
            self.store.profile_path(self.provider, reservation.profile)
        )
        if not token:
            raise AiAuthSwitchError(
                f"profile {reservation.profile!r} has no access token"
            )
        request = urllib.request.Request(
            self.config.upstream_url,
            data=body,
            headers=_request_headers(headers, token, account_id),
            method="POST",
        )
        return self.opener(request, timeout=self.config.request_timeout)

    def forward(
        self,
        body: bytes,
        headers: dict[str, str],
        *,
        route_key: str | None = None,
    ) -> ProxyResponse:
        chunks: list[bytes] = []
        metadata: list[tuple[int, dict[str, str]]] = []

        def on_headers(status: int, response_headers: dict[str, str]) -> None:
            metadata.append((status, response_headers))

        self.stream(
            body,
            headers,
            route_key=route_key,
            on_headers=on_headers,
            on_chunk=chunks.append,
        )
        if not metadata:
            return ProxyResponse(
                502,
                {"Content-Type": "application/json"},
                b'{"error":"upstream request failed"}',
            )
        status, response_headers = metadata[0]
        return ProxyResponse(status, response_headers, b"".join(chunks))

    def stream(
        self,
        body: bytes,
        headers: dict[str, str],
        *,
        route_key: str | None = None,
        on_headers: Callable[[int, dict[str, str]], None],
        on_chunk: Callable[[bytes], None],
    ) -> None:
        if len(body) > MAX_REQUEST_BYTES:
            on_headers(413, {"Content-Type": "application/json"})
            on_chunk(b'{"error":"request too large"}')
            return
        last_error: str | None = None
        last_retry_response: tuple[int, dict[str, str], bytes] | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                reservation = self._reserve(route_key, migrate=attempt > 0)
            except AiAuthSwitchError:
                if last_retry_response is not None:
                    status, response_headers, retry_body = last_retry_response
                    on_headers(status, response_headers)
                    on_chunk(retry_body)
                    return
                raise
            response = None
            headers_sent = False
            try:
                try:
                    response = self._open_upstream(body, headers, reservation)
                    status_value = getattr(response, "status", None)
                    status = int(
                        status_value if status_value is not None else response.getcode()
                    )
                    response_headers = {
                        key: value
                        for key, value in response.headers.items()
                        if key.casefold()
                        not in {"content-length", "transfer-encoding", "connection"}
                    }
                except AiAuthSwitchError as exc:
                    last_error = str(exc)
                    self.coordinator.mark_failure(
                        reservation.profile, "auth", last_error
                    )
                    if attempt < self.config.max_retries:
                        continue
                    on_headers(503, {"Content-Type": "application/json"})
                    on_chunk(b'{"error":"profile authentication is unavailable"}')
                    return
                except urllib.error.HTTPError as exc:
                    status = int(exc.code)
                    response_headers = {
                        key: value
                        for key, value in exc.headers.items()
                        if key.casefold()
                        not in {"content-length", "transfer-encoding", "connection"}
                    }
                    error_body = exc.read(MAX_REQUEST_BYTES)
                    if (
                        status in RETRYABLE_STATUS_CODES
                        and attempt < self.config.max_retries
                    ):
                        last_retry_response = (status, response_headers, error_body)
                        kind = "401" if status == 401 else "upstream"
                        self.coordinator.mark_failure(
                            reservation.profile, kind, f"upstream HTTP {status}"
                        )
                        continue
                    if status in RETRYABLE_STATUS_CODES:
                        kind = "401" if status == 401 else "upstream"
                        self.coordinator.mark_failure(
                            reservation.profile, kind, f"upstream HTTP {status}"
                        )
                    on_headers(status, response_headers)
                    on_chunk(error_body)
                    return
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = str(getattr(exc, "reason", exc))
                    self.coordinator.mark_failure(
                        reservation.profile, "network", last_error
                    )
                    if attempt < self.config.max_retries:
                        continue
                    on_headers(502, {"Content-Type": "application/json"})
                    on_chunk(b'{"error":"upstream request failed"}')
                    return
                if (
                    status in RETRYABLE_STATUS_CODES
                    and attempt < self.config.max_retries
                ):
                    retry_body = response.read(MAX_REQUEST_BYTES)
                    last_retry_response = (status, response_headers, retry_body)
                    kind = "401" if status == 401 else "upstream"
                    self.coordinator.mark_failure(
                        reservation.profile, kind, f"upstream HTTP {status}"
                    )
                    continue
                headers_sent = True
                on_headers(status, response_headers)
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    on_chunk(chunk)
                self.coordinator.mark_success(reservation.profile)
                return
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(getattr(exc, "reason", exc))
                self.coordinator.mark_failure(
                    reservation.profile, "network", last_error
                )
                if not headers_sent and attempt < self.config.max_retries:
                    continue
                if not headers_sent:
                    on_headers(502, {"Content-Type": "application/json"})
                    on_chunk(json.dumps({"error": last_error}).encode())
                return
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                self.coordinator.release(reservation)

    def _handler_class(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                if self.path not in {"/responses", "/v1/responses"}:
                    self.send_error(404)
                    return
                if (
                    self.headers.get("Authorization", "")
                    != f"Bearer {proxy.listener_token}"
                ):
                    self.send_error(401)
                    return
                raw_length = self.headers.get("Content-Length")
                try:
                    length = int(raw_length or "0")
                except ValueError:
                    self.send_error(400)
                    return
                if length < 0 or length > MAX_REQUEST_BYTES:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                route_key = request_route_key(
                    body, self.headers.get(LOCAL_ROUTE_HEADER)
                )
                started = False

                def on_headers(status: int, response_headers: dict[str, str]) -> None:
                    nonlocal started
                    started = True
                    self.close_connection = True
                    self.send_response(status)
                    for key, value in response_headers.items():
                        if key.casefold() not in {"server", "date"}:
                            self.send_header(key, value)
                    self.send_header("Connection", "close")
                    self.end_headers()

                def on_chunk(chunk: bytes) -> None:
                    self.wfile.write(chunk)
                    self.wfile.flush()

                try:
                    proxy.stream(
                        body,
                        {key: value for key, value in self.headers.items()},
                        route_key=route_key.strip()
                        if route_key and route_key.strip()
                        else None,
                        on_headers=on_headers,
                        on_chunk=on_chunk,
                    )
                except AiAuthSwitchError as exc:
                    if not started:
                        self.send_error(503, str(exc))

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def serve_forever(self) -> None:
        self._httpd = ThreadingHTTPServer(
            (self.config.host, self.config.port), self._handler_class()
        )
        self._httpd.daemon_threads = True
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()
            self._httpd = None

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
