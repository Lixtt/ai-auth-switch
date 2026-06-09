from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.store import set_private_permissions


OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_DEFAULT_PROFILE = "openai-codex:default"
HERMES_CODEX_BRIDGE_SOURCE = "manual:codex-cli-bridge"
HERMES_CODEX_BRIDGE_TOKEN = "codex-app-server-uses-codex-cli-auth"


@dataclass(frozen=True)
class SyncResult:
    target: str
    status: str
    message: str
    path: Path | None = None

    @property
    def ok(self) -> bool:
        return self.status in {"synced", "skipped"}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError as exc:
        raise AiAuthSwitchError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AiAuthSwitchError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AiAuthSwitchError(f"expected JSON object in {path}")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    set_private_permissions(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    set_private_permissions(tmp)
    os.replace(tmp, path)
    set_private_permissions(path)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _summarize_codex_auth(auth_path: Path) -> dict[str, Any]:
    payload = _read_json(auth_path)
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return {}

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return {}

    id_payload = _decode_jwt_payload(str(tokens.get("id_token") or ""))
    access_payload = _decode_jwt_payload(access_token)
    auth_claims = access_payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    profile = access_payload.get("https://api.openai.com/profile")
    if not isinstance(profile, dict):
        profile = {}

    email = id_payload.get("email") or profile.get("email")
    account_id = (
        tokens.get("account_id")
        or auth_claims.get("chatgpt_account_id")
        or auth_claims.get("account_id")
        or access_payload.get("account_id")
    )
    return {
        "email": email if isinstance(email, str) else None,
        "account_id": account_id if isinstance(account_id, str) else None,
        "access_exp": access_payload.get("exp"),
        "last_refresh": payload.get("last_refresh")
        if isinstance(payload.get("last_refresh"), str)
        else None,
    }


def sync_codex_dependents(
    provider: Provider,
    *,
    sync_hermes: bool = True,
    sync_openclaw: bool = True,
    restart_openclaw: bool = True,
    hermes_login: bool = False,
    hermes_profile_name: str | None = None,
    store_dir: Path | None = None,
    hermes_agent_dir: Path | None = None,
    openclaw_state_dir: Path | None = None,
) -> list[SyncResult]:
    if provider.id != "codex":
        raise AiAuthSwitchError(f"dependent sync is only supported for codex, not {provider.id}")

    active = provider.active_auth_path
    if not active.exists():
        raise AiAuthSwitchError(f"active Codex auth file not found: {active}")
    _summarize_codex_auth(active)

    results: list[SyncResult] = []
    if sync_hermes:
        del hermes_login  # Kept for CLI compatibility; Hermes now uses Codex CLI bridge.
        profile_name = hermes_profile_name or provider.infer_profile_name(active)
        if not profile_name:
            results.append(
                SyncResult(
                    target="hermes",
                    status="skipped",
                    message="could not infer Codex profile name for Hermes sync",
                )
            )
        else:
            results.append(
                _sync_hermes_profile(
                    profile_name,
                    store_dir=store_dir,
                    hermes_agent_dir=hermes_agent_dir,
                )
            )
    if sync_openclaw:
        results.append(_sync_openclaw_from_codex(openclaw_state_dir=openclaw_state_dir))
        if restart_openclaw:
            results.append(_restart_openclaw_gateway())
    return results


def _default_hermes_agent_dir(hermes_agent_dir: Path | None = None) -> Path:
    if hermes_agent_dir is not None:
        return hermes_agent_dir.expanduser()
    configured_agent_dir = os.environ.get("HERMES_AGENT_DIR", "").strip()
    if configured_agent_dir:
        return Path(configured_agent_dir).expanduser()
    return Path.home() / ".hermes" / "hermes-agent"


def _sync_hermes_profile(
    profile_name: str,
    *,
    store_dir: Path | None = None,
    hermes_agent_dir: Path | None = None,
) -> SyncResult:
    del store_dir
    agent_dir = _default_hermes_agent_dir(hermes_agent_dir)
    python = agent_dir / "venv" / "bin" / "python"
    auth_module = agent_dir / "hermes_cli" / "auth.py"
    if not python.exists() or not os.access(python, os.X_OK) or not auth_module.exists():
        return SyncResult(
            target="hermes",
            status="skipped",
            message="Hermes install path not found",
            path=agent_dir,
        )

    return _sync_hermes_codex_cli_bridge(profile_name, python=python, agent_dir=agent_dir)


def _sync_hermes_codex_cli_bridge(
    profile_name: str,
    *,
    python: Path,
    agent_dir: Path,
) -> SyncResult:
    helper = r"""
import json
import os
import sys
from pathlib import Path

agent_dir = Path(os.environ["AI_AUTH_SWITCH_HERMES_AGENT_DIR"])
sys.path.insert(0, str(agent_dir))

from hermes_cli.auth import (  # noqa: E402
    DEFAULT_CODEX_BASE_URL,
    _auth_store_lock,
    _load_auth_store,
    _save_auth_store,
    _update_config_for_provider,
)
from hermes_cli.codex_runtime_switch import apply as apply_codex_runtime  # noqa: E402
from hermes_cli.config import load_config, save_config  # noqa: E402
from agent.credential_pool import load_pool  # noqa: E402


profile_name = os.environ["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"]
bridge_source = os.environ["AI_AUTH_SWITCH_HERMES_BRIDGE_SOURCE"]
bridge_token = os.environ["AI_AUTH_SWITCH_HERMES_BRIDGE_TOKEN"]
base_url = DEFAULT_CODEX_BASE_URL

config_path = _update_config_for_provider("openai-codex", base_url)
config = load_config()
model_cfg = config.get("model")
if not isinstance(model_cfg, dict):
    model_cfg = {}
    config["model"] = model_cfg
model_cfg["provider"] = "openai-codex"
model_cfg["base_url"] = base_url
runtime_status = apply_codex_runtime(config, "codex_app_server", persist_callback=save_config)
if not runtime_status.success:
    raise SystemExit(runtime_status.message or "failed to enable codex_app_server runtime")

with _auth_store_lock():
    auth_store = _load_auth_store()
    providers = auth_store.get("providers")
    if isinstance(providers, dict):
        providers.pop("openai-codex", None)
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}
        auth_store["credential_pool"] = pool
    existing = pool.get("openai-codex")
    retained = []
    if isinstance(existing, list):
        retained = [
            item
            for item in existing
            if isinstance(item, dict)
            and item.get("source") not in {"device_code", "manual:device_code", bridge_source}
        ]
    retained.insert(0, {
        "id": "codex-cli-bridge",
        "label": f"Codex CLI ({profile_name})",
        "auth_type": "api_key",
        "priority": 0,
        "source": bridge_source,
        "access_token": bridge_token,
        "base_url": base_url,
    })
    for index, item in enumerate(retained):
        if isinstance(item, dict):
            item["priority"] = index
    pool["openai-codex"] = retained
    suppressed = auth_store.get("suppressed_sources")
    if isinstance(suppressed, dict):
        suppressed.pop("openai-codex", None)
        if not suppressed:
            auth_store.pop("suppressed_sources", None)
    auth_store["active_provider"] = "openai-codex"
    _save_auth_store(auth_store)

try:
    load_pool("openai-codex")
except Exception:
    pass
print(json.dumps({
    "status": "synced",
    "config": str(config_path),
    "runtime": "codex_app_server",
    "profile": profile_name,
}, ensure_ascii=False))
"""

    env = os.environ.copy()
    env["AI_AUTH_SWITCH_HERMES_AGENT_DIR"] = str(agent_dir)
    env["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"] = profile_name
    env["AI_AUTH_SWITCH_HERMES_BRIDGE_SOURCE"] = HERMES_CODEX_BRIDGE_SOURCE
    env["AI_AUTH_SWITCH_HERMES_BRIDGE_TOKEN"] = HERMES_CODEX_BRIDGE_TOKEN
    try:
        completed = subprocess.run(
            [str(python), "-c", helper],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            target="hermes",
            status="error",
            message="Hermes Codex CLI bridge sync timed out",
            path=agent_dir,
        )
    except OSError as exc:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"failed to run Hermes Codex CLI bridge sync: {exc}",
            path=agent_dir,
        )

    output = "\n".join(
        line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()
    )
    if completed.returncode != 0:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"Hermes Codex CLI bridge sync exited {completed.returncode}: {_tail(output)}",
            path=agent_dir,
        )

    payload = _last_json_object(output)
    if payload and payload.get("status") == "synced":
        config = payload.get("config")
        suffix = f"; config {config}" if isinstance(config, str) and config else ""
        return SyncResult(
            target="hermes",
            status="synced",
            message=f"Codex CLI bridge active for {profile_name}; runtime codex_app_server{suffix}",
            path=Path(config).expanduser() if isinstance(config, str) and config else None,
        )
    return SyncResult(
        target="hermes",
        status="synced",
        message=f"Codex CLI bridge active for {profile_name}",
        path=agent_dir,
    )


