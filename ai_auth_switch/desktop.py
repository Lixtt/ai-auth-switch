from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, ProfileInfo
from ai_auth_switch.usage import AccountUsage, fetch_profile_usage, is_free_plan
from ai_auth_switch.utils import atomic_write

DESKTOP_DAEMON_ENV = "CODEX_APP_SERVER_USE_LOCAL_DAEMON"
DESKTOP_CODEX_BIN_ENV = "AI_AUTH_SWITCH_DESKTOP_CODEX_BIN"
CHATGPT_DESKTOP_FILE_ENV = "AI_AUTH_SWITCH_CHATGPT_DESKTOP_FILE"
CHATGPT_RESOURCES_ENV = "AI_AUTH_SWITCH_CHATGPT_RESOURCES"
SERVICE_NAME = "ai-auth-switch-desktop-auto.service"
MANAGED_LAUNCHER_MARKER = "X-AiAuthSwitch-Managed=true"


@dataclass(frozen=True)
class DesktopAutoConfig:
    idle_seconds: float = 60.0
    cooldown_seconds: float = 1800.0
    poll_seconds: float = 15.0
    switch_below_remaining: float = 10.0
    min_improvement: float = 5.0
    usage_timeout: float = 5.0
    usage_workers: int = 4
    usage_cache_ttl: float = 60.0

    @classmethod
    def from_dict(cls, value: object) -> DesktopAutoConfig:
        if not isinstance(value, dict):
            return cls()

        def number(name: str, default: float, *, minimum: float = 0.0) -> float:
            candidate = value.get(name)
            if not isinstance(candidate, (int, float)) or float(candidate) < minimum:
                return default
            return float(candidate)

        workers = value.get("usage_workers")
        if not isinstance(workers, int) or workers < 1:
            workers = cls.usage_workers
        return cls(
            idle_seconds=number("idle_seconds", cls.idle_seconds),
            cooldown_seconds=number("cooldown_seconds", cls.cooldown_seconds),
            poll_seconds=number("poll_seconds", cls.poll_seconds, minimum=0.1),
            switch_below_remaining=number(
                "switch_below_remaining", cls.switch_below_remaining
            ),
            min_improvement=number("min_improvement", cls.min_improvement),
            usage_timeout=number("usage_timeout", cls.usage_timeout, minimum=0.1),
            usage_workers=workers,
            usage_cache_ttl=number("usage_cache_ttl", cls.usage_cache_ttl),
        )


@dataclass
class DesktopAutoState:
    idle_since: float | None = None
    last_switch_at: float | None = None
    last_profile: str | None = None
    selected_at: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: object) -> DesktopAutoState:
        if not isinstance(value, dict):
            return cls()
        selected = value.get("selected_at")
        selected_at = (
            {
                str(name): float(timestamp)
                for name, timestamp in selected.items()
                if isinstance(name, str) and isinstance(timestamp, (int, float))
            }
            if isinstance(selected, dict)
            else {}
        )
        idle_since = value.get("idle_since")
        last_switch_at = value.get("last_switch_at")
        last_profile = value.get("last_profile")
        return cls(
            idle_since=(
                float(idle_since) if isinstance(idle_since, (int, float)) else None
            ),
            last_switch_at=(
                float(last_switch_at)
                if isinstance(last_switch_at, (int, float))
                else None
            ),
            last_profile=last_profile if isinstance(last_profile, str) else None,
            selected_at=selected_at,
        )


@dataclass(frozen=True)
class DesktopPaths:
    config: Path
    state: Path
    service: Path
    launcher: Path
    launcher_backup: Path
    system_launcher: Path


@dataclass(frozen=True)
class DesktopProcessState:
    mode: str
    app_pids: tuple[int, ...] = ()
    app_server_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ProfileCapacity:
    name: str
    remaining_percent: float
    resets_at: int | None


@dataclass(frozen=True)
class RotationDecision:
    profile: str | None
    reason: str
    current_remaining: float | None = None
    selected_remaining: float | None = None


@dataclass(frozen=True)
class RotationResult:
    changed: bool
    profile: str | None
    reason: str
    previous_profile: str | None = None


