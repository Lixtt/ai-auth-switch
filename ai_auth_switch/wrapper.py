from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import ClaudeProvider, Provider
from ai_auth_switch.providers.claude import AUTH_OVERRIDE_ENV_VARS
from ai_auth_switch.store import AuthStore, sha256_file
from ai_auth_switch.utils import set_private_permissions


def _is_codex_auth_artifact(name: str) -> bool:
    return (
        name == "auth.json"
        or name.startswith(("auth.json.", ".auth.json."))
    )


def _is_claude_private_artifact(name: str) -> bool:
    return (
        name in (".credentials.json", ".claude.json")
        or name.startswith((".credentials.json.", "..credentials.json."))
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
            # On the supported POSIX hosts a symlink does not need the target
            # type. Avoiding source.is_dir() saves one shared-filesystem stat
            # per Codex state entry on every launch.
            os.symlink(source.absolute(), target)
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to share Codex state {source} in isolated home: {exc}"
            ) from exc


def _sync_replaced_shared_files(
    source_home: Path,
    isolated_home: Path,
    *,
    is_private_artifact: Callable[[str], bool] = _is_codex_auth_artifact,
) -> None:
    """Keep shared state files when a provider replaces a symlink."""
    for isolated in isolated_home.iterdir():
        if is_private_artifact(isolated.name) or isolated.is_symlink():
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


def _codex_runtime_parent() -> Path:
    """Return a machine-local parent for private Codex homes.

    Codex refuses to install helper binaries when CODEX_HOME is below the
    system temporary directory, so prefer the per-user runtime directory and
    fall back to /var/tmp. Both stay local to a worker in the target cluster.
    """
    configured = os.environ.get("AI_AUTH_SWITCH_RUNTIME_DIR", "").strip()
    managed_parent = not configured
    if configured:
        parent = Path(configured).expanduser()
    else:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if not runtime_dir:
            candidate = Path("/run/user") / str(os.geteuid())
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                runtime_dir = str(candidate)
        parent = (
            Path(runtime_dir) / "ai-auth-switch" / "codex"
            if runtime_dir
            else Path("/var/tmp") / f"ai-auth-switch-{os.geteuid()}" / "codex"
        )

    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to create runtime directory {parent}: {exc}"
        ) from exc
    if not parent.is_dir():
        raise AiAuthSwitchError(f"runtime path is not a directory: {parent}")
    if managed_parent:
        try:
            if parent.is_symlink() or parent.stat().st_uid != os.geteuid():
                raise AiAuthSwitchError(f"unsafe runtime directory: {parent}")
            parent.chmod(0o700)
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to secure runtime directory {parent}: {exc}"
            ) from exc
    return parent


def _claude_runtime_parent() -> Path:
    configured = os.environ.get("AI_AUTH_SWITCH_RUNTIME_DIR", "").strip()
    managed_parent = not configured
    if configured:
        parent = Path(configured).expanduser()
    else:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if not runtime_dir:
            candidate = Path("/run/user") / str(os.geteuid())
            if candidate.is_dir() and os.access(candidate, os.W_OK):
                runtime_dir = str(candidate)
        parent = (
            Path(runtime_dir) / "ai-auth-switch" / "claude"
            if runtime_dir
            else Path("/var/tmp") / f"ai-auth-switch-{os.geteuid()}" / "claude"
        )
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to create Claude runtime directory {parent}: {exc}"
        ) from exc
    if not parent.is_dir():
        raise AiAuthSwitchError(f"runtime path is not a directory: {parent}")
    if managed_parent:
        try:
            if parent.is_symlink() or parent.stat().st_uid != os.geteuid():
                raise AiAuthSwitchError(f"unsafe runtime directory: {parent}")
            parent.chmod(0o700)
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to secure Claude runtime directory {parent}: {exc}"
            ) from exc
    return parent


