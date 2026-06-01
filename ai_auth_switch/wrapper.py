from __future__ import annotations

import subprocess
from collections.abc import Sequence

from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore


def run_with_profile(
    store: AuthStore,
    provider: Provider,
    profile: str,
    command: Sequence[str],
) -> int:
    if not command:
        command = provider.login_command
    with store.lock():
        with store.activated_temporarily(provider, profile):
            return subprocess.call(list(command))
