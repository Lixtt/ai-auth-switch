from __future__ import annotations

from pathlib import Path

from ai_auth_switch.errors import AiAuthSwitchError

from .base import Provider
from .claude import ClaudeProvider
from .codex import CodexProvider


def get_provider(
    provider_id: str,
    *,
    codex_home: Path | None = None,
    claude_config_dir: Path | None = None,
) -> Provider:
    normalized = provider_id.strip().lower()
    if normalized == "codex":
        return CodexProvider(codex_home=codex_home)
    if normalized == "claude":
        return ClaudeProvider(config_dir=claude_config_dir)
    raise AiAuthSwitchError(f"unsupported provider: {provider_id}")


__all__ = ["ClaudeProvider", "CodexProvider", "Provider", "get_provider"]