def desktop_paths(
    store: AuthStore,
    *,
    home: Path | None = None,
    config_home: Path | None = None,
    data_home: Path | None = None,
    system_launcher: Path | None = None,
) -> DesktopPaths:
    user_home = (home or Path.home()).expanduser()
    user_config = config_home or Path(
        os.environ.get("XDG_CONFIG_HOME", user_home / ".config")
    )
    user_data = data_home or Path(
        os.environ.get("XDG_DATA_HOME", user_home / ".local" / "share")
    )
    root = store.base_dir / "desktop-auto"
    configured_launcher = os.environ.get(CHATGPT_DESKTOP_FILE_ENV)
    source_launcher = system_launcher or (
        Path(configured_launcher).expanduser()
        if configured_launcher
        else Path("/usr/share/applications/chatgpt.desktop")
    )
    return DesktopPaths(
        config=root / "config.json",
        state=root / "state.json",
        service=user_config / "systemd" / "user" / SERVICE_NAME,
        launcher=user_data / "applications" / "chatgpt.desktop",
        launcher_backup=root / "chatgpt.desktop.original",
        system_launcher=source_launcher,
    )


def load_desktop_config(path: Path) -> DesktopAutoConfig:
    try:
        return DesktopAutoConfig.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return DesktopAutoConfig()


def save_desktop_config(path: Path, config: DesktopAutoConfig) -> None:
    atomic_write(path, json.dumps(asdict(config), indent=2, sort_keys=True) + "\n")


def load_desktop_state(path: Path) -> DesktopAutoState:
    try:
        return DesktopAutoState.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return DesktopAutoState()


def save_desktop_state(path: Path, state: DesktopAutoState) -> None:
    atomic_write(path, json.dumps(asdict(state), indent=2, sort_keys=True) + "\n")


def desktop_codex_binary() -> Path:
    configured = os.environ.get(DESKTOP_CODEX_BIN_ENV)
    candidates = []
    if configured and configured.strip():
        candidates.append(Path(configured).expanduser())
    candidates.append(Path("/usr/lib/chatgpt/resources/codex"))
    resolved = shutil.which("codex")
    if resolved:
        candidates.append(Path(resolved))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AiAuthSwitchError(
        "Codex app-server executable not found; set "
        f"{DESKTOP_CODEX_BIN_ENV} to the desktop-bundled codex binary"
    )


def desktop_daemon_supported(resources: Path | None = None) -> bool:
    configured = os.environ.get(CHATGPT_RESOURCES_ENV)
    root = resources or (
        Path(configured).expanduser()
        if configured
        else Path("/usr/lib/chatgpt/resources")
    )
    app_bundle = root / "app.asar"
    try:
        needle = DESKTOP_DAEMON_ENV.encode()
        overlap = b""
        with app_bundle.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                content = overlap + chunk
                if needle in content:
                    return True
                overlap = content[-len(needle) :]
    except OSError:
        return False
    return False


def parse_desktop_processes(output: str) -> DesktopProcessState:
    processes: dict[int, tuple[int, str]] = {}
    for line in output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, parent = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        processes[pid] = (parent, parts[2])

    app_pids = {
        pid
        for pid, (_parent, command) in processes.items()
        if "/ChatGPT" in command and "--type=" not in command
    }
    if not app_pids:
        return DesktopProcessState(mode="stopped")

    def belongs_to_app(pid: int) -> bool:
        seen = set()
        current = pid
        while current in processes and current not in seen:
            seen.add(current)
            parent = processes[current][0]
            if parent in app_pids:
                return True
            current = parent
        return False

    managed = []
    unmanaged = []
    for pid, (_parent, command) in processes.items():
        if pid in app_pids or "app-server" not in command or not belongs_to_app(pid):
            continue
        if "app-server proxy" in command:
            managed.append(pid)
        else:
            unmanaged.append(pid)
    if unmanaged:
        return DesktopProcessState(
            mode="unmanaged",
            app_pids=tuple(sorted(app_pids)),
            app_server_pids=tuple(sorted(unmanaged)),
        )
    if managed:
        return DesktopProcessState(
            mode="managed",
            app_pids=tuple(sorted(app_pids)),
            app_server_pids=tuple(sorted(managed)),
        )
    return DesktopProcessState(mode="transition", app_pids=tuple(sorted(app_pids)))


