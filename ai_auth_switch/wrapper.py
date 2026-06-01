from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

from ai_auth_switch.providers import Provider
from ai_auth_switch.store import AuthStore


def run_with_profile(
    store: AuthStore,
    provider: Provider,
    profile: str,
    command: Sequence[str],
    *,
    on_activated: Callable[[], None] | None = None,
    on_restored: Callable[[], None] | None = None,
) -> int:
    if not command:
        command = provider.login_command
    activated = False
    with store.lock():
        try:
            with store.activated_temporarily(provider, profile):
                activated = True
                if on_activated is not None:
                    on_activated()
                return subprocess.call(list(command))
        finally:
            if activated and on_restored is not None:
                on_restored()