def _sync_openclaw_from_codex(
    *,
    openclaw_state_dir: Path | None = None,
) -> SyncResult:
    home = Path.home()
    configured_state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if openclaw_state_dir is not None:
        base = openclaw_state_dir.expanduser()
    elif configured_state_dir:
        base = Path(configured_state_dir).expanduser()
    else:
        base = home / ".openclaw"
    agent_dir = base / "agents" / "main" / "agent"
    profiles_path = agent_dir / "auth-profiles.json"
    state_path = agent_dir / "auth-state.json"
    if not state_path.exists():
        return SyncResult(
            target="openclaw",
            status="skipped",
            message="OpenClaw auth state file not found",
            path=state_path,
        )

    removed_local_default = False
    if profiles_path.exists():
        profiles = _read_json(profiles_path)
        profile_entries = profiles.get("profiles")
        if isinstance(profile_entries, dict) and OPENAI_CODEX_DEFAULT_PROFILE in profile_entries:
            del profile_entries[OPENAI_CODEX_DEFAULT_PROFILE]
            removed_local_default = True
            _write_json(profiles_path, profiles)

    state = _read_json(state_path)
    order = state.get("order")
    if not isinstance(order, dict):
        order = {}
        state["order"] = order
    last_good = state.get("lastGood")
    if not isinstance(last_good, dict):
        last_good = {}
        state["lastGood"] = last_good

    order[OPENAI_CODEX_PROVIDER] = [OPENAI_CODEX_DEFAULT_PROFILE]
    last_good[OPENAI_CODEX_PROVIDER] = OPENAI_CODEX_DEFAULT_PROFILE

    usage_stats = state.get("usageStats")
    if isinstance(usage_stats, dict):
        usage_stats.pop(OPENAI_CODEX_DEFAULT_PROFILE, None)

    _write_json(state_path, state)
    removed = "; removed local default profile" if removed_local_default else ""
    return SyncResult(
        target="openclaw",
        status="synced",
        message=f"default profile now uses Codex CLI auth{removed}",
        path=state_path,
    )


