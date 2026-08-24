from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - dependency install guard
        tomllib = None

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.utils import atomic_write, set_private_permissions

POOL_PROVIDER_ID = "ai-auth-switch-pool"
POOL_TOKEN_ENV = "AI_AUTH_SWITCH_POOL_TOKEN"
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_toml(content: str) -> dict[str, object]:
    if tomllib is None:
        raise AiAuthSwitchError(
            "Python 3.10 requires the optional tomli dependency; "
            "reinstall ai-auth-switch"
        )
    try:
        parsed = tomllib.loads(content)
    except Exception as exc:
        raise AiAuthSwitchError(f"invalid TOML: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class PoolConfigInstallResult:
    config_path: Path
    backup_path: Path | None
    changed: bool
    provider_id: str


def _provider_block(provider_id: str, base_url: str, env_key: str) -> str:
    return (
        f"[model_providers.{provider_id}]\n"
        f"name = {json.dumps('ai-auth-switch local account pool')}\n"
        f"base_url = {json.dumps(base_url)}\n"
        f"env_key = {json.dumps(env_key)}\n"
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
    )


def install_pool_provider(
    codex_home: Path,
    *,
    base_url: str,
    provider_id: str = POOL_PROVIDER_ID,
    env_key: str = POOL_TOKEN_ENV,
    backup: bool = True,
) -> PoolConfigInstallResult:
    if not _PROVIDER_ID_RE.fullmatch(provider_id):
        raise AiAuthSwitchError(f"invalid custom provider id: {provider_id!r}")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_key):
        raise AiAuthSwitchError(f"invalid token environment variable: {env_key!r}")
    if not base_url.startswith(("http://", "https://")):
        raise AiAuthSwitchError("custom provider base URL must be HTTP or HTTPS")
    config_path = codex_home.expanduser() / "config.toml"
    try:
        original = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = ""
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to read Codex config {config_path}: {exc}"
        ) from exc

    model_provider_re = re.compile(r"(?m)^model_provider\s*=\s*[^\n]*$")
    updated = model_provider_re.sub(
        f"model_provider = {json.dumps(provider_id)}", original, count=1
    )
    if updated == original and not model_provider_re.search(original):
        updated = f"model_provider = {json.dumps(provider_id)}\n\n{original}"

    section_re = re.compile(
        rf"(?ms)^\[model_providers\.{re.escape(provider_id)}\]\s*\n.*?(?=^\[|\Z)"
    )
    block = _provider_block(provider_id, base_url, env_key)
    if section_re.search(updated):
        updated = section_re.sub(block, updated, count=1)
    else:
        updated = updated.rstrip() + "\n\n" + block

    try:
        parse_toml(updated)
    except AiAuthSwitchError as exc:
        raise AiAuthSwitchError(
            f"generated Codex config is invalid TOML: {exc}"
        ) from exc
    if updated == original:
        return PoolConfigInstallResult(config_path, None, False, provider_id)

    backup_path = None
    if backup and original:
        backup_path = config_path.with_name(
            f".{config_path.name}.ai-auth-switch-backup.{time.time_ns()}"
        )
        atomic_write(backup_path, original)
    atomic_write(config_path, updated)
    set_private_permissions(config_path)
    return PoolConfigInstallResult(config_path, backup_path, True, provider_id)


def restore_codex_config(codex_home: Path, backup_path: Path) -> Path:
    config_path = codex_home.expanduser() / "config.toml"
    backup = backup_path.expanduser()
    try:
        content = backup.read_text(encoding="utf-8")
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to read config backup {backup}: {exc}"
        ) from exc
    parse_toml(content)
    atomic_write(config_path, content)
    set_private_permissions(config_path)
    return config_path
