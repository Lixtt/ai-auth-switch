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
        results.append(_sync_hermes_from_codex(active, hermes_agent_dir=hermes_agent_dir))
    if sync_openclaw:
        results.append(_sync_openclaw_from_codex(openclaw_state_dir=openclaw_state_dir))
        if restart_openclaw:
            results.append(_restart_openclaw_gateway())
    return results


def _sync_hermes_from_codex(
    auth_path: Path,
    *,
    hermes_agent_dir: Path | None = None,
) -> SyncResult:
    home = Path.home()
    configured_agent_dir = os.environ.get("HERMES_AGENT_DIR", "").strip()
    if hermes_agent_dir is not None:
        agent_dir = hermes_agent_dir.expanduser()
    elif configured_agent_dir:
        agent_dir = Path(configured_agent_dir).expanduser()
    else:
        agent_dir = home / ".hermes" / "hermes-agent"
    python = agent_dir / "venv" / "bin" / "python"
    auth_module = agent_dir / "hermes_cli" / "auth.py"
    if not python.exists() or not os.access(python, os.X_OK) or not auth_module.exists():
        return SyncResult(
            target="hermes",
            status="skipped",
            message="Hermes install path not found",
            path=agent_dir,
        )

    helper = r"""
import base64
import datetime as dt
import json
import os
import sys
from pathlib import Path

agent_dir = Path(os.environ["AI_AUTH_SWITCH_HERMES_AGENT_DIR"])
sys.path.insert(0, str(agent_dir))

from hermes_cli.auth import (  # noqa: E402
    DEFAULT_CODEX_BASE_URL,
    _import_codex_cli_tokens,
    _save_codex_tokens,
    _update_config_for_provider,
)


def decode_jwt(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


tokens = _import_codex_cli_tokens()
if not tokens:
    print(json.dumps({"status": "skipped", "reason": "no_valid_codex_cli_tokens"}))
    raise SystemExit(0)

source = Path(os.environ["CODEX_HOME"]).expanduser() / "auth.json"
source_payload = json.loads(source.read_text(encoding="utf-8"))
last_refresh = source_payload.get("last_refresh")
if not isinstance(last_refresh, str):
    last_refresh = None

_save_codex_tokens(tokens, last_refresh)
base_url = os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/") or DEFAULT_CODEX_BASE_URL
config_path = _update_config_for_provider("openai-codex", base_url)

id_payload = decode_jwt(tokens.get("id_token") or "")
access_payload = decode_jwt(tokens.get("access_token") or "")
access_exp = access_payload.get("exp")
print(json.dumps({
    "status": "synced",
    "email": id_payload.get("email"),
    "account_id": tokens.get("account_id"),
    "access_exp": dt.datetime.fromtimestamp(access_exp, dt.timezone.utc).isoformat() if access_exp else None,
    "config": str(config_path),
}, ensure_ascii=False))
"""

    env = os.environ.copy()
    env["AI_AUTH_SWITCH_HERMES_AGENT_DIR"] = str(agent_dir)
    env["CODEX_HOME"] = str(auth_path.parent)
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
            message="Hermes sync timed out",
            path=agent_dir,
        )
    except OSError as exc:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"failed to run Hermes sync: {exc}",
            path=agent_dir,
        )

    output = "\n".join(
        line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()
    )
    if completed.returncode != 0:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"Hermes sync exited {completed.returncode}",
            path=agent_dir,
        )

    payload = _last_json_object(output)
    if payload and payload.get("status") == "synced":
        config = payload.get("config")
        suffix = f"; config {config}" if isinstance(config, str) and config else ""
        return SyncResult(
            target="hermes",
            status="synced",
            message=f"openai-codex credentials imported{suffix}",
            path=Path(config).expanduser() if isinstance(config, str) and config else None,
        )
    if payload and payload.get("status") == "skipped":
        reason = payload.get("reason")
        return SyncResult(
            target="hermes",
            status="skipped",
            message=str(reason or "Hermes skipped"),
            path=agent_dir,
        )
    return SyncResult(
        target="hermes",
        status="synced",
        message="openai-codex credentials imported",
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
