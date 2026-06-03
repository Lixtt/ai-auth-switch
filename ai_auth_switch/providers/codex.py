from __future__ import annotations

import base64
import json
import os
import re
import shutil
from pathlib import Path

from .base import Provider


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) < 2:
        return {}

    payload = parts[1].replace("-", "+").replace("_", "/")
    padding = len(payload) % 4
    if padding == 2:
        payload += "=="
    elif padding == 3:
        payload += "="
    elif padding == 1:
        return {}

    try:
        raw = base64.b64decode(payload)
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _extract_email_from_jwt(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        email = profile.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()

    email = payload.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def _extract_account_id_from_jwt(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    for key in ("chatgpt_account_id", "account_id", "sub"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        value = auth.get("chatgpt_account_id") or auth.get("account_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_json(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nested_string(data: dict, *path: str) -> str | None:
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
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}

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
                email = _extract_email_from_jwt(token)
                if email:
                    return f"email:{email.lower()}"

        account = _nested_string(tokens, "account_id") or _nested_string(data, "account_id")
        if not account:
            for token_key in ("id_token", "access_token"):
                token = tokens.get(token_key)
                if isinstance(token, str):
                    account = _extract_account_id_from_jwt(token)
                    if account:
                        break
        if account:
            return f"account:{account}"

        return None

    def infer_profile_name(self, auth_file: Path) -> str | None:
        data = _read_json(auth_file)
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}

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
                email = _extract_email_from_jwt(token)
                if email:
                    return email

        account = _nested_string(tokens, "account_id") or _nested_string(data, "account_id")
        if not account:
            for token_key in ("id_token", "access_token"):
                token = tokens.get(token_key)
                if isinstance(token, str):
                    account = _extract_account_id_from_jwt(token)
                    if account:
                        break

        auth_mode = _nested_string(data, "auth_mode") or "chatgpt"
        if account:
            prefix = re.sub(r"[^A-Za-z0-9]", "", account)[:8]
            if prefix:
                return f"{auth_mode}-{prefix}"
        return auth_mode