def _run_codex_with_profile(
    store: AuthStore,
    provider: Provider,
    profile: str,
    command: Sequence[str],
) -> int:
    source_home = provider.active_auth_path.parent
    profile_path = store.profile_path(provider, profile)
    with tempfile.TemporaryDirectory(
        prefix="ai-auth-switch-codex-",
        dir=_codex_runtime_parent(),
    ) as tmp:
        isolated_home = Path(tmp)
        set_private_permissions(isolated_home)
        _link_shared_codex_state(source_home, isolated_home)

        isolated_auth = isolated_home / "auth.json"
        # Same-account runs deliberately reference one profile file so each
        # Codex process can observe a token rotated by another process. The
        # private home still keeps different accounts on different auth paths.
        # Only installation and reconciliation are serialized; the Codex
        # process itself remains fully concurrent.
        with store.profile_lock(provider, profile):
            if not profile_path.exists():
                raise AiAuthSwitchError(f"profile not found: {profile}")

            try:
                os.symlink(profile_path.absolute(), isolated_auth)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to install isolated Codex auth for {profile}: {exc}"
                ) from exc
            initial_auth_digest = sha256_file(isolated_auth)
            expected_identity = provider.auth_identity(isolated_auth)

        env = os.environ.copy()
        env["CODEX_HOME"] = str(isolated_home)
        if not env.get("CODEX_SQLITE_HOME", "").strip():
            env["CODEX_SQLITE_HOME"] = str(source_home)

        try:
            return subprocess.call(list(command), env=env)
        finally:
            # Codex may atomically replace auth.json and thereby detach this
            # session from the shared profile. Serialize only the resulting
            # guarded write-back, not the child process lifetime.
            try:
                with store.profile_lock(provider, profile):
                    store.sync_profile_auth(
                        provider,
                        profile,
                        isolated_auth,
                        expected_identity=expected_identity,
                        initial_digest=initial_auth_digest,
                    )
            finally:
                _sync_replaced_shared_files(source_home, isolated_home)


def _run_claude_with_profile(
    store: AuthStore,
    provider: ClaudeProvider,
    profile: str,
    command: Sequence[str],
) -> int:
    source_home = provider.config_dir
    try:
        source_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        set_private_permissions(source_home)
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to create Claude config directory {source_home}: {exc}"
        ) from exc
    profile_path = store.profile_path(provider, profile)
    with tempfile.TemporaryDirectory(
        prefix="ai-auth-switch-claude-",
        dir=_claude_runtime_parent(),
    ) as tmp:
        isolated_home = Path(tmp)
        set_private_permissions(isolated_home)

        try:
            entries = list(source_home.iterdir())
        except FileNotFoundError:
            entries = []
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to inspect Claude config directory {source_home}: {exc}"
            ) from exc
        for source in entries:
            if _is_claude_private_artifact(source.name):
                continue
            try:
                os.symlink(source.absolute(), isolated_home / source.name)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to share Claude state {source} in isolated config: {exc}"
                ) from exc

        isolated_state = isolated_home / ".claude.json"
        source_state = provider.config_state_path
        if source_state.is_file():
            try:
                shutil.copy2(source_state, isolated_state)
                set_private_permissions(isolated_state)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to copy Claude account state {source_state}: {exc}"
                ) from exc
        metadata = store.read_profile_metadata(provider, profile)
        if metadata:
            provider.apply_profile_metadata(metadata, config_dir=isolated_home)

        isolated_auth = isolated_home / ".credentials.json"
        with store.profile_lock(provider, profile):
            if not profile_path.exists():
                raise AiAuthSwitchError(f"profile not found: {profile}")
            try:
                os.symlink(profile_path.absolute(), isolated_auth)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to install isolated Claude auth for {profile}: {exc}"
                ) from exc
            initial_auth_digest = sha256_file(isolated_auth)
            expected_identity = provider.auth_identity(profile_path)

        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(isolated_home)
        # Explicitly selected saved OAuth credentials must not be shadowed by
        # higher-precedence environment authentication inherited from the shell.
        for name in AUTH_OVERRIDE_ENV_VARS:
            env.pop(name, None)

        try:
            return subprocess.call(list(command), env=env)
        finally:
            try:
                with store.profile_lock(provider, profile):
                    store.sync_profile_auth(
                        provider,
                        profile,
                        isolated_auth,
                        expected_identity=expected_identity,
                        initial_digest=initial_auth_digest,
                    )
            finally:
                _sync_replaced_shared_files(
                    source_home,
                    isolated_home,
                    is_private_artifact=_is_claude_private_artifact,
                )


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
    if provider.id == "claude" and isinstance(provider, ClaudeProvider):
        return _run_claude_with_profile(store, provider, profile, command)

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
