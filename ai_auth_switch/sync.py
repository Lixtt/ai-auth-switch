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
from ai_auth_switch.store import sanitize_profile_name, set_private_permissions


OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_DEFAULT_PROFILE = "openai-codex:default"
HERMES_CODEX_DEFAULT_BASE_URL = "https://chatgpt.com/backend-api/codex"


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
                    login=hermes_login,
                    store_dir=store_dir,
                    hermes_agent_dir=hermes_agent_dir,
                )
            )
    if sync_openclaw:
        results.append(_sync_openclaw_from_codex(openclaw_state_dir=openclaw_state_dir))
        if restart_openclaw:
            results.append(_restart_openclaw_gateway())
    return results


def _dependent_store_dir(store_dir: Path | None = None) -> Path:
    if store_dir is not None:
        return store_dir.expanduser()
    configured = os.environ.get("AI_AUTH_SWITCH_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser() / "ai-auth-switch"
    return Path.home() / ".local" / "share" / "ai-auth-switch"


def _hermes_snapshot_path(profile_name: str, store_dir: Path | None = None) -> Path:
    return (
        _dependent_store_dir(store_dir)
        / "dependent-auth"
        / "hermes"
        / "codex"
        / f"{sanitize_profile_name(profile_name)}.json"
    )


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
    login: bool,
    store_dir: Path | None = None,
    hermes_agent_dir: Path | None = None,
) -> SyncResult:
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

    snapshot = _hermes_snapshot_path(profile_name, store_dir)
    _sync_current_hermes_state_back_to_snapshot(store_dir=store_dir)

    if login:
        return _login_hermes_codex_profile(
            profile_name,
            snapshot,
            python=python,
            agent_dir=agent_dir,
        )

    if not snapshot.exists():
        return SyncResult(
            target="hermes",
            status="skipped",
            message=(
                f"no Hermes Codex session saved for {profile_name}; "
                f"run `ai-auth-switch auth login codex {profile_name}` to create one"
            ),
            path=snapshot,
        )

    return _activate_hermes_codex_snapshot(snapshot, python=python, agent_dir=agent_dir)


def _login_hermes_codex_profile(
    profile_name: str,
    snapshot: Path,
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
    _codex_device_code_login,
    _load_auth_store,
    _load_provider_state,
    _save_codex_tokens,
    _update_config_for_provider,
    unsuppress_credential_source,
)
from agent.credential_pool import load_pool  # noqa: E402


profile_name = os.environ["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"]
snapshot_path = Path(os.environ["AI_AUTH_SWITCH_HERMES_SNAPSHOT_PATH"]).expanduser()

print(f"Signing in to Hermes OpenAI Codex for profile {profile_name}...")
creds = _codex_device_code_login()
base_url = creds.get("base_url", DEFAULT_CODEX_BASE_URL)
try:
    unsuppress_credential_source("openai-codex", "device_code")
except Exception:
    pass
_save_codex_tokens(creds["tokens"], creds.get("last_refresh"), label=profile_name)
config_path = _update_config_for_provider("openai-codex", base_url)
try:
    load_pool("openai-codex")
except Exception:
    pass
auth_store = _load_auth_store()
state = _load_provider_state(auth_store, "openai-codex") or {}
pool = auth_store.get("credential_pool")
pool_entries = []
if isinstance(pool, dict) and isinstance(pool.get("openai-codex"), list):
    pool_entries = [item for item in pool["openai-codex"] if isinstance(item, dict)]

snapshot_path.parent.mkdir(parents=True, exist_ok=True)
os.chmod(snapshot_path.parent, 0o700)
tmp = snapshot_path.with_name(f".{snapshot_path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps({
    "version": 1,
    "provider": "openai-codex",
    "profile": profile_name,
    "base_url": base_url,
    "provider_state": state,
    "credential_pool": pool_entries,
}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, snapshot_path)
os.chmod(snapshot_path, 0o600)