def _restart_openclaw_gateway() -> SyncResult:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return SyncResult(
            target="openclaw-gateway",
            status="skipped",
            message="systemctl not found",
        )

    try:
        active = subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", "openclaw-gateway.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SyncResult(
            target="openclaw-gateway",
            status="skipped",
            message=f"could not check service state: {exc}",
        )
    if active.returncode != 0:
        return SyncResult(
            target="openclaw-gateway",
            status="skipped",
            message="openclaw-gateway.service is not active",
        )

    try:
        restarted = subprocess.run(
            [systemctl, "--user", "restart", "openclaw-gateway.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            target="openclaw-gateway",
            status="error",
            message="restart timed out",
        )
    except OSError as exc:
        return SyncResult(
            target="openclaw-gateway",
            status="error",
            message=f"restart failed: {exc}",
        )
    if restarted.returncode != 0:
        output = "\n".join(
            line
            for line in (restarted.stdout + "\n" + restarted.stderr).splitlines()
            if line.strip()
        )
        return SyncResult(
            target="openclaw-gateway",
            status="error",
            message=f"restart exited {restarted.returncode}: {_tail(output)}",
        )
    return SyncResult(
        target="openclaw-gateway",
        status="synced",
        message="service restarted",
    )


def _last_json_object(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _tail(output: str, limit: int = 800) -> str:
    text = output.strip()
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]
