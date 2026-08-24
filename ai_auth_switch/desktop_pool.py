from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_auth_switch.desktop import (
    SERVICE_NAME,
    _refresh_desktop_database,
    _systemctl,
    desktop_paths,
)
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.pool_config import (
    install_pool_provider,
    parse_toml,
    restore_codex_config,
)
from ai_auth_switch.pool_responses import (
    DEFAULT_LISTEN_HOST,
    DEFAULT_LISTEN_PORT,
    default_token_path,
    ensure_listener_token,
)
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.utils import atomic_write, set_private_permissions

POOL_DESKTOP_SERVICE = "ai-auth-switch-desktop-pool.service"
POOL_DESKTOP_MARKER = "X-AiAuthSwitch-Pool=true"


@dataclass(frozen=True)
class DesktopPoolPaths:
    launcher: Path
    launcher_backup: Path
    wrapper: Path
    service: Path
    config_backup: Path | None = None
    auto_service_state: Path | None = None


def desktop_pool_paths(store: AuthStore) -> DesktopPoolPaths:
    base = store.base_dir / "desktop-pool"
    normal = desktop_paths(store)
    return DesktopPoolPaths(
        launcher=normal.launcher,
        launcher_backup=base / "chatgpt.desktop.original",
        wrapper=base / "chatgpt-pool-launcher",
        service=normal.service.parent / POOL_DESKTOP_SERVICE,
        auto_service_state=base / "auto-service-state",
    )


def _desktop_command(content: str) -> str:
    for line in content.splitlines():
        if not line.startswith("Exec="):
            continue
        tokens = shlex.split(line.removeprefix("Exec="))
        if not tokens:
            continue
        if tokens[0] == "env":
            tokens = [
                token for token in tokens[1:] if "=" not in token.split("%", 1)[0]
            ]
        if not tokens:
            continue
        executable = shutil.which(tokens[0]) or tokens[0]
        if executable.startswith("-"):
            continue
        return executable
    raise AiAuthSwitchError("ChatGPT desktop launcher contains no usable Exec entry")


def _replace_exec(content: str, wrapper: Path) -> str:
    lines = content.splitlines()
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith("Exec=") and not replaced:
            output.append(f"Exec={wrapper} %U")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        raise AiAuthSwitchError("ChatGPT desktop launcher contains no Exec entry")
    if POOL_DESKTOP_MARKER not in output:
        output.append(POOL_DESKTOP_MARKER)
    return "\n".join(output) + "\n"


def _write_wrapper(path: Path, command: str, token_path: Path) -> None:
    rendered = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"token_file={shlex.quote(str(token_path))}\n"
        'if [ ! -r "$token_file" ]; then\n'
        "  echo 'ai-auth-switch: pool token file is missing' >&2\n"
        "  exit 1\n"
        "fi\n"
        'AI_AUTH_SWITCH_POOL_TOKEN=$(cat "$token_file")\n'
        "export AI_AUTH_SWITCH_POOL_TOKEN\n"
        "export CODEX_APP_SERVER_USE_LOCAL_DAEMON=1\n"
        f'exec {shlex.quote(command)} "$@"\n'
    )
    atomic_write(path, rendered)
    path.chmod(0o700)


