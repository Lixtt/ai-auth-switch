from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Provider:
    """Provider contract for auth switching.

    A provider deliberately owns only auth-related behavior. It must not rewrite
    unrelated application config.
    """

    id: str
    active_auth_path: Path
    login_command: Sequence[str]
    login_args: Sequence[str] = ("login",)

    def infer_profile_name(self, auth_file: Path) -> str | None:
        return None

    def auth_identity(self, auth_file: Path) -> str | None:
        """Return a stable account identity for matching refreshed auth files."""
        return None

    def profile_metadata_path(self, profile_file: Path) -> Path | None:
        """Return an optional sidecar path for non-secret account metadata."""
        return None

    def read_profile_metadata(self, auth_file: Path) -> dict[str, Any] | None:
        """Read display/identity metadata associated with an auth file."""
        return None

    def apply_profile_metadata(
        self,
        metadata: dict[str, Any],
        *,
        config_dir: Path | None = None,
    ) -> None:
        """Apply profile metadata to the provider's active or isolated config."""
        return
