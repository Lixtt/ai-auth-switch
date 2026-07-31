#!/usr/bin/env python3
"""Point Hermes's ``openai-codex`` credential pool at the active Codex CLI auth.

Runs under the Hermes agent virtualenv. All inputs arrive via environment
variables set by :func:`ai_auth_switch.sync._sync_hermes_codex_cli_access_token`,
so the script stays self-contained and importable by either Python runtime.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


def main() -> int:
    agent_dir = Path(os.environ["AI_AUTH_SWITCH_HERMES_AGENT_DIR"])
    sys.path.insert(0, str(agent_dir))

    from hermes_cli.auth import (  # noqa: E402
        DEFAULT_CODEX_BASE_URL,
        _auth_store_lock,
        _load_auth_store,
        _save_auth_store,
        _update_config_for_provider,
    )
    from hermes_cli.codex_runtime_switch import set_runtime  # noqa: E402
    from hermes_cli.config import load_config, save_config  # noqa: E402
    from agent.credential_pool import load_pool  # noqa: E402

    profile_name = os.environ["AI_AUTH_SWITCH_HERMES_PROFILE_NAME"]
    active_auth_path = Path(os.environ["AI_AUTH_SWITCH_CODEX_AUTH_PATH"])
    cli_access_source = os.environ["AI_AUTH_SWITCH_HERMES_CLI_ACCESS_SOURCE"]
    legacy_bridge_source = os.environ["AI_AUTH_SWITCH_HERMES_LEGACY_BRIDGE_SOURCE"]
    base_url = DEFAULT_CODEX_BASE_URL

    try:
        codex_auth = json.loads(active_auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"failed to read Codex auth file {active_auth_path}: {exc}")
    tokens = codex_auth.get("tokens")
    if not isinstance(tokens, dict):
        raise SystemExit(f"Codex auth file {active_auth_path} does not contain tokens")
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise SystemExit(f"Codex auth file {active_auth_path} is missing access_token")
    access_token = access_token.strip()

    access_payload = _decode_jwt_payload(access_token)
    expires_at_ms = None
    exp = access_payload.get("exp")
    if isinstance(exp, (int, float)):
        expires_at_ms = int(exp * 1000)

    config_path = _update_config_for_provider("openai-codex", base_url)
    config = load_config()
    model_cfg = config.get("model")
    if not isinstance(model_cfg, dict):
        model_cfg = {}
        config["model"] = model_cfg
    model_cfg["provider"] = "openai-codex"
    model_cfg["base_url"] = base_url
    current_default = str(model_cfg.get("default") or "").strip()
    if not current_default.startswith("gpt-"):
        model_cfg["default"] = os.environ["AI_AUTH_SWITCH_HERMES_DEFAULT_MODEL"]
    set_runtime(config, "auto")
    save_config(config)

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
                and item.get("source")
                not in {
                    "device_code",
                    "manual:device_code",
                    legacy_bridge_source,
                    cli_access_source,
                }
            ]
        cli_entry = {
            "id": "codex-cli-access-token",
            "label": f"Codex CLI ({profile_name})",
            "auth_type": "api_key",
            "priority": 0,
            "source": cli_access_source,
            "access_token": access_token,
            "base_url": base_url,
            "last_status": None,
            "last_status_at": None,
            "last_error_code": None,
            "last_error_reason": None,
            "last_error_message": None,
            "last_error_reset_at": None,
            "request_count": 0,
        }
        last_refresh = codex_auth.get("last_refresh")
        if isinstance(last_refresh, str) and last_refresh.strip():
            cli_entry["last_refresh"] = last_refresh.strip()
        if expires_at_ms is not None:
            cli_entry["expires_at_ms"] = expires_at_ms
        retained.insert(0, cli_entry)
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
    print(
        json.dumps(
            {
                "status": "synced",
                "config": str(config_path),
                "runtime": "auto",
                "profile": profile_name,
                "expires_at_ms": expires_at_ms,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
