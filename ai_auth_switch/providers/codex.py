from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ai_auth_switch.utils import extract_account_id_from_jwt, extract_email_from_jwt

from .base import Provider


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_string(data: dict[str, Any], *path: str) -> str | None:
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def default_codex_command() -> list[str]:
    configured = os.environ.get("CODEX_BIN")
    if configured and configured.strip():
        return [configured]
    resolved = shutil.which("codex")
    return [resolved or "codex"]


class CodexProvider(Provider):
    def __init__(self, codex_home: Path | None = None, login_command: list[str] | None = None):
        home = (codex_home or default_codex_home()).expanduser()
        super().__init__(
            id="codex",
            active_auth_path=home / "auth.json",
            login_command=login_command or default_codex_command(),
        )

    def auth_identity(self, auth_file: Path) -> str | None:
        data = _read_json(auth_file)
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}

        for candidate in (
            _nested_string(data, "email"),
            _nested_string(data, "account", "email"),
            _nested_string(tokens, "email"),
            _nested_string(tokens, "user_email"),
            _nested_string(tokens, "account_email"),
        ):
            if candidate:
                return f"email:{candidate.lower()}"

        for token_key in ("id_token", "access_token"):
            token = tokens.get(token_key)
            if isinstance(token, str):
                email = extract_email_from_jwt(token)
                if email:
                    return f"email:{email.lower()}"

        account = _nested_string(tokens, "account_id") or _nested_string(data, "account_id")
        if not account:
            for token_key in ("id_token", "access_token"):
                token = tokens.get(token_key)
                if isinstance(token, str):
                    account = extract_account_id_from_jwt(token)
                    if account:
                        break
        if account:
            return f"account:{account}"

        return None

    def infer_profile_name(self, auth_file: Path) -> str | None:
        data = _read_json(auth_file)
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            tokens = {}

        email_candidates = [
            _nested_string(data, "email"),
            _nested_string(data, "account", "email"),
            _nested_string(tokens, "email"),
            _nested_string(tokens, "user_email"),
            _nested_string(tokens, "account_email"),
        ]
        for candidate in email_candidates:
            if candidate:
                return candidate

        for token_key in ("id_token", "access_token"):
            token = tokens.get(token_key)
            if isinstance(token, str):
                email = extract_email_from_jwt(token)
                if email:
                    return email

        account = _nested_string(tokens, "account_id") or _nested_string(data, "account_id")
        if not account:
            for token_key in ("id_token", "access_token"):
                token = tokens.get(token_key)
                if isinstance(token, str):
                    account = extract_account_id_from_jwt(token)
                    if account:
                        break

        auth_mode = _nested_string(data, "auth_mode") or "chatgpt"
        if account:
            prefix = re.sub(r"[^A-Za-z0-9]", "", account)[:8]
            if prefix:
                return f"{auth_mode}-{prefix}"
        return auth_mode