def detect_desktop_processes(
    *,
    codex_home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DesktopProcessState:
    try:
        result = runner(
            ["ps", "-eo", "pid=,ppid=,args="],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AiAuthSwitchError(
            f"failed to inspect ChatGPT Desktop processes: {exc}"
        ) from exc
    state = parse_desktop_processes(result.stdout)
    home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    socket_path = home / "app-server-control" / "app-server-control.sock"
    if state.mode == "transition" and socket_path.exists():
        return DesktopProcessState(
            mode="managed",
            app_pids=state.app_pids,
            app_server_pids=state.app_server_pids,
        )
    return state


class _UnixWebSocket:
    _GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, path: Path, *, timeout: float):
        self.path = path
        self.timeout = timeout
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.buffer = bytearray()

    def __enter__(self) -> _UnixWebSocket:
        try:
            self.socket.connect(str(self.path))
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                "GET /rpc HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            self.socket.sendall(request.encode("ascii"))
            header = self._read_http_header()
            lines = header.decode("latin-1").split("\r\n")
            if not lines or " 101 " not in f" {lines[0]} ":
                raise AiAuthSwitchError(
                    f"desktop app-server websocket upgrade failed: {lines[0]}"
                )
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1(
                    (key + self._GUID).encode("ascii"),
                    usedforsecurity=False,
                ).digest()
            ).decode("ascii")
            if headers.get("sec-websocket-accept") != expected:
                raise AiAuthSwitchError(
                    "desktop app-server websocket returned an invalid accept key"
                )
            return self
        except Exception:
            self.socket.close()
            raise

    def __exit__(self, exc_type, exc, tb) -> None:
        self.socket.close()

    def _receive_bytes(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.socket.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise AiAuthSwitchError("desktop app-server websocket closed")
            self.buffer.extend(chunk)
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def _read_http_header(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self.buffer:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise AiAuthSwitchError(
                    "desktop app-server closed during websocket upgrade"
                )
            self.buffer.extend(chunk)
            if len(self.buffer) > 64 * 1024:
                raise AiAuthSwitchError(
                    "desktop app-server returned an oversized websocket header"
                )
        index = self.buffer.index(marker) + len(marker)
        header = bytes(self.buffer[:index])
        del self.buffer[:index]
        return header

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length <= 125:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((0x80 | opcode, 0x80 | 126)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0x80 | 127)) + struct.pack("!Q", length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + masked)

    def send_json(self, message: dict[str, Any]) -> None:
        self._send_frame(
            0x1,
            json.dumps(message, separators=(",", ":")).encode("utf-8"),
        )

    def receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        while True:
            first, second = self._receive_bytes(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._receive_bytes(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._receive_bytes(8))[0]
            if length > 16 * 1024 * 1024:
                raise AiAuthSwitchError(
                    "desktop app-server returned an oversized websocket message"
                )
            mask = self._receive_bytes(4) if masked else None
            payload = self._receive_bytes(length)
            if mask is not None:
                payload = bytes(
                    value ^ mask[index % 4] for index, value in enumerate(payload)
                )
            if opcode == 0x8:
                raise AiAuthSwitchError("desktop app-server closed the websocket")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode not in (0x0, 0x1):
                continue
            fragments.extend(payload)
            if not final:
                continue
            try:
                message = json.loads(fragments.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AiAuthSwitchError(
                    "desktop app-server returned an invalid websocket message"
                ) from exc
            if isinstance(message, dict):
                return message
            fragments.clear()


def _app_server_request(
    codex_home: Path,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 5.0,
    connection: Callable[..., _UnixWebSocket] = _UnixWebSocket,
) -> dict[str, Any]:
    socket_path = codex_home / "app-server-control" / "app-server-control.sock"
    try:
        websocket_context = connection(socket_path, timeout=timeout)
        with websocket_context as websocket:
            websocket.send_json(
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "ai_auth_switch",
                            "title": "ai-auth-switch",
                            "version": "desktop-auto",
                        }
                    },
                }
            )

            def receive(request_id: int) -> dict[str, Any]:
                while True:
                    message = websocket.receive_json()
                    if message.get("id") == request_id:
                        return message

            initialized = receive(1)
            if initialized.get("error") is not None:
                raise AiAuthSwitchError(
                    f"desktop app-server initialization failed: {initialized['error']}"
                )
            websocket.send_json({"method": "initialized", "params": {}})
            websocket.send_json({"method": method, "id": 2, "params": params})
            response = receive(2)
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to query the desktop app-server at {socket_path}: {exc}"
        ) from exc

    error = response.get("error")
    if error is not None:
        raise AiAuthSwitchError(f"desktop app-server request failed: {error}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise AiAuthSwitchError("desktop app-server returned an invalid response")
    return result


def active_desktop_threads(
    codex_home: Path,
    *,
    timeout: float = 5.0,
    connection: Callable[..., _UnixWebSocket] = _UnixWebSocket,
) -> list[str]:
    result = _app_server_request(
        codex_home,
        "thread/list",
        {"cursor": None, "limit": 100, "sortKey": "updated_at"},
        timeout=timeout,
        connection=connection,
    )
    threads = result.get("data")
    if not isinstance(threads, list):
        raise AiAuthSwitchError("desktop app-server returned no thread list")
    active = []
    for thread in threads:
        if not isinstance(thread, dict):
            continue
        status = thread.get("status")
        if isinstance(status, dict) and status.get("type") == "active":
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                active.append(thread_id)
    return active


def _capacity(name: str, usage: AccountUsage | None) -> ProfileCapacity | None:
    if usage is None or usage.error or is_free_plan(usage):
        return None
    windows = [window for window in (usage.primary, usage.secondary) if window]
    if not windows:
        return None
    remaining = min(window.remaining_percent for window in windows)
    if remaining <= 0:
        return None
    resets = [window.resets_at for window in windows if window.resets_at is not None]
    return ProfileCapacity(
        name=name,
        remaining_percent=remaining,
        resets_at=min(resets) if resets else None,
    )


def choose_desktop_profile(
    profiles: list[ProfileInfo],
    usages: dict[str, AccountUsage],
    current: str | None,
    config: DesktopAutoConfig,
    state: DesktopAutoState,
    *,
    force: bool = False,
) -> RotationDecision:
    capacities = {
        profile.name: capacity
        for profile in profiles
        if (capacity := _capacity(profile.name, usages.get(profile.name))) is not None
    }
    candidates = [capacity for name, capacity in capacities.items() if name != current]
    if not candidates:
        return RotationDecision(
            None,
            "no other authenticated paid account has usable quota",
        )
    candidates.sort(
        key=lambda candidate: (
            -candidate.remaining_percent,
            state.selected_at.get(candidate.name, 0.0),
            candidate.resets_at if candidate.resets_at is not None else float("inf"),
            candidate.name,
        )
    )
    selected = candidates[0]
    current_capacity = capacities.get(current) if current else None
    current_remaining = (
        current_capacity.remaining_percent if current_capacity is not None else None
    )
    if not force and current_remaining is not None:
        if current_remaining > config.switch_below_remaining:
            return RotationDecision(
                None,
                "current account remains above the switch threshold",
                current_remaining=current_remaining,
            )
        if selected.remaining_percent - current_remaining < config.min_improvement:
            return RotationDecision(
                None,
                "no account improves remaining quota enough to switch",
                current_remaining=current_remaining,
                selected_remaining=selected.remaining_percent,
            )
    return RotationDecision(
        selected.name,
        "selected the healthiest eligible account",
        current_remaining=current_remaining,
        selected_remaining=selected.remaining_percent,
    )


def _run_daemon_command(
    codex_bin: Path,
    action: str,
    *,
    codex_home: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    env = os.environ.copy()
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    try:
        completed = runner(
            [str(codex_bin), "app-server", "daemon", action],
            env=env,
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AiAuthSwitchError(
            f"failed to {action} desktop app-server daemon: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AiAuthSwitchError(
            f"failed to {action} desktop app-server daemon: "
            f"{detail or completed.returncode}"
        )


def _fetch_capacities(
    store: AuthStore,
    provider: Provider,
    profiles: list[ProfileInfo],
    config: DesktopAutoConfig,
    *,
    refresh: bool,
    fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
) -> dict[str, AccountUsage]:
    return fetcher(
        ((profile.name, profile.path) for profile in profiles),
        timeout=config.usage_timeout,
        workers=config.usage_workers,
        cache_dir=store.base_dir / "cache" / "usage" / provider.id,
        cache_ttl=config.usage_cache_ttl,
        refresh=refresh,
    )


def rotate_desktop_account(
    store: AuthStore,
    provider: Provider,
    config: DesktopAutoConfig,
    state: DesktopAutoState,
    *,
    force: bool = False,
    now: float | None = None,
    process_state: DesktopProcessState | None = None,
    codex_bin: Path | None = None,
    process_detector: Callable[[], DesktopProcessState] | None = None,
    thread_reader: Callable[[Path], list[str]] = active_desktop_threads,
    usage_fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
    daemon_runner: Callable[..., None] = _run_daemon_command,
) -> RotationResult:
    timestamp = time.time() if now is None else now
    runtime = process_state or (
        process_detector()
        if process_detector is not None
        else detect_desktop_processes(codex_home=provider.active_auth_path.parent)
    )
    if runtime.mode == "unmanaged":
        raise AiAuthSwitchError(
            "ChatGPT Desktop is using its private app-server; run "
            "`ais desktop auto install` and restart ChatGPT once before rotating"
        )
    if runtime.mode == "transition":
        raise AiAuthSwitchError("ChatGPT Desktop is reconnecting; try again shortly")
    binary = codex_bin or desktop_codex_binary()
    codex_home = provider.active_auth_path.parent
    if runtime.mode == "managed":
        active = thread_reader(codex_home)
        if active:
            raise AiAuthSwitchError(
                f"refusing to switch while {len(active)} desktop turn(s) are active"
            )

    profiles = store.list_profiles(provider)
    current_info = store.current_profile(provider)
    current = current_info.name if current_info else None
    usages = _fetch_capacities(
        store,
        provider,
        profiles,
        config,
        refresh=force,
        fetcher=usage_fetcher,
    )
    decision = choose_desktop_profile(
        profiles,
        usages,
        current,
        config,
        state,
        force=force,
    )
    if decision.profile is None:
        return RotationResult(False, current, decision.reason, previous_profile=current)

    if runtime.mode == "managed":
        active = thread_reader(codex_home)
        if active:
            raise AiAuthSwitchError(
                f"refusing to switch because {len(active)} desktop turn(s) "
                "started during selection"
            )

    previous = current
    with store.lock():
        store.activate(provider, decision.profile)
    try:
        if runtime.mode == "managed":
            daemon_runner(
                binary,
                "restart",
                codex_home=provider.active_auth_path.parent,
            )
    except Exception as exc:
        rollback_restart_error = None
        if previous is not None:
            with store.lock():
                store.activate(provider, previous)
            try:
                daemon_runner(
                    binary,
                    "restart",
                    codex_home=provider.active_auth_path.parent,
                )
            except Exception as rollback_exc:
                rollback_restart_error = rollback_exc
        if rollback_restart_error is not None:
            raise AiAuthSwitchError(
                "desktop account switch failed and the daemon could not restart "
                f"after restoring {previous}: {rollback_restart_error}"
            ) from exc
        raise

    state.last_switch_at = timestamp
    state.last_profile = decision.profile
    state.selected_at[decision.profile] = timestamp
    state.idle_since = timestamp
    return RotationResult(
        True,
        decision.profile,
        decision.reason,
        previous_profile=previous,
    )


def desktop_auto_cycle(
    store: AuthStore,
    provider: Provider,
    config: DesktopAutoConfig,
    state: DesktopAutoState,
    *,
    now: float | None = None,
    process_detector: Callable[[], DesktopProcessState] | None = None,
    thread_reader: Callable[[Path], list[str]] = active_desktop_threads,
    codex_bin: Path | None = None,
    usage_fetcher: Callable[..., dict[str, AccountUsage]] = fetch_profile_usage,
    daemon_runner: Callable[..., None] = _run_daemon_command,
) -> RotationResult:
    timestamp = time.time() if now is None else now
    runtime = (
        process_detector()
        if process_detector is not None
        else detect_desktop_processes(codex_home=provider.active_auth_path.parent)
    )
    current_info = store.current_profile(provider)
    current = current_info.name if current_info else None
    if runtime.mode != "managed":
        state.idle_since = None
        return RotationResult(
            False,
            current,
            f"desktop mode is {runtime.mode}; waiting for managed daemon mode",
            previous_profile=current,
        )

    binary = codex_bin or desktop_codex_binary()
    codex_home = provider.active_auth_path.parent
    active = thread_reader(codex_home)
    if active:
        state.idle_since = None
        return RotationResult(
            False,
            current,
            f"{len(active)} desktop turn(s) are active",
            previous_profile=current,
        )
    if state.idle_since is None:
        state.idle_since = timestamp
        return RotationResult(
            False,
            current,
            "desktop became idle; waiting for the grace period",
            previous_profile=current,
        )
    if timestamp - state.idle_since < config.idle_seconds:
        return RotationResult(
            False,
            current,
            "desktop idle grace period has not elapsed",
            previous_profile=current,
        )
    if (
        state.last_switch_at is not None
        and timestamp - state.last_switch_at < config.cooldown_seconds
    ):
        return RotationResult(
            False,
            current,
            "desktop account switch cooldown is active",
            previous_profile=current,
        )
    return rotate_desktop_account(
        store,
        provider,
        config,
        state,
        force=False,
        now=timestamp,
        process_state=runtime,
        codex_bin=binary,
        thread_reader=thread_reader,
        usage_fetcher=usage_fetcher,
        daemon_runner=daemon_runner,
    )


def _systemd_quote(value: str | Path) -> str:
    rendered = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{rendered}"'


def _service_text(
    executable: Path,
    store: AuthStore,
    provider: Provider,
) -> str:
    command = " ".join(
        _systemd_quote(part)
        for part in (
            executable,
            "--store-dir",
            store.base_dir,
            "--codex-home",
            provider.active_auth_path.parent,
            "desktop",
            "auto",
            "run",
        )
    )
    return (
        "[Unit]\n"
        "Description=ai-auth-switch desktop account auto-rotation\n"
        "After=graphical-session.target\n\n"
        "Conflicts=ai-auth-switch-desktop-pool.service\n"
        "Before=ai-auth-switch-desktop-pool.service\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        f"Environment={_systemd_quote(f'CODEX_HOME={provider.active_auth_path.parent}')}\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _managed_launcher(content: str, codex_home: Path) -> str:
    lines = content.splitlines()
    replaced = False
    rendered = []
    for line in lines:
        if line.startswith("Exec=") and DESKTOP_DAEMON_ENV not in line:
            rendered.append(
                f"Exec=env {DESKTOP_DAEMON_ENV}=1 "
                f"CODEX_HOME={_systemd_quote(codex_home)} "
                f"{line.removeprefix('Exec=')}"
            )
            replaced = True
        else:
            rendered.append(line)
    if not replaced and not any(DESKTOP_DAEMON_ENV in line for line in rendered):
        raise AiAuthSwitchError(
            "ChatGPT desktop launcher contains no usable Exec entry"
        )
    if MANAGED_LAUNCHER_MARKER not in rendered:
        rendered.append(MANAGED_LAUNCHER_MARKER)
    return "\n".join(rendered) + "\n"


def _systemctl(
    *args: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = runner(
            ["systemctl", "--user", *args],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AiAuthSwitchError(f"failed to run systemctl --user: {exc}") from exc
    if check and completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise AiAuthSwitchError(
            f"systemctl --user {' '.join(args)} failed: "
            f"{detail or completed.returncode}"
        )
    return completed


def _refresh_desktop_database(
    applications_dir: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    executable = shutil.which("update-desktop-database")
    if not executable:
        return
    try:
        runner(
            [executable, str(applications_dir)],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return


def install_desktop_auto(
    store: AuthStore,
    provider: Provider,
    executable: Path,
    config: DesktopAutoConfig,
    *,
    paths: DesktopPaths | None = None,
    enable: bool = True,
    supports_daemon: bool | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    daemon_runner: Callable[..., None] = _run_daemon_command,
) -> DesktopPaths:
    if sys.platform != "linux":
        raise AiAuthSwitchError("desktop auto-rotation currently supports Linux only")
    if supports_daemon is None:
        supports_daemon = desktop_daemon_supported()
    if not supports_daemon:
        raise AiAuthSwitchError(
            f"this ChatGPT Desktop build does not expose {DESKTOP_DAEMON_ENV}"
        )
    target_paths = paths or desktop_paths(store)
    source = (
        target_paths.launcher
        if target_paths.launcher.exists()
        else target_paths.system_launcher
    )
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to read ChatGPT desktop launcher {source}: {exc}"
        ) from exc

    if enable:
        binary = desktop_codex_binary()
        managed_binary = (
            provider.active_auth_path.parent
            / "packages"
            / "standalone"
            / "current"
            / "codex"
        )
        if not managed_binary.exists():
            managed_binary.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(binary, managed_binary)
            except OSError as exc:
                raise AiAuthSwitchError(
                    "failed to install managed desktop Codex link "
                    f"{managed_binary}: {exc}"
                ) from exc
        if not managed_binary.is_file() or not os.access(managed_binary, os.X_OK):
            raise AiAuthSwitchError(
                f"managed desktop Codex executable is unusable: {managed_binary}"
            )
        daemon_runner(
            binary,
            "start",
            codex_home=provider.active_auth_path.parent,
        )

    if (
        target_paths.launcher.exists()
        and MANAGED_LAUNCHER_MARKER not in content
        and not target_paths.launcher_backup.exists()
    ):
        atomic_write(target_paths.launcher_backup, content)
    atomic_write(
        target_paths.launcher,
        _managed_launcher(content, provider.active_auth_path.parent),
    )
    _refresh_desktop_database(target_paths.launcher.parent, runner=runner)
    save_desktop_config(target_paths.config, config)
    atomic_write(target_paths.service, _service_text(executable, store, provider))
    if enable:
        _systemctl("daemon-reload", runner=runner)
        _systemctl("enable", "--now", SERVICE_NAME, runner=runner)
    return target_paths


def disable_desktop_auto(
    store: AuthStore,
    *,
    paths: DesktopPaths | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> DesktopPaths:
    target_paths = paths or desktop_paths(store)
    _systemctl("disable", "--now", SERVICE_NAME, runner=runner, check=False)
    if target_paths.service.exists():
        target_paths.service.unlink()
    _systemctl("daemon-reload", runner=runner, check=False)
    try:
        launcher_content = target_paths.launcher.read_text(encoding="utf-8")
    except OSError:
        launcher_content = ""
    if MANAGED_LAUNCHER_MARKER in launcher_content:
        if target_paths.launcher_backup.exists():
            atomic_write(
                target_paths.launcher,
                target_paths.launcher_backup.read_text(encoding="utf-8"),
            )
            target_paths.launcher_backup.unlink()
        else:
            target_paths.launcher.unlink()
        _refresh_desktop_database(target_paths.launcher.parent, runner=runner)
    return target_paths


def desktop_auto_status(
    store: AuthStore,
    provider: Provider,
    *,
    paths: DesktopPaths | None = None,
    process_detector: Callable[[], DesktopProcessState] | None = None,
    thread_reader: Callable[[Path], list[str]] = active_desktop_threads,
    codex_bin: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    target_paths = paths or desktop_paths(store)
    runtime = (
        process_detector()
        if process_detector is not None
        else detect_desktop_processes(codex_home=provider.active_auth_path.parent)
    )
    enabled = _systemctl("is-enabled", SERVICE_NAME, runner=runner, check=False)
    active_service = _systemctl("is-active", SERVICE_NAME, runner=runner, check=False)
    active_threads: list[str] | None = None
    app_server_error = None
    if runtime.mode == "managed":
        try:
            active_threads = thread_reader(provider.active_auth_path.parent)
        except AiAuthSwitchError as exc:
            app_server_error = str(exc)
    current = store.current_profile(provider)
    return {
        "installed": target_paths.service.exists() and target_paths.config.exists(),
        "service_enabled": enabled.returncode == 0,
        "service_active": active_service.returncode == 0,
        "desktop_mode": runtime.mode,
        "active_threads": active_threads,
        "app_server_error": app_server_error,
        "current_profile": current.name if current else None,
        "config": asdict(load_desktop_config(target_paths.config)),
        "restart_required": runtime.mode == "unmanaged",
    }


def run_desktop_auto(
    store: AuthStore,
    provider: Provider,
    *,
    paths: DesktopPaths | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    target_paths = paths or desktop_paths(store)
    previous_message = None
    while True:
        config = load_desktop_config(target_paths.config)
        state = load_desktop_state(target_paths.state)
        try:
            result = desktop_auto_cycle(store, provider, config, state)
            message = result.reason
            if result.changed:
                message = (
                    f"switched desktop account {result.previous_profile or '<none>'} "
                    f"-> {result.profile}"
                )
        except AiAuthSwitchError as exc:
            message = f"desktop auto-rotation waiting: {exc}"
        save_desktop_state(target_paths.state, state)
        if message != previous_message:
            print(message, flush=True)
            previous_message = message
        sleep(config.poll_seconds)
