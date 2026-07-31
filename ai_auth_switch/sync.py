from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider
from ai_auth_switch.utils import atomic_write, decode_jwt_payload, set_private_permissions


OPENAI_CODEX_PROVIDER = "openai-codex"
OPENAI_CODEX_DEFAULT_PROFILE = "openai-codex:default"
OPENAI_PROVIDER = "openai"
OPENAI_DEFAULT_PROFILE = "openai:default"
HERMES_CODEX_BRIDGE_SOURCE = "manual:codex-cli-bridge"
HERMES_CODEX_CLI_ACCESS_SOURCE = "manual:codex-cli-access-token"
HERMES_CODEX_DEFAULT_MODEL = "gpt-5.5"
PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


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
    atomic_write(
        path,
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
    )


def _summarize_codex_auth(auth_path: Path) -> dict[str, Any]:
    payload = _read_json(auth_path)
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return {}

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        return {}

    id_payload = decode_jwt_payload(str(tokens.get("id_token") or ""))
    access_payload = decode_jwt_payload(access_token)
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
    restart_hermes: bool = True,
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
            hermes_result = SyncResult(
                target="hermes",
                status="skipped",
                message="could not infer Codex profile name for Hermes sync",
            )
        else:
            hermes_result = _sync_hermes_profile(
                profile_name,
                active_auth_path=active,
                store_dir=store_dir,
                hermes_agent_dir=hermes_agent_dir,
            )
        results.append(hermes_result)
        if restart_hermes and hermes_result.status == "synced":
            results.append(_restart_hermes_gateway())
    if sync_openclaw:
        results.append(
            _sync_openclaw_from_codex(
                active_auth_path=active,
                openclaw_state_dir=openclaw_state_dir,
            )
        )
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
    active_auth_path: Path,
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

    return _sync_hermes_codex_cli_access_token(
        profile_name,
        active_auth_path=active_auth_path,
        python=python,
        agent_dir=agent_dir,
    )


def _sync_hermes_codex_cli_access_token(
    profile_name: str,
    *,
    active_auth_path: Path,
    python: Path,
    agent_dir: Path,
) -> SyncResult:
    helper = Path(__file__).with_name("hermes_codex_sync.py")

    env = os.environ.copy()
    env["AI_AUTH_SWITCH_HERMES_AGENT_DIR"] = str(agent_dir)
    env["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"] = profile_name
    env["AI_AUTH_SWITCH_CODEX_AUTH_PATH"] = str(active_auth_path)
    env["AI_AUTH_SWITCH_HERMES_CLI_ACCESS_SOURCE"] = HERMES_CODEX_CLI_ACCESS_SOURCE
    env["AI_AUTH_SWITCH_HERMES_LEGACY_BRIDGE_SOURCE"] = HERMES_CODEX_BRIDGE_SOURCE
    env["AI_AUTH_SWITCH_HERMES_DEFAULT_MODEL"] = HERMES_CODEX_DEFAULT_MODEL
    try:
        completed = subprocess.run(
            [str(python), str(helper)],
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
            message="Hermes Codex CLI access-token sync timed out",
            path=agent_dir,
        )
    except OSError as exc:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"failed to run Hermes Codex CLI access-token sync: {exc}",
            path=agent_dir,
        )

    output = "\n".join(
        line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()
    )
    if completed.returncode != 0:
        return SyncResult(
            target="hermes",
            status="error",
            message=f"Hermes Codex CLI access-token sync exited {completed.returncode}: {_tail(output)}",
            path=agent_dir,
        )

    payload = _last_json_object(output)
    if payload and payload.get("status") == "synced":
        config = payload.get("config")
        suffix = f"; config {config}" if isinstance(config, str) and config else ""
        return SyncResult(
            target="hermes",
            status="synced",
            message=f"Codex CLI access token active for {profile_name}; runtime auto{suffix}",
            path=Path(config).expanduser() if isinstance(config, str) and config else None,
        )
    return SyncResult(
        target="hermes",
        status="synced",
        message=f"Codex CLI access token active for {profile_name}",
        path=agent_dir,
    )