def _service_text(
    store: AuthStore,
    provider: Provider,
    *,
    token_path: Path,
    port: int,
) -> str:
    python = shlex.quote(sys.executable)
    command = " ".join(
        [
            python,
            "-m",
            "ai_auth_switch.cli",
            "--store-dir",
            shlex.quote(str(store.base_dir)),
            "--codex-home",
            shlex.quote(str(provider.active_auth_path.parent)),
            "pool",
            "responses",
            "--pool-host",
            DEFAULT_LISTEN_HOST,
            "--pool-port",
            str(port),
            "--pool-token-file",
            shlex.quote(str(token_path)),
        ]
    )
    return (
        "[Unit]\n"
        "Description=ai-auth-switch local Codex account pool\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={command}\n"
        "Restart=on-failure\n"
        "RestartSec=3\n"
        "Environment=PYTHONUNBUFFERED=1\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_desktop_pool(
    store: AuthStore,
    provider: Provider,
    *,
    port: int = DEFAULT_LISTEN_PORT,
    paths: DesktopPoolPaths | None = None,
    runner=subprocess.run,
) -> DesktopPoolPaths:
    if sys.platform != "linux":
        raise AiAuthSwitchError("desktop pool currently supports Linux only")
    target = paths or desktop_pool_paths(store)
    normal = desktop_paths(store)
    had_launcher = target.launcher.exists()
    source = target.launcher if target.launcher.exists() else normal.system_launcher
    try:
        content = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to read ChatGPT desktop launcher {source}: {exc}"
        ) from exc
    # Re-running the install must not capture the pool wrapper as the desktop
    # command: once the launcher points at the wrapper, reading it back would
    # produce a self-referencing wrapper that never starts ChatGPT. Prefer the
    # pristine backup for command extraction, falling back to the current or
    # system launcher when no backup exists yet.
    command_content = content
    if target.launcher_backup.exists():
        try:
            command_content = target.launcher_backup.read_text(encoding="utf-8")
        except OSError:
            command_content = content
    command = _desktop_command(command_content)
    token_path = default_token_path(store)
    ensure_listener_token(token_path)
    config_result = install_pool_provider(
        provider.active_auth_path.parent,
        base_url=f"http://{DEFAULT_LISTEN_HOST}:{port}/v1",
    )
    target = DesktopPoolPaths(
        target.launcher,
        target.launcher_backup,
        target.wrapper,
        target.service,
        config_result.backup_path,
        target.auto_service_state,
    )
    try:
        auto_active = (
            _systemctl("is-active", SERVICE_NAME, runner=runner, check=False).returncode
            == 0
        )
        if target.auto_service_state is not None:
            atomic_write(
                target.auto_service_state,
                "active\n" if auto_active else "inactive\n",
            )
        _systemctl("disable", "--now", SERVICE_NAME, runner=runner, check=False)
        target.wrapper.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(target.wrapper.parent)
        _write_wrapper(target.wrapper, command, token_path)
        if target.launcher.exists() and not target.launcher_backup.exists():
            atomic_write(target.launcher_backup, content)
        target.launcher.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(target.launcher.parent)
        atomic_write(target.launcher, _replace_exec(content, target.wrapper))
        atomic_write(
            target.service,
            _service_text(store, provider, token_path=token_path, port=port),
        )
        _refresh_desktop_database(target.launcher.parent, runner=runner)
        _systemctl("daemon-reload", runner=runner)
        _systemctl("enable", "--now", POOL_DESKTOP_SERVICE, runner=runner)
    except BaseException:
        with suppress(AiAuthSwitchError):
            _systemctl(
                "disable",
                "--now",
                POOL_DESKTOP_SERVICE,
                runner=runner,
                check=False,
            )
        if "auto_active" in locals() and auto_active:
            with suppress(AiAuthSwitchError):
                _systemctl("enable", "--now", SERVICE_NAME, runner=runner)
        if had_launcher:
            atomic_write(target.launcher, content)
        elif target.launcher.exists():
            target.launcher.unlink()
        if config_result.backup_path is not None:
            restore_codex_config(
                provider.active_auth_path.parent,
                config_result.backup_path,
            )
        raise
    return target


def disable_desktop_pool(
    store: AuthStore,
    provider: Provider,
    *,
    paths: DesktopPoolPaths | None = None,
    runner=subprocess.run,
) -> DesktopPoolPaths:
    target = paths or desktop_pool_paths(store)
    _systemctl("disable", "--now", POOL_DESKTOP_SERVICE, runner=runner, check=False)
    if target.launcher_backup.exists():
        try:
            original = target.launcher_backup.read_text(encoding="utf-8")
        except OSError as exc:
            raise AiAuthSwitchError(
                f"failed to restore desktop launcher: {exc}"
            ) from exc
        atomic_write(target.launcher, original)
        target.launcher_backup.unlink()
    elif target.launcher.exists():
        try:
            content = target.launcher.read_text(encoding="utf-8")
        except OSError:
            content = ""
        if POOL_DESKTOP_MARKER in content:
            target.launcher.unlink()
    _refresh_desktop_database(target.launcher.parent, runner=runner)
    _systemctl("daemon-reload", runner=runner, check=False)
    if target.auto_service_state is not None:
        try:
            auto_was_active = (
                target.auto_service_state.read_text(encoding="utf-8").strip()
                == "active"
            )
        except OSError:
            auto_was_active = False
        if auto_was_active:
            _systemctl("enable", "--now", SERVICE_NAME, runner=runner)
        with suppress(OSError):
            target.auto_service_state.unlink()
    config_path = provider.active_auth_path.parent / "config.toml"
    try:
        current_config = parse_toml(config_path.read_text(encoding="utf-8"))
    except (OSError, AiAuthSwitchError):
        current_config = {}
    if current_config.get("model_provider") == "ai-auth-switch-pool":
        backups = sorted(
            config_path.parent.glob(".config.toml.ai-auth-switch-backup.*")
        )
        if backups:
            restore_codex_config(provider.active_auth_path.parent, backups[-1])
    return target


def desktop_pool_status(store: AuthStore, provider: Provider) -> dict[str, Any]:
    target = desktop_pool_paths(store)
    config_path = provider.active_auth_path.parent / "config.toml"
    backups = sorted(config_path.parent.glob(".config.toml.ai-auth-switch-backup.*"))
    return {
        "installed": target.launcher.exists()
        and POOL_DESKTOP_MARKER in target.launcher.read_text(encoding="utf-8"),
        "launcher": str(target.launcher),
        "wrapper": str(target.wrapper),
        "service": str(target.service),
        "token_file": str(default_token_path(store)),
        "config_backup": str(backups[-1]) if backups else None,
    }