print(json.dumps({
    "status": "synced",
    "config": str(config_path),
    "snapshot": str(snapshot_path),
}, ensure_ascii=False))
"""

    env = os.environ.copy()
    env["AI_AUTH_SWITCH_HERMES_AGENT_DIR"] = str(agent_dir)
    env["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"] = profile_name
    env["AI_AUTH_SWITCH_HERMES_SNAPSHOT_PATH"] = str(snapshot)
    try:
        completed = subprocess.run(
            [str(python), "-c", helper],
            check=False,
            env=env,
            timeout=15 * 60,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            target="hermes",
            status="error",
            message="Hermes Codex login timed out",
            path=agent_dir,
        )
    except OSError as exc:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"failed to run Hermes Codex login: {exc}",
            path=agent_dir,
        )

    if completed.returncode != 0:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"Hermes Codex login exited {completed.returncode}",
            path=agent_dir,
        )

    return SyncResult(
        target="hermes",
        status="synced",
        message=f"independent Codex session saved for {profile_name}",
        path=snapshot,
    )


def _activate_hermes_codex_snapshot(
    snapshot: Path,
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
    _save_provider_state,
    _update_config_for_provider,
)
from agent.credential_pool import load_pool  # noqa: E402


def _pool_entries_from_state(state, base_url):
    tokens = state.get("tokens") if isinstance(state, dict) else None
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return []
    entry = {
        "source": "device_code",
        "auth_type": "oauth",
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token"),
        "base_url": base_url,
        "last_refresh": state.get("last_refresh"),
        "label": state.get("label") or "device_code",
    }
    return [{key: value for key, value in entry.items() if value is not None}]


def _unsuppress_device_code(auth_store):
    suppressed = auth_store.get("suppressed_sources")
    if not isinstance(suppressed, dict):
        return
    provider_list = suppressed.get("openai-codex")
    if not isinstance(provider_list, list):
        return
    while "device_code" in provider_list:
        provider_list.remove("device_code")
    if not provider_list:
        suppressed.pop("openai-codex", None)
    if not suppressed:
        auth_store.pop("suppressed_sources", None)

snapshot_path = Path(os.environ["AI_AUTH_SWITCH_HERMES_SNAPSHOT_PATH"]).expanduser()
snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
state = snapshot.get("provider_state")
if not isinstance(state, dict):
    raise SystemExit("invalid Hermes snapshot: missing provider_state")
pool_entries = snapshot.get("credential_pool")
if pool_entries is not None and not isinstance(pool_entries, list):
    raise SystemExit("invalid Hermes snapshot: credential_pool must be a list")

base_url = str(snapshot.get("base_url") or DEFAULT_CODEX_BASE_URL).strip().rstrip("/")
active_pool = [
    item for item in (pool_entries or _pool_entries_from_state(state, base_url))
    if isinstance(item, dict)
]
with _auth_store_lock():
    auth_store = _load_auth_store()
    _save_provider_state(auth_store, "openai-codex", state)
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}
        auth_store["credential_pool"] = pool
    pool["openai-codex"] = active_pool
    _unsuppress_device_code(auth_store)
    auth_store["active_provider"] = "openai-codex"
    _save_auth_store(auth_store)

try:
    load_pool("openai-codex")
except Exception:
    pass
config_path = _update_config_for_provider("openai-codex", base_url)
print(json.dumps({"status": "synced", "config": str(config_path)}, ensure_ascii=False))
"""

    env = os.environ.copy()
    env["AI_AUTH_SWITCH_HERMES_AGENT_DIR"] = str(agent_dir)
    env["AI_AUTH_SWITCH_HERMES_SNAPSHOT_PATH"] = str(snapshot)
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
            message="Hermes Codex activation timed out",
            path=agent_dir,
        )
    except OSError as exc:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"failed to run Hermes Codex activation: {exc}",
            path=agent_dir,
        )

    output = "\n".join(
        line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()
    )
    if completed.returncode != 0:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"Hermes Codex activation exited {completed.returncode}",
            path=agent_dir,
        )

    payload = _last_json_object(output)
    if payload and payload.get("status") == "synced":
        config = payload.get("config")
        suffix = f"; config {config}" if isinstance(config, str) and config else ""
        return SyncResult(
            target="hermes",
            status="synced",
            message=f"activated independent Codex session{suffix}",
            path=Path(config).expanduser() if isinstance(config, str) and config else None,
        )
    return SyncResult(
        target="hermes",
        status="synced",
        message="activated independent Codex session",
        path=agent_dir,
    )


