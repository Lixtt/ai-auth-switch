from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ai_auth_switch import __version__
from ai_auth_switch.utils import atomic_write, extract_account_id_from_jwt


DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    window_seconds: int | None = None
    resets_at: int | None = None

    @property
    def remaining_percent(self) -> float:
        return max(0.0, min(100.0, 100.0 - self.used_percent))


@dataclass(frozen=True)
class AccountUsage:
    plan_type: str | None = None
    primary: UsageWindow | None = None
    secondary: UsageWindow | None = None
    credits_balance: str | None = None
    credits_unlimited: bool = False
    error: str | None = None


def _read_auth(path: Path) -> tuple[str | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    access_token = tokens.get("access_token") or data.get("access_token")
    account_id = tokens.get("account_id") or data.get("account_id")
    if not isinstance(access_token, str) or not access_token.strip():
        access_token = None
    if not isinstance(account_id, str) or not account_id.strip():
        account_id = _account_id_from_jwt(access_token) if access_token else None
    return access_token, account_id


def _account_id_from_jwt(token: str) -> str | None:
    return extract_account_id_from_jwt(token)


def _window(value: object) -> UsageWindow | None:
    if not isinstance(value, dict):
        return None
    used = value.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    seconds = value.get("limit_window_seconds")
    reset = value.get("reset_at")
    return UsageWindow(
        used_percent=float(used),
        window_seconds=int(seconds) if isinstance(seconds, (int, float)) else None,
        resets_at=int(reset) if isinstance(reset, (int, float)) else None,
    )


def parse_usage(data: object) -> AccountUsage:
    if not isinstance(data, dict):
        return AccountUsage(error="invalid response")
    limits = data.get("rate_limit")
    limits = limits if isinstance(limits, dict) else {}
    credits = data.get("credits")
    credits = credits if isinstance(credits, dict) else {}
    balance = credits.get("balance")
    return AccountUsage(
        plan_type=data.get("plan_type") if isinstance(data.get("plan_type"), str) else None,
        primary=_window(limits.get("primary_window")),
        secondary=_window(limits.get("secondary_window")),
        credits_balance=str(balance) if isinstance(balance, (str, int, float)) else None,
        credits_unlimited=credits.get("unlimited") is True,
    )


def fetch_usage(
    auth_path: Path,
    *,
    timeout: float = 5.0,
    url: str = DEFAULT_USAGE_URL,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> AccountUsage:
    token, account_id = _read_auth(auth_path)
    if not token:
        return AccountUsage(error="missing access token")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"ai-auth-switch/{__version__}",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(url, headers=headers)
    try:
        with opener(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return parse_usage(data)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return AccountUsage(error="authentication expired")
        return AccountUsage(error=f"HTTP {exc.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return AccountUsage(error=f"request failed: {reason}")
    except (ValueError, json.JSONDecodeError):
        return AccountUsage(error="invalid response")


def fetch_profile_usage(
    profiles: Iterable[tuple[str, Path]],
    *,
    timeout: float = 5.0,
    workers: int = 4,
    cache_dir: Path | None = None,
    cache_ttl: float = 60.0,
    refresh: bool = False,
    fetcher: Callable[..., AccountUsage] = fetch_usage,
) -> dict[str, AccountUsage]:
    items = list(profiles)
    if not items:
        return {}
    results: dict[str, AccountUsage] = {}
    pending_items = []
    for name, path in items:
        cached = None if refresh else _read_cache(cache_dir, name, path, cache_ttl)
        if cached is not None:
            results[name] = cached
        else:
            pending_items.append((name, path))
    if not pending_items:
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pending_items)))) as pool:
        pending = {
            pool.submit(fetcher, path, timeout=timeout): (name, path)
            for name, path in pending_items
        }
        for future in as_completed(pending):
            name, path = pending[future]
            try:
                results[name] = future.result()
            except Exception as exc:
                results[name] = AccountUsage(error=f"request failed: {exc}")
            _write_cache(cache_dir, name, path, results[name])
    return results


def _auth_fingerprint(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cache_path(cache_dir: Path | None, name: str) -> Path | None:
    if cache_dir is None:
        return None
    key = hashlib.sha256(name.encode("utf-8")).hexdigest()
    return cache_dir / f"{key}.json"


def _read_cache(
    cache_dir: Path | None, name: str, auth_path: Path, ttl: float
) -> AccountUsage | None:
    path = _cache_path(cache_dir, name)
    if path is None or ttl <= 0:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data["fetched_at"]) > ttl:
            return None
        if data.get("auth_fingerprint") != _auth_fingerprint(auth_path):
            return None
        return _usage_from_dict(data["usage"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_cache(
    cache_dir: Path | None, name: str, auth_path: Path, usage: AccountUsage
) -> None:
    path = _cache_path(cache_dir, name)
    fingerprint = _auth_fingerprint(auth_path)
    if path is None or fingerprint is None:
        return
    try:
        payload = {
            "fetched_at": time.time(),
            "auth_fingerprint": fingerprint,
            "usage": usage_to_dict(usage),
        }
        atomic_write(
            path,
            json.dumps(payload, separators=(",", ":")),
        )
    except OSError:
        return


def _window_to_dict(window: UsageWindow | None) -> dict[str, Any] | None:
    if window is None:
        return None
    return {
        "used_percent": window.used_percent,
        "window_seconds": window.window_seconds,
        "resets_at": window.resets_at,
    }


def usage_to_dict(usage: AccountUsage) -> dict[str, Any]:
    return {
        "plan_type": usage.plan_type,
        "primary": _window_to_dict(usage.primary),
        "secondary": _window_to_dict(usage.secondary),
        "credits_balance": usage.credits_balance,
        "credits_unlimited": usage.credits_unlimited,
        "error": usage.error,
    }


def _usage_from_dict(data: object) -> AccountUsage:
    if not isinstance(data, dict):
        raise ValueError("invalid cached usage")

    def cached_window(value: object) -> UsageWindow | None:
        if not isinstance(value, dict):
            return None
        return UsageWindow(
            used_percent=float(value["used_percent"]),
            window_seconds=value.get("window_seconds"),
            resets_at=value.get("resets_at"),
        )

    return AccountUsage(
        plan_type=data.get("plan_type"),
        primary=cached_window(data.get("primary")),
        secondary=cached_window(data.get("secondary")),
        credits_balance=data.get("credits_balance"),
        credits_unlimited=data.get("credits_unlimited") is True,
        error=data.get("error"),
    )


def format_window(window: UsageWindow) -> str:
    if window.window_seconds and window.window_seconds % 3600 == 0:
        label = f"{window.window_seconds // 3600}h"
    elif window.window_seconds and window.window_seconds % 60 == 0:
        label = f"{window.window_seconds // 60}m"
    else:
        label = "window"
    return f"{label} {window.remaining_percent:g}% left"


def format_usage(usage: AccountUsage) -> str:
    if usage.error:
        return f"usage unavailable: {usage.error}"
    parts = []
    if usage.plan_type:
        parts.append(usage.plan_type)
    for window in (usage.primary, usage.secondary):
        if window:
            parts.append(format_window(window))
    if usage.credits_unlimited:
        parts.append("credits unlimited")
    elif usage.credits_balance is not None:
        parts.append(f"credits {usage.credits_balance}")
    return ", ".join(parts) if parts else "usage unavailable: no limits returned"
