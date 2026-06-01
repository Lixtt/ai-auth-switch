from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class Provider:
    """Provider contract for auth switching.

    A provider deliberately owns only auth-related behavior. It must not rewrite
    unrelated application config.
    """

    id: str
    active_auth_path: Path
    login_command: Sequence[str]

    def infer_profile_name(self, auth_file: Path) -> str | None:
        return None
