from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore, set_private_permissions


def _is_codex_auth_artifact(name: str) -> bool:
    return (
        name == "auth.json"
        or name.startswith("auth.json.")
        or name.startswith(".auth.json.")
    )


def _link_shared_codex_state(source_home: Path, isolated_home: Path) -> None:
    """Share every existing Codex state entry except auth credentials."""
    try:
        entries = list(source_home.iterdir())
    except OSError as exc:
        raise AiAuthSwitchError(f"failed to inspect Codex home {source_home}: {exc}") from exc

    for source in entries:
        if _is_codex_auth_artifact(source.name):
            continue
        target = isolated_home / source.name
        try:
            os.symlink(
                source.absolute(),
                target,
                target_is_directory=source.is_dir(),
            )
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to share Codex state {source} in isolated home: {exc}"
            ) from exc


def _sync_replaced_shared_files(source_home: Path, isolated_home: Path) -> None:
    """Keep state files shared when Codex atomically replaces a symlink."""
    for isolated in isolated_home.iterdir():
        if _is_codex_auth_artifact(isolated.name) or isolated.is_symlink():
            continue

        shared = source_home / isolated.name
        if isolated.is_file():
            tmp = shared.with_name(
                f".{shared.name}.ai-auth-switch.{os.getpid()}.{time.time_ns()}"
            )
            try:
                shutil.copy2(isolated, tmp)
                os.replace(tmp, shared)
            finally:
                if tmp.exists():
                    tmp.unlink()
            continue

        if isolated.is_dir() and not shared.exists() and not shared.is_symlink():
            try:
                os.replace(isolated, shared)
            except OSError:
                # Cross-device runtime directories are unusual, but preserving
                # newly introduced Codex state is still better than discarding it.
                shutil.copytree(isolated, shared)


def _run_codex_with_profile(
    store: AuthStore,
    provider: Provider,
    profile: str,
    command: Sequence[str],
) -> int:
    source_home = provider.active_auth_path.parent
    profile_path = store.profile_path(provider, profile)
    runtime_root = store.base_dir / "runtime" / provider.id

    with store.profile_lock(provider, profile):
        if not profile_path.exists():
            raise AiAuthSwitchError(f"profile not found: {profile}")

        runtime_root.mkdir(parents=True, exist_ok=True)
        set_private_permissions(runtime_root)
        with tempfile.TemporaryDirectory(
            prefix="session-",
            dir=runtime_root,
        ) as tmp:
            isolated_home = Path(tmp)
            set_private_permissions(isolated_home)
            _link_shared_codex_state(source_home, isolated_home)

            isolated_auth = isolated_home / "auth.json"
            try:
                os.symlink(profile_path.absolute(), isolated_auth)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to install isolated Codex auth for {profile}: {exc}"
                ) from exc

            env = os.environ.copy()
            env["CODEX_HOME"] = str(isolated_home)
            if not env.get("CODEX_SQLITE_HOME", "").strip():
                env["CODEX_SQLITE_HOME"] = str(source_home)

            try:
                return subprocess.call(list(command), env=env)
            finally:
                store.sync_profile_auth(provider, profile, isolated_auth)
                _sync_replaced_shared_files(source_home, isolated_home)


def run_with_profile(
    store: AuthStore,
    provider: Provider,
    profile: str,
    command: Sequence[str],
    *,
    on_activated: Callable[[], None] | None = None,
    on_restored: Callable[[], None] | None = None,
) -> int:
    if not command:
        command = provider.login_command

    if provider.id == "codex":
        return _run_codex_with_profile(store, provider, profile, command)

    activated = False
    with store.lock():
        try:
            with store.activated_temporarily(provider, profile):
                activated = True
                if on_activated is not None:
                    on_activated()
                return subprocess.call(list(command))
        finally:
            if activated and on_restored is not None:
                on_restored()
