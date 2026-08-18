from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from ai_auth_switch.utils import atomic_write

from .base import Provider

SENSITIVE_KEY_PARTS = ("token", "secret", "password", "api_key", "apikey")
AUTH_OVERRIDE_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _safe_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str):
            continue
        lowered = key.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            continue
        if isinstance(child, (str, int, float, bool)) or child is None or isinstance(child, list) and all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in child
        ):
            safe[key] = child
        elif isinstance(child, dict):
            safe[key] = _safe_mapping(child)
    return safe


def default_claude_config_dir() -> tuple[Path, bool]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured and configured.strip():
        return Path(configured).expanduser(), True
    return Path.home() / ".claude", False


def default_claude_command() -> list[str]:
    configured = os.environ.get("CLAUDE_BIN")
    if configured and configured.strip():
        return [configured]
    resolved = shutil.which("claude")
    return [resolved or "claude"]


class ClaudeProvider(Provider):
    """Claude Code OAuth credentials stored in ``.credentials.json``.

    Claude Code keeps account display metadata separately from credentials.
    The store therefore saves a non-secret sidecar for stable identity checks
    and for restoring the correct account information after a profile switch.
    """

    def __init__(
        self,
        config_dir: Path | None = None,
        login_command: list[str] | None = None,
    ):
        if config_dir is None:
            home, explicit = default_claude_config_dir()
        else:
            home, explicit = config_dir.expanduser(), True
        object.__setattr__(self, "config_dir", home)
        object.__setattr__(self, "explicit_config_dir", explicit)
        super().__init__(
            id="claude",
            active_auth_path=home / ".credentials.json",
            login_command=login_command or default_claude_command(),
            login_args=("auth", "login"),
        )

    @property
    def config_state_path(self) -> Path:
        if self.explicit_config_dir:
            return self.config_dir / ".claude.json"
        return Path.home() / ".claude.json"

    def state_path_for_auth(self, auth_file: Path) -> Path:
        try:
            is_active = auth_file == self.active_auth_path or (
                auth_file.exists()
                and self.active_auth_path.exists()
                and auth_file.resolve() == self.active_auth_path.resolve()
            )
        except OSError:
            is_active = auth_file == self.active_auth_path
        if is_active:
            return self.config_state_path
        if auth_file.name == ".credentials.json":
            return auth_file.parent / ".claude.json"
        return auth_file.parent / ".metadata" / f"{auth_file.stem}.json"

    def profile_metadata_path(self, profile_file: Path) -> Path:
        return profile_file.parent / ".metadata" / f"{profile_file.stem}.json"

    def read_profile_metadata(self, auth_file: Path) -> dict[str, Any] | None:
        if auth_file.name != ".credentials.json":
            sidecar = self.profile_metadata_path(auth_file)
            data = _read_json(sidecar)
            return data or None

        credentials = _read_json(auth_file)
        oauth = credentials.get("claudeAiOauth")
        oauth = oauth if isinstance(oauth, dict) else {}
        state = _read_json(self.state_path_for_auth(auth_file))
        account = _safe_mapping(state.get("oauthAccount"))
        metadata: dict[str, Any] = {}
        if account:
            metadata["oauthAccount"] = account
        for key in ("subscriptionType", "rateLimitTier"):
            value = oauth.get(key)
            if isinstance(value, str) and value.strip():
                metadata[key] = value.strip()
        return metadata or None

    @staticmethod
    def _identity_from_metadata(metadata: dict[str, Any]) -> str | None:
        account = metadata.get("oauthAccount")
        if not isinstance(account, dict):
            account = {}
        account_uuid = account.get("accountUuid")
        if isinstance(account_uuid, str) and account_uuid.strip():
            return f"account:{account_uuid.strip().lower()}"
        email = account.get("emailAddress")
        if isinstance(email, str) and email.strip():
            return f"email:{email.strip().lower()}"
        return None

    def auth_identity(self, auth_file: Path) -> str | None:
        metadata = self.read_profile_metadata(auth_file)
        return self._identity_from_metadata(metadata or {})

    def infer_profile_name(self, auth_file: Path) -> str | None:
        metadata = self.read_profile_metadata(auth_file) or {}
        account = metadata.get("oauthAccount")
        if isinstance(account, dict):
            email = account.get("emailAddress")
            if isinstance(email, str) and email.strip():
                return email.strip()
            account_uuid = account.get("accountUuid")
            if isinstance(account_uuid, str) and account_uuid.strip():
                prefix = "".join(ch for ch in account_uuid if ch.isalnum())[:8]
                if prefix:
                    return f"claude-{prefix}"

        # Some credential formats may eventually include identity directly.
        credentials = _read_json(auth_file)
        for container in (credentials, credentials.get("claudeAiOauth")):
            if not isinstance(container, dict):
                continue
            for key in ("email", "emailAddress"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        subscription = metadata.get("subscriptionType")
        label = subscription.strip().lower() if isinstance(subscription, str) else "oauth"
        try:
            digest = hashlib.sha256(auth_file.read_bytes()).hexdigest()[:8]
        except OSError:
            return None
        return f"claude-{label}-{digest}"

    def apply_profile_metadata(
        self,
        metadata: dict[str, Any],
        *,
        config_dir: Path | None = None,
    ) -> None:
        account = metadata.get("oauthAccount")
        if not isinstance(account, dict) or not account:
            return
        target = (
            Path(config_dir).expanduser() / ".claude.json"
            if config_dir is not None
            else self.config_state_path
        )
        state = _read_json(target)
        state["oauthAccount"] = _safe_mapping(account)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            target,
            json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