def _sync_openclaw_from_codex(
    *,
    active_auth_path: Path,
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
    sqlite_path = agent_dir / "openclaw-agent.sqlite"
    profiles_path = agent_dir / "auth-profiles.json"
    state_path = agent_dir / "auth-state.json"
    if sqlite_path.exists():
        return _sync_openclaw_sqlite_from_codex(sqlite_path, active_auth_path)
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


def _sync_openclaw_sqlite_from_codex(sqlite_path: Path, active_auth_path: Path) -> SyncResult:
    try:
        profile = _build_openclaw_openai_profile(active_auth_path)
        now_ms = int(time.time() * 1000)
        with sqlite3.connect(sqlite_path) as conn:
            _assert_openclaw_auth_tables(conn, sqlite_path)
            store = _load_openclaw_sqlite_json(
                conn,
                table="auth_profile_store",
                key_column="store_key",
                json_column="store_json",
                key="primary",
                default={"version": 1, "profiles": {}},
                sqlite_path=sqlite_path,
            )
            profiles = store.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                store["profiles"] = profiles
            profiles[OPENAI_DEFAULT_PROFILE] = profile

            state = _load_openclaw_sqlite_json(
                conn,
                table="auth_profile_state",
                key_column="state_key",
                json_column="state_json",
                key="primary",
                default={"version": 1},
                sqlite_path=sqlite_path,
            )
            order = state.get("order")
            if not isinstance(order, dict):
                order = {}
                state["order"] = order
            last_good = state.get("lastGood")
            if not isinstance(last_good, dict):
                last_good = {}
                state["lastGood"] = last_good
            order[OPENAI_PROVIDER] = [OPENAI_DEFAULT_PROFILE]
            last_good[OPENAI_PROVIDER] = OPENAI_DEFAULT_PROFILE
            usage_stats = state.get("usageStats")
            if isinstance(usage_stats, dict):
                usage_stats.pop(OPENAI_DEFAULT_PROFILE, None)

            conn.execute(
                "insert into auth_profile_store(store_key, store_json, updated_at) "
                "values('primary', ?, ?) "
                "on conflict(store_key) do update set "
                "store_json=excluded.store_json, updated_at=excluded.updated_at",
                (json.dumps(store, separators=(",", ":"), ensure_ascii=False), now_ms),
            )
            conn.execute(
                "insert into auth_profile_state(state_key, state_json, updated_at) "
                "values('primary', ?, ?) "
                "on conflict(state_key) do update set "
                "state_json=excluded.state_json, updated_at=excluded.updated_at",
                (json.dumps(state, separators=(",", ":"), ensure_ascii=False), now_ms),
            )
            conn.commit()
        set_private_permissions(sqlite_path)
    except AiAuthSwitchError as exc:
        return SyncResult(
            target="openclaw",
            status="error",
            message=str(exc),
            path=sqlite_path,
        )
    except sqlite3.Error as exc:
        return SyncResult(
            target="openclaw",
            status="error",
            message=f"failed to update OpenClaw SQLite auth store: {exc}",
            path=sqlite_path,
        )

    email = profile.get("email")
    suffix = f" ({email})" if isinstance(email, str) and email else ""
    return SyncResult(
        target="openclaw",
        status="synced",
        message=f"OpenClaw SQLite {OPENAI_DEFAULT_PROFILE}{suffix} now uses active Codex auth",
        path=sqlite_path,
    )


def _assert_openclaw_auth_tables(conn: sqlite3.Connection, sqlite_path: Path) -> None:
    rows = conn.execute(
        "select name from sqlite_master where type='table' "
        "and name in ('auth_profile_store', 'auth_profile_state')"
    ).fetchall()
    found = {str(row[0]) for row in rows}
    missing = {"auth_profile_store", "auth_profile_state"} - found
    if missing:
        raise AiAuthSwitchError(
            f"OpenClaw SQLite auth store missing tables in {sqlite_path}: "
            f"{', '.join(sorted(missing))}"
        )


def _load_openclaw_sqlite_json(
    conn: sqlite3.Connection,
    *,
    table: str,
    key_column: str,
    json_column: str,
    key: str,
    default: dict[str, Any],
    sqlite_path: Path,
) -> dict[str, Any]:
    row = conn.execute(
        f"select {json_column} from {table} where {key_column} = ?",
        (key,),
    ).fetchone()
    if not row or row[0] is None:
        return dict(default)
    try:
        data = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        raise AiAuthSwitchError(
            f"invalid OpenClaw SQLite JSON in {sqlite_path}:{table}/{key}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise AiAuthSwitchError(
            f"expected OpenClaw SQLite JSON object in {sqlite_path}:{table}/{key}"
        )
    return data


def _build_openclaw_openai_profile(active_auth_path: Path) -> dict[str, Any]:
    payload = _read_json(active_auth_path)
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        raise AiAuthSwitchError(f"Codex auth file {active_auth_path} does not contain tokens")

    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    id_token = tokens.get("id_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AiAuthSwitchError(f"Codex auth file {active_auth_path} is missing access_token")
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AiAuthSwitchError(f"Codex auth file {active_auth_path} is missing refresh_token")
    access_token = access_token.strip()
    refresh_token = refresh_token.strip()
    id_token = id_token.strip() if isinstance(id_token, str) and id_token.strip() else None

    access_payload = decode_jwt_payload(access_token)
    id_payload = decode_jwt_payload(id_token or "")
    access_auth = _dict_claim(access_payload, "https://api.openai.com/auth")
    id_auth = _dict_claim(id_payload, "https://api.openai.com/auth")
    access_profile = _dict_claim(access_payload, "https://api.openai.com/profile")

    exp = access_payload.get("exp") or id_payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise AiAuthSwitchError(f"Codex auth file {active_auth_path} token is missing exp")
    expires = int(exp * 1000)

    email = _first_string(
        id_payload.get("email"),
        access_profile.get("email"),
        payload.get("email"),
    )
    account_id = _first_string(
        tokens.get("account_id"),
        id_auth.get("chatgpt_account_id"),
        id_auth.get("account_id"),
        access_auth.get("chatgpt_account_id"),
        access_auth.get("account_id"),
        access_payload.get("account_id"),
    )
    plan = _first_string(
        id_auth.get("chatgpt_plan_type"),
        access_auth.get("chatgpt_plan_type"),
    )

    profile: dict[str, Any] = {
        "type": "oauth",
        "provider": OPENAI_PROVIDER,
        "access": access_token,
        "refresh": refresh_token,
        "expires": expires,
        "label": f"Codex CLI ({email})" if email else "Codex CLI",
    }
    if id_token:
        profile["idToken"] = id_token
    if email:
        profile["email"] = email
    if account_id:
        profile["accountId"] = account_id
    if plan:
        profile["chatgptPlanType"] = plan
    return profile


def _dict_claim(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _sync_systemd_proxy_environment(systemctl: str) -> str | None:
    present = [key for key in PROXY_ENV_KEYS if key in os.environ]
    absent = [key for key in PROXY_ENV_KEYS if key not in os.environ]
    details: list[str] = []

    if present:
        completed = subprocess.run(
            [systemctl, "--user", "import-environment", *present],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            details.append(
                f"proxy import exited {completed.returncode}: {_tail(_captured_output(completed))}"
            )
    if absent:
        completed = subprocess.run(
            [systemctl, "--user", "unset-environment", *absent],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode != 0:
            details.append(
                f"proxy unset exited {completed.returncode}: {_tail(_captured_output(completed))}"
            )
    if details:
        return "; ".join(details)
    return "proxy environment imported" if present else "proxy environment cleared"


def _captured_output(completed: subprocess.CompletedProcess) -> str:
    return "\n".join(
        line
        for line in (completed.stdout + "\n" + completed.stderr).splitlines()
        if line.strip()
    )


def _restart_gateway(service: str) -> SyncResult:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return SyncResult(
            target=service,
            status="skipped",
            message="systemctl not found",
        )

    try:
        active = subprocess.run(
            [systemctl, "--user", "is-active", "--quiet", f"{service}.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SyncResult(
            target=service,
            status="skipped",
            message=f"could not check service state: {exc}",
        )
    if active.returncode != 0:
        return SyncResult(
            target=service,
            status="skipped",
            message=f"{service}.service is not active",
        )

    try:
        proxy_env_message = _sync_systemd_proxy_environment(systemctl)
    except (OSError, subprocess.TimeoutExpired) as exc:
        proxy_env_message = f"proxy environment sync failed: {exc}"

    try:
        restarted = subprocess.run(
            [systemctl, "--user", "restart", f"{service}.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return SyncResult(
            target=service,
            status="error",
            message="restart timed out",
        )
    except OSError as exc:
        return SyncResult(
            target=service,
            status="error",
            message=f"restart failed: {exc}",
        )
    if restarted.returncode != 0:
        return SyncResult(
            target=service,
            status="error",
            message=f"restart exited {restarted.returncode}: {_tail(_captured_output(restarted))}",
        )
    suffix = f"; {proxy_env_message}" if proxy_env_message else ""
    return SyncResult(
        target=service,
        status="synced",
        message=f"service restarted{suffix}",
    )


def _restart_hermes_gateway() -> SyncResult:
    return _restart_gateway("hermes-gateway")


def _restart_openclaw_gateway() -> SyncResult:
    return _restart_gateway("openclaw-gateway")


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
