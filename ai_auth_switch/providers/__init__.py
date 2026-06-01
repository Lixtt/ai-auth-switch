from __future__ import annotations

from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError

from .base import Provider
from .codex import CodexProvider


def get_provider(provider_id: str, *, codex_home: Path | None = None) -> Provider:
    normalized = provider_id.strip().lower()
    if normalized == "codex":
        return CodexProvider(codex_home=codex_home)
    raise AiAuthSwitchError(f"unsupported provider: {provider_id}")


__all__ = ["CodexProvider", "Provider", "get_provider"]
