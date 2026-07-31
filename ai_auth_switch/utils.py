from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any


def decode_jwt_payload(token: str) -> dict[str, Any]:
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


def extract_email_from_jwt(token: str) -> str | None:
    payload = decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict):
        email = profile.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()

    email = payload.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def extract_account_id_from_jwt(token: str) -> str | None:
    payload = decode_jwt_payload(token)
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


def set_private_permissions(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    except OSError:
        pass


def atomic_write(path: Path, data: str) -> None:
    """Atomically replace *path* with *data*, keeping private permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    set_private_permissions(path.parent)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
    tmp.write_text(data, encoding="utf-8")
    set_private_permissions(tmp)
    os.replace(tmp, path)
    set_private_permissions(path)
