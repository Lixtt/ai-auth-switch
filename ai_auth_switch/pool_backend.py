from __future__ import annotations

import json
import os
import selectors
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, sha256_file
from ai_auth_switch.utils import set_private_permissions
from ai_auth_switch.wrapper import (
    _link_shared_codex_state,
    _sync_replaced_shared_files,
)


@dataclass(frozen=True)
class BackendProcessInfo:
    profile: str
    pid: int
    runtime_home: Path
    auth_path: Path


class BackendProcess:
    """One Codex app-server process isolated to a saved auth profile."""

    def __init__(
        self,
        store: AuthStore,
        provider: Provider,
        profile: str,
        *,
        command: Sequence[str],
        runtime_parent: Path | None = None,
        popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        timeout: float = 10.0,
    ):
        self.store = store
        self.provider = provider
        self.profile = profile
        self.command = tuple(str(part) for part in command)
        self.runtime_parent = runtime_parent
        self.popen_factory = popen
        self.timeout = timeout
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[str] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._runtime_home: Path | None = None
        self._isolated_auth: Path | None = None
        self._initial_digest: str | None = None
        self._expected_identity: str | None = None
        self._initial_result: dict[str, Any] = {}

    @property
    def info(self) -> BackendProcessInfo:
        if (
            self._process is None
            or self._runtime_home is None
            or self._isolated_auth is None
        ):
            raise AiAuthSwitchError("backend process is not started")
        return BackendProcessInfo(
            profile=self.profile,
            pid=self._process.pid,
            runtime_home=self._runtime_home,
            auth_path=self._isolated_auth,
        )

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> dict[str, Any]:
        if self._process is not None:
            raise AiAuthSwitchError(f"backend {self.profile} is already started")
        profile_path = self.store.profile_path(self.provider, self.profile)
        if not profile_path.exists():
            raise AiAuthSwitchError(f"profile not found: {self.profile}")
        source_home = self.provider.active_auth_path.parent
        source_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(
            prefix="ai-auth-switch-pool-",
            dir=str(self.runtime_parent) if self.runtime_parent else None,
        )
        runtime_home = Path(self._temporary.name)
        set_private_permissions(runtime_home)
        _link_shared_codex_state(source_home, runtime_home)
        isolated_auth = runtime_home / "auth.json"
        with self.store.profile_lock(self.provider, self.profile):
            try:
                os.symlink(profile_path.absolute(), isolated_auth)
            except OSError as exc:
                self._cleanup_runtime()
                raise AiAuthSwitchError(
                    f"failed to install isolated auth for {self.profile}: {exc}"
                ) from exc
            self._initial_digest = sha256_file(isolated_auth)
            self._expected_identity = self.provider.auth_identity(isolated_auth)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(runtime_home)
        env.setdefault("CODEX_SQLITE_HOME", str(source_home))
        try:
            process = self.popen_factory(
                [*self.command, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            self._cleanup_runtime()
            raise AiAuthSwitchError(
                f"failed to start backend {self.profile}: {exc}"
            ) from exc
        self._process = process
        self._runtime_home = runtime_home
        self._isolated_auth = isolated_auth
        if process.stdin is None or process.stdout is None:
            self.stop()
            raise AiAuthSwitchError(f"backend {self.profile} has no JSONL pipes")
        self._selector = selectors.DefaultSelector()
        self._selector.register(process.stdout, selectors.EVENT_READ)
        self.send(
            {
                "method": "initialize",
                "id": f"pool-initialize:{self.profile}",
                "params": {
                    "clientInfo": {
                        "name": "ai_auth_switch_pool",
                        "title": "ai-auth-switch pool backend",
                        "version": "0.6.0-dev",
                    }
                },
            }
        )
        target_id = f"pool-initialize:{self.profile}"
        response = self.read_message(timeout=self.timeout)
        while response is not None and response.get("id") != target_id:
            response = self.read_message(timeout=self.timeout)
        if response is None:
            error = self._stderr_text()
            self.stop()
            raise AiAuthSwitchError(
                f"backend {self.profile} did not initialize"
                + (f": {error}" if error else "")
            )
        if response.get("error") is not None:
            self.stop()
            raise AiAuthSwitchError(
                f"backend {self.profile} initialization failed: {response['error']}"
            )
        self.send({"method": "initialized", "params": {}})
        self._initial_result = (
            response.get("result") if isinstance(response.get("result"), dict) else {}
        )
        return dict(self._initial_result)

    @property
    def initial_result(self) -> dict[str, Any]:
        return dict(self._initial_result)

    @property
    def stdout(self):
        return self._process.stdout if self._process is not None else None

    def send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AiAuthSwitchError(f"backend {self.profile} is not writable")
        try:
            self._process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AiAuthSwitchError(
                f"backend {self.profile} stdin is closed: {exc}"
            ) from exc

    def read_message(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        if self._process is None or self._process.stdout is None:
            raise AiAuthSwitchError(f"backend {self.profile} is not started")
        if self._selector is None:
            raise AiAuthSwitchError(f"backend {self.profile} has no selector")
        wait = self.timeout if timeout is None else timeout
        events = self._selector.select(wait)
        if not events:
            raise AiAuthSwitchError(f"backend {self.profile} read timed out")
        line = self._process.stdout.readline()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AiAuthSwitchError(
                f"backend {self.profile} returned invalid JSON: {line[:200]!r}"
            ) from exc
        if not isinstance(message, dict):
            raise AiAuthSwitchError(f"backend {self.profile} returned non-object JSON")
        return message

    def _stderr_text(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        if self._process.poll() is None:
            return ""
        try:
            return self._process.stderr.read().strip()
        except OSError:
            return ""

    def stop(self) -> None:
        process = self._process
        selector = self._selector
        try:
            if selector is not None:
                selector.close()
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        finally:
            self._selector = None
            self._process = None
            self._sync_auth()
            self._cleanup_runtime()

    def _sync_auth(self) -> None:
        if self._isolated_auth is None:
            return
        try:
            with self.store.profile_lock(self.provider, self.profile):
                self.store.sync_profile_auth(
                    self.provider,
                    self.profile,
                    self._isolated_auth,
                    expected_identity=self._expected_identity,
                    initial_digest=self._initial_digest,
                )
        except OSError:
            return

    def _cleanup_runtime(self) -> None:
        if self._runtime_home is not None:
            _sync_replaced_shared_files(
                self.provider.active_auth_path.parent,
                self._runtime_home,
            )
        temporary = self._temporary
        self._temporary = None
        self._runtime_home = None
        self._isolated_auth = None
        self._initial_digest = None
        self._expected_identity = None
        self._initial_result = {}
        if temporary is not None:
            temporary.cleanup()

    def __enter__(self) -> BackendProcess:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
