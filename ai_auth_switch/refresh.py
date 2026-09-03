from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai_auth_switch import __version__
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers.base import Provider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.utils import (
    atomic_write,
    decode_jwt_payload,
    set_private_permissions,
)

DEFAULT_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_SCOPE = "openid profile email"

# Refresh a token slightly before it actually expires so a long-running request
# is not started with credentials that die mid-flight.
DEFAULT_EXPIRY_SKEW = 300.0

REFRESHED = "refreshed"
SKIPPED = "skipped"
LOGIN_REQUIRED = "login_required"
REJECTED = "rejected"
FAILED = "failed"

# Token-endpoint error codes that no retry can fix: the stored refresh token is
# gone for good and only an interactive login can produce a new one. Retrying
# these would hammer the endpoint on every pool request.
PERMANENT_ERROR_CODES = frozenset(
    {
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_invalidated",
        "refresh_token_reused",
    }
)


@dataclass(frozen=True)
class RefreshResult:
    profile: str
    status: str
    message: str
    expires_at: int | None = None
    rotated: bool = False

    @property
    def changed(self) -> bool:
        return self.status == REFRESHED

    @property
    def needs_login(self) -> bool:
        return self.status == LOGIN_REQUIRED


class RefreshError(AiAuthSwitchError):
    """A token exchange that failed, with its permanence classified."""

    def __init__(self, message: str, *, code: str | None = None, permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent


def token_url() -> str:
    configured = os.environ.get("AI_AUTH_SWITCH_CODEX_TOKEN_URL")
    if configured and configured.strip():
        return configured.strip()
    return DEFAULT_TOKEN_URL


def supports_refresh(provider: Provider) -> bool:
    """Report whether this provider's saved profiles can be refreshed offline.

    Claude Code stores its credentials differently and has no published token
    endpoint here, so only Codex profiles are refreshable for now.
    """
    return provider.id == "codex"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tokens(data: dict[str, Any]) -> dict[str, Any]:
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def access_token_expires_at(path: Path) -> int | None:
    """Return the stored access token's expiry, or None when it is unreadable."""
    token = _tokens(_read_json(path)).get("access_token")
    if not isinstance(token, str) or not token.strip():
        return None
    exp = decode_jwt_payload(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def needs_refresh(
    path: Path,
    *,
    skew: float = DEFAULT_EXPIRY_SKEW,
    now: float | None = None,
) -> bool:
    """Report whether *path* holds an access token at or past its expiry.

    An unreadable or non-JWT access token counts as needing a refresh: the
    caller cannot prove it is still usable, and a wasted exchange is cheaper
    than routing a request onto a dead credential.
    """
    timestamp = time.time() if now is None else now
    expires_at = access_token_expires_at(path)
    if expires_at is None:
        return True
    return expires_at - timestamp <= skew


def has_refresh_token(path: Path) -> bool:
    value = _tokens(_read_json(path)).get("refresh_token")
    return isinstance(value, str) and bool(value.strip())


def _error_from_body(status: int, body: bytes) -> RefreshError:
    code: str | None = None
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            raw_code = error.get("code")
            code = raw_code if isinstance(raw_code, str) else None
            raw_message = error.get("message")
            message = raw_message if isinstance(raw_message, str) else ""
        elif isinstance(error, str):
            code = error
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    if not message:
        message = body.decode("utf-8", "replace").strip()[:200] or f"HTTP {status}"
    permanent = code in PERMANENT_ERROR_CODES
    label = f"{message} ({code})" if code else message
    return RefreshError(label, code=code, permanent=permanent)


def exchange_refresh_token(
    refresh_token: str,
    *,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    url: str | None = None,
    client_id: str = CODEX_CLIENT_ID,
) -> dict[str, Any]:
    """Exchange a Codex refresh token for a new access token.

    Mirrors the Codex CLI's own exchange: a form-encoded POST to the OAuth
    token endpoint with the published Codex client id.
    """
    body = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": CODEX_SCOPE,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url or token_url(),
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": f"ai-auth-switch/{__version__}",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read()
        except OSError:
            body = b""
        raise _error_from_body(exc.code, body) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise RefreshError(f"request failed: {reason}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise RefreshError("token endpoint returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RefreshError("token endpoint returned invalid JSON")
    return payload


def _apply_tokens(data: dict[str, Any], payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Merge a token response into auth-file data, keeping unrelated keys."""
    updated = dict(data)
    tokens = dict(_tokens(data))
    previous_refresh = tokens.get("refresh_token")
    tokens["access_token"] = payload["access_token"]
    for key in ("refresh_token", "id_token"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            tokens[key] = value.strip()
    updated["tokens"] = tokens
    updated["last_refresh"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "000Z"
    )
    return updated, tokens.get("refresh_token") != previous_refresh


def refresh_profile(
    store: AuthStore,
    provider: Provider,
    name: str,
    *,
    force: bool = False,
    timeout: float = 30.0,
    skew: float = DEFAULT_EXPIRY_SKEW,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: float | None = None,
) -> RefreshResult:
    """Exchange one saved profile's refresh token and write the result back.

    The profile update lock is held across the exchange on purpose. Refresh
    tokens rotate, so two processes exchanging the same token concurrently
    would leave one of them holding a token the server has already retired.
    """
    if not supports_refresh(provider):
        raise AiAuthSwitchError("token refresh currently supports codex only")
    timestamp = time.time() if now is None else now
    profile_path = store.profile_path(provider, name)
    if not profile_path.exists():
        return RefreshResult(name, SKIPPED, "profile not found")

    with store.profile_lock(provider, name):
        data = _read_json(profile_path)
        tokens = _tokens(data)
        refresh_token = tokens.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return RefreshResult(name, SKIPPED, "profile has no refresh token")

        expires_at = access_token_expires_at(profile_path)
        if not force and not needs_refresh(profile_path, skew=skew, now=timestamp):
            return RefreshResult(
                name,
                SKIPPED,
                f"access token still valid until {_format_time(expires_at)}",
                expires_at,
            )

        expected_identity = provider.auth_identity(profile_path)
        try:
            payload = exchange_refresh_token(
                refresh_token.strip(), timeout=timeout, opener=opener
            )
        except RefreshError as exc:
            status = LOGIN_REQUIRED if exc.permanent else FAILED
            return RefreshResult(name, status, str(exc), expires_at)

        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token.strip():
            return RefreshResult(
                name, FAILED, "token response has no access_token", expires_at
            )

        updated, rotated = _apply_tokens(data, {**payload, "access_token": access_token})
        candidate = profile_path.with_name(
            f".{profile_path.name}.refresh.{os.getpid()}.{time.time_ns()}"
        )
        try:
            atomic_write(candidate, json.dumps(updated, indent=2) + "\n")
            set_private_permissions(candidate)
            # Reuse the store's identity guard so a token response for another
            # account is preserved for inspection instead of overwriting this
            # profile, exactly as wrapper reconciliation does.
            store.sync_profile_auth(
                provider,
                name,
                candidate,
                expected_identity=expected_identity,
            )
        except AiAuthSwitchError as exc:
            return RefreshResult(name, REJECTED, str(exc), expires_at)
        except OSError as exc:
            return RefreshResult(name, FAILED, f"write failed: {exc}", expires_at)
        finally:
            candidate.unlink(missing_ok=True)

    new_expiry = access_token_expires_at(profile_path)
    return RefreshResult(
        name,
        REFRESHED,
        f"new access token valid until {_format_time(new_expiry)}",
        new_expiry,
        rotated,
    )


def refresh_profiles(
    store: AuthStore,
    provider: Provider,
    names: Iterable[str],
    *,
    force: bool = False,
    timeout: float = 30.0,
    skew: float = DEFAULT_EXPIRY_SKEW,
    workers: int = 4,
    opener: Callable[..., Any] = urllib.request.urlopen,
    refresher: Callable[..., RefreshResult] = refresh_profile,
) -> list[RefreshResult]:
    """Refresh several profiles, keeping the caller's name order in the result."""
    ordered = list(names)
    if not ordered:
        return []

    def run(name: str) -> RefreshResult:
        try:
            return refresher(
                store,
                provider,
                name,
                force=force,
                timeout=timeout,
                skew=skew,
                opener=opener,
            )
        except AiAuthSwitchError as exc:
            return RefreshResult(name, FAILED, str(exc))

    if workers <= 1 or len(ordered) == 1:
        return [run(name) for name in ordered]
    with ThreadPoolExecutor(max_workers=min(workers, len(ordered))) as pool:
        return list(pool.map(run, ordered))


def _format_time(value: int | None) -> str:
    if value is None:
        return "unknown"
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds")


def result_to_dict(result: RefreshResult) -> dict[str, Any]:
    return {
        "profile": result.profile,
        "status": result.status,
        "message": result.message,
        "expires_at": result.expires_at,
        "refresh_token_rotated": result.rotated,
    }