def _sync_current_hermes_state_back_to_snapshot(*, store_dir: Path | None = None) -> None:
    auth_path = Path(os.environ.get("HERMES_HOME", "")).expanduser() / "auth.json"
    if not os.environ.get("HERMES_HOME", "").strip():
        auth_path = Path.home() / ".hermes" / "auth.json"
    if not auth_path.exists():
        return

    try:
        auth_store = _read_json(auth_path)
    except AiAuthSwitchError:
        return
    provider_state = (auth_store.get("providers") or {}).get("openai-codex")
    if not isinstance(provider_state, dict):
        return
    pool_entries = _hermes_codex_pool_entries(auth_store)
    identity = _codex_provider_state_identity(provider_state) or _codex_pool_entries_identity(
        pool_entries
    )
    if not identity:
        return

    root = _dependent_store_dir(store_dir) / "dependent-auth" / "hermes" / "codex"
    if not root.exists():
        return
    for snapshot in sorted(root.glob("*.json")):
        try:
            data = _read_json(snapshot)
        except AiAuthSwitchError:
            continue
        existing_state = data.get("provider_state")
        if not isinstance(existing_state, dict):
            continue
        existing_pool = data.get("credential_pool")
        if not isinstance(existing_pool, list):
            existing_pool = []
        existing_identity = _codex_provider_state_identity(
            existing_state
        ) or _codex_pool_entries_identity(existing_pool)
        if existing_identity != identity:
            continue
        data["provider_state"] = provider_state
        data["credential_pool"] = pool_entries
        _write_json(snapshot, data)
        return


def _hermes_codex_pool_entries(auth_store: dict[str, Any]) -> list[dict[str, Any]]:
    pool = auth_store.get("credential_pool")
    if isinstance(pool, dict):
        entries = pool.get("openai-codex")
        if isinstance(entries, list):
            return [dict(item) for item in entries if isinstance(item, dict)]
    provider_state = (auth_store.get("providers") or {}).get("openai-codex")
    if isinstance(provider_state, dict):
        generated = _hermes_codex_pool_entries_from_provider_state(provider_state)
        if generated:
            return generated
    return []


def _hermes_codex_pool_entries_from_provider_state(
    provider_state: dict[str, Any],
) -> list[dict[str, Any]]:
    tokens = provider_state.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return []
    entry = {
        "source": "device_code",
        "auth_type": "oauth",
        "access_token": tokens.get("access_token"),
        "refresh_token": tokens.get("refresh_token"),
        "base_url": HERMES_CODEX_DEFAULT_BASE_URL,
        "last_refresh": provider_state.get("last_refresh"),
        "label": provider_state.get("label") or "device_code",
    }
    return [{key: value for key, value in entry.items() if value is not None}]


def _codex_pool_entries_identity(entries: list[Any]) -> str | None:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        identity = _codex_provider_state_identity(entry)
        if identity:
            return identity
    return None


def _codex_provider_state_identity(provider_state: dict[str, Any]) -> str | None:
    tokens = provider_state.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {
            key: value
            for key, value in {
                "id_token": provider_state.get("id_token"),
                "access_token": provider_state.get("access_token"),
                "refresh_token": provider_state.get("refresh_token"),
                "account_id": provider_state.get("account_id"),
            }.items()
            if value is not None
        }

    id_payload = _decode_jwt_payload(str(tokens.get("id_token") or ""))
    access_payload = _decode_jwt_payload(str(tokens.get("access_token") or ""))
    profile = access_payload.get("https://api.openai.com/profile")
    if not isinstance(profile, dict):
        profile = {}
    auth_claims = access_payload.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}

    email = id_payload.get("email") or profile.get("email")
    if isinstance(email, str) and email.strip():
        return f"email:{email.strip().lower()}"

    account_id = (
        tokens.get("account_id")
        or auth_claims.get("chatgpt_account_id")
        or auth_claims.get("account_id")
        or access_payload.get("account_id")
    )
    if isinstance(account_id, str) and account_id.strip():
        return f"account:{account_id.strip()}"
    return None


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
