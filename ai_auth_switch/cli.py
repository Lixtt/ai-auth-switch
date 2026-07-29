from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ai_auth_switch import __version__
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider, get_provider
from ai_auth_switch.store import (
    AliasInfo,
    AuthStore,
    numbered_codex_alias_index,
)
from ai_auth_switch.sync import SyncResult, sync_codex_dependents
from ai_auth_switch.usage import fetch_profile_usage, format_usage, usage_to_dict
from ai_auth_switch.wrapper import run_with_profile


SUPPORTED_PROVIDERS = ["codex"]
PROGRAM_NAMES = {
    "ai-auth-switch",
    "ais",
    "ai-auth-switch.py",
    "cli.py",
    "__main__.py",
    "python",
    "python3",
}
AUTO_ALIAS_BIN_DIR_ENV = "AI_AUTH_SWITCH_ALIAS_BIN_DIR"
AUTO_ALIAS_TARGET_ENV = "AI_AUTH_SWITCH_ALIAS_TARGET"


class CliUsageError(Exception):
    pass


class HelpfulArgumentParser(argparse.ArgumentParser):
    """Argument parser that shows actionable, command-local help on errors."""

    def _check_value(self, action, value):
        if action.choices is not None and value not in action.choices:
            choices = [str(choice) for choice in action.choices or ()]
            matches = difflib.get_close_matches(str(value), choices, n=1, cutoff=0.55)
            message = f"invalid choice: {value!r} (choose from {', '.join(choices)})"
            if matches:
                message += f"\nDid you mean {matches[0]!r}?"
            self.error(message)
        return super()._check_value(action, value)

    def error(self, message: str) -> None:
        self.print_help(sys.stderr)
        self._print_message(f"\nerror: {message}\n", sys.stderr)
        raise CliUsageError(message)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


@dataclass(frozen=True)
class AliasLinkSyncResult:
    installed: tuple[Path, ...] = ()
    updated: tuple[Path, ...] = ()
    removed: tuple[Path, ...] = ()
    conflicts: tuple[Path, ...] = ()


def _strip_separator(args: Sequence[str]) -> list[str]:
    args = list(args)
    if args and args[0] == "--":
        return args[1:]
    return args


def _provider_from_args(args: argparse.Namespace) -> Provider:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else None
    return get_provider(args.provider, codex_home=codex_home)


def _store_from_args(args: argparse.Namespace) -> AuthStore:
    return AuthStore(Path(args.store_dir).expanduser() if args.store_dir else None)


def _provider_ids(args: argparse.Namespace) -> list[str]:
    provider = getattr(args, "provider", None)
    if provider:
        return [provider]
    return SUPPORTED_PROVIDERS


def _provider_by_id(provider_id: str, args: argparse.Namespace) -> Provider:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else None
    return get_provider(provider_id, codex_home=codex_home)


def _alias_executable_target(target: str | Path | None = None) -> Path:
    configured = os.environ.get(AUTO_ALIAS_TARGET_ENV)
    checkout_target = Path(__file__).resolve().parents[1] / "bin" / "ai-auth-switch"
    if target is not None:
        candidate = Path(target).expanduser()
    elif configured and configured.strip():
        candidate = Path(configured).expanduser()
    elif checkout_target.is_file() and os.access(checkout_target, os.X_OK):
        candidate = checkout_target
    else:
        candidate = Path(shutil.which("ai-auth-switch") or sys.argv[0]).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AiAuthSwitchError(f"ai-auth-switch executable not found: {candidate}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AiAuthSwitchError(f"ai-auth-switch executable not found: {candidate}")
    return resolved


def _symlink_points_to(link: Path, target: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        raw_target = Path(os.readlink(link))
    except OSError:
        return False
    if not raw_target.is_absolute():
        raw_target = link.parent / raw_target
    return raw_target.resolve() == target.resolve()


def _is_managed_alias_link(link: Path) -> bool:
    if not link.is_symlink():
        return False
    try:
        raw_target = Path(os.readlink(link))
    except OSError:
        return False
    return raw_target.name.startswith("ai-auth-switch")


def _replace_symlink(link: Path, target: Path) -> None:
    tmp = link.with_name(f".{link.name}.tmp.{os.getpid()}.{time.time_ns()}")
    try:
        os.symlink(target, tmp)
        os.replace(tmp, link)
    finally:
        if tmp.is_symlink():
            tmp.unlink()


def _sync_numbered_alias_executables(
    aliases: Sequence[AliasInfo],
    *,
    bin_dir: Path,
    target: Path,
) -> AliasLinkSyncResult:
    automatic = {
        alias.name: alias
        for alias in aliases
        if alias.provider_id == "codex"
        and numbered_codex_alias_index(alias.name) is not None
    }
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AiAuthSwitchError(f"failed to create alias directory {bin_dir}: {exc}") from exc

    installed: list[Path] = []
    updated: list[Path] = []
    removed: list[Path] = []
    conflicts: list[Path] = []
    for name in sorted(automatic, key=lambda item: numbered_codex_alias_index(item) or 0):
        link = bin_dir / name
        if not link.exists() and not link.is_symlink():
            try:
                os.symlink(target, link)
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to install automatic alias {link}: {exc}"
                ) from exc
            installed.append(link)
        elif not _symlink_points_to(link, target):
            if _is_managed_alias_link(link):
                try:
                    _replace_symlink(link, target)
                except OSError as exc:
                    raise AiAuthSwitchError(
                        f"failed to update automatic alias {link}: {exc}"
                    ) from exc
                updated.append(link)
            else:
                conflicts.append(link)

    try:
        candidates = list(bin_dir.iterdir())
    except OSError as exc:
        raise AiAuthSwitchError(f"failed to inspect alias directory {bin_dir}: {exc}") from exc
    for link in candidates:
        if (
            numbered_codex_alias_index(link.name) is not None
            and link.name not in automatic
            and (
                _symlink_points_to(link, target)
                or _is_managed_alias_link(link)
            )
        ):
            try:
                link.unlink()
            except OSError as exc:
                raise AiAuthSwitchError(
                    f"failed to remove stale automatic alias {link}: {exc}"
                ) from exc
            removed.append(link)

    return AliasLinkSyncResult(
        installed=tuple(installed),
        updated=tuple(updated),
        removed=tuple(removed),
        conflicts=tuple(conflicts),
    )


def _automatic_alias_bin_dir(args: argparse.Namespace) -> Path | None:
    configured = os.environ.get(AUTO_ALIAS_BIN_DIR_ENV)
    if configured and configured.strip():
        return Path(configured).expanduser()
    if getattr(args, "store_dir", None):
        return None
    return Path.home() / ".local/bin"


def _print_alias_link_sync(result: AliasLinkSyncResult) -> None:
    for link in result.installed:
        print(f"installed automatic alias {link.name} -> {os.readlink(link)}")
    for link in result.updated:
        print(f"updated automatic alias {link.name} -> {os.readlink(link)}")
    for link in result.removed:
        print(f"removed stale automatic alias {link.name}")
    for link in result.conflicts:
        print(
            f"ai-auth-switch: automatic alias not installed; path already exists: {link}",
            file=sys.stderr,
        )


BUILTIN_SHORTCUTS = ("ais",)


def _ensure_builtin_shortcut(
    name: str,
    *,
    bin_dir: Path,
    target: Path,
) -> None:
    """Ensure a built-in shortcut symlink (e.g. ``ais``) exists in *bin_dir*."""
    link = bin_dir / name
    if link.is_symlink() and _symlink_points_to(link, target):
        return
    if link.exists():
        if _is_managed_alias_link(link):
            try:
                _replace_symlink(link, target)
            except OSError as exc:
                print(
                    f"ai-auth-switch: failed to update built-in shortcut {link}: {exc}",
                    file=sys.stderr,
                )
        else:
            print(
                f"ai-auth-switch: built-in shortcut not installed; path already exists: {link}",
                file=sys.stderr,
            )
        return
    try:
        os.symlink(target, link)
        print(f"installed built-in shortcut {link.name} -> {os.readlink(link)}")
    except OSError as exc:
        print(
            f"ai-auth-switch: failed to install built-in shortcut {link}: {exc}",
            file=sys.stderr,
        )


def _sync_automatic_alias_links(
    args: argparse.Namespace,
    aliases: Sequence[AliasInfo],
) -> None:
    bin_dir = _automatic_alias_bin_dir(args)
    if bin_dir is None:
        return
    target = _alias_executable_target()
    try:
        result = _sync_numbered_alias_executables(
            aliases,
            bin_dir=bin_dir,
            target=target,
        )
    except AiAuthSwitchError as exc:
        print(f"ai-auth-switch: automatic alias sync failed: {exc}", file=sys.stderr)
        return
    _print_alias_link_sync(result)
    for name in BUILTIN_SHORTCUTS:
        _ensure_builtin_shortcut(name, bin_dir=bin_dir, target=target)


def _auth_hint(provider: Provider) -> str:
    active = provider.active_auth_path
    if not active.exists() and not active.is_symlink():
        return (
            f"auth file not found at {active}; set CODEX_HOME or pass "
            f"--codex-home if {provider.id} uses another config directory"
        )

    inferred = provider.infer_profile_name(active)
    suffix = f" ({inferred})" if inferred else ""
    return (
        f"unmanaged {provider.id} auth found at {active}{suffix}; run "
        f"`ai-auth-switch auth save {provider.id}` to import it"
    )


def _print_sync_results(results: Sequence[SyncResult]) -> None:
    for result in results:
        path = f" ({result.path})" if result.path is not None else ""
        stream = sys.stderr if result.status == "error" else sys.stdout
        print(
            f"{result.target}: {result.status}: {result.message}{path}",
            file=stream,
        )


def _sync_after_auth_change(
    args: argparse.Namespace,
    provider: Provider,
    store: AuthStore,
    *,
    profile_name: str | None = None,
    hermes_login: bool = False,
) -> list[SyncResult]:
    if provider.id != "codex" or getattr(args, "no_dependent_sync", False):
        return []
    try:
        results = sync_codex_dependents(
            provider,
            hermes_login=hermes_login,
            hermes_profile_name=profile_name,
            store_dir=store.base_dir,
        )
    except AiAuthSwitchError as exc:
        results = [SyncResult(target="dependent-sync", status="error", message=str(exc))]
    _print_sync_results(results)
    return results


def _cmd_auth_list(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    provider_ids = _provider_ids(args)
    aliases = store.list_aliases()
    json_providers = {}
    for index, provider_id in enumerate(provider_ids):
        provider = _provider_by_id(provider_id, args)
        profiles = store.list_profiles(provider)
        usages = (
            fetch_profile_usage(
                ((profile.name, profile.path) for profile in profiles),
                timeout=args.usage_timeout,
                workers=args.usage_workers,
                cache_dir=store.base_dir / "cache" / "usage" / provider.id,
                cache_ttl=args.usage_cache_ttl,
                refresh=args.refresh_usage,
            )
            if args.usage and provider.id == "codex"
            else {}
        )
        aliases_by_profile = {
            alias.profile: alias.name
            for alias in aliases
            if alias.provider_id == provider.id
            and numbered_codex_alias_index(alias.name) is not None
        }
        if len(provider_ids) > 1:
            if index and not args.json:
                print()
            if not args.json:
                print(f"{provider.id}:")
        if not profiles:
            if args.json:
                json_providers[provider.id] = []
                continue
            prefix = "  " if len(provider_ids) > 1 else ""
            print(f"{prefix}no profiles")
            print(f"{prefix}{_auth_hint(provider)}")
            continue
        for profile in profiles:
            mark = "*" if profile.active else " "
            suffix = " (content match)" if profile.by_content else ""
            alias_name = aliases_by_profile.get(profile.name)
            if alias_name:
                suffix += f" [{alias_name}]"
            usage = usages.get(profile.name)
            if usage is not None:
                suffix += f" ({format_usage(usage)})"
            actual_name = provider.infer_profile_name(profile.path)
            if (
                actual_name
                and actual_name.casefold() != profile.name.casefold()
            ):
                suffix += f" (actual auth: {actual_name})"
            if args.json:
                entry = {
                    "name": profile.name,
                    "active": profile.active,
                    "content_match": profile.by_content,
                    "alias": alias_name,
                    "actual_auth": actual_name,
                }
                if usage is not None:
                    entry["usage"] = usage_to_dict(usage)
                json_providers.setdefault(provider.id, []).append(entry)
                continue
            prefix = "  " if len(provider_ids) > 1 else ""
            print(f"{prefix}{mark} {profile.name}{suffix}")
    if args.json:
        print(json.dumps({"providers": json_providers}, indent=2, sort_keys=True))
    return 0


def _cmd_auth_current(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    provider_ids = _provider_ids(args)
    missing = False
    for provider_id in provider_ids:
        provider = _provider_by_id(provider_id, args)
        current = store.current_profile(provider)
        prefix = f"{provider.id}: " if len(provider_ids) > 1 else ""
        if current is None:
            print(f"{prefix}not active")
            print(f"{prefix}{_auth_hint(provider)}")
            missing = True
            continue
        suffix = " (content match)" if current.by_content else ""
        print(f"{prefix}{current.name}{suffix}")
    return 1 if missing else 0


def _cmd_auth_save(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.save_current(provider, args.name)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"saved {provider.id} auth as {profile.name}")
    print(f"active {provider.id} auth -> {profile.path}")
    _sync_after_auth_change(args, provider, store, profile_name=profile.name)
    return 0


def _cmd_auth_use(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.activate(provider, args.name)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"active {provider.id} auth -> {profile.name}")
    _sync_after_auth_change(args, provider, store, profile_name=profile.name)
    return 0


def _cmd_auth_sync(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    current = store.current_profile(provider)
    results = sync_codex_dependents(
        provider,
        sync_hermes=not args.no_hermes,
        sync_openclaw=not args.no_openclaw,
        restart_hermes=not args.no_hermes_restart,
        restart_openclaw=not args.no_openclaw_restart,
        hermes_login=args.hermes_login,
        hermes_profile_name=current.name if current else None,
        store_dir=store.base_dir,
    )
    _print_sync_results(results)
    return 1 if any(not result.ok for result in results) else 0


def _cmd_auth_remove(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        store.remove(provider, args.name)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"removed {provider.id} profile {args.name}")
    return 0


def _cmd_auth_rename(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.rename(provider, args.old, args.new)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"renamed {provider.id} profile {args.old} -> {profile.name}")
    return 0


def _run_login(provider: Provider, login_args: Sequence[str]) -> int:
    command = list(provider.login_command) + ["login"] + _strip_separator(login_args)
    return subprocess.call(command)


def _cmd_auth_login(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    name, login_args = _split_login_name_and_args(args.login_args)
    active = provider.active_auth_path
    backup = active.with_name(f".{active.name}.login-backup.{os.getpid()}.{time.time_ns()}")
    had_active = False

    with store.lock():
        active.parent.mkdir(parents=True, exist_ok=True)
        if active.exists() or active.is_symlink():
            os.replace(active, backup)
            had_active = True

        status = _run_login(provider, login_args)
        if status != 0:
            if active.exists() or active.is_symlink():
                active.unlink()
            if had_active:
                os.replace(backup, active)
            return status

        try:
            profile = store.save_current(provider, name)
        except Exception:
            if active.exists() or active.is_symlink():
                active.unlink()
            if had_active:
                os.replace(backup, active)
            raise

        if had_active and backup.exists():
            backup.unlink()

        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"saved {provider.id} login as {profile.name}")
    print(f"active {provider.id} auth -> {profile.path}")
    _sync_after_auth_change(
        args,
        provider,
        store,
        profile_name=profile.name,
    )
    return 0


def _split_login_name_and_args(raw: Sequence[str]) -> tuple[str | None, list[str]]:
    args = list(raw)
    if args and args[0] == "--":
        return None, args[1:]
    if args and not args[0].startswith("-"):
        name = args.pop(0)
        if args and args[0] == "--":
            args.pop(0)
        return name, args
    return None, args


def _cmd_run(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    command = _strip_separator(args.command)
    if not command:
        command = list(provider.login_command)

    return run_with_profile(
        store,
        provider,
        args.name,
        command,
    )


def _alias_display(alias: AliasInfo) -> str:
    command = " ".join(alias.command)
    return f"{alias.name} -> {alias.provider_id}:{alias.profile} -- {command}"


def _run_alias(
    store: AuthStore,
    alias: AliasInfo,
    extra_args: Sequence[str],
    *,
    provider: Provider | None = None,
) -> int:
    provider = provider or get_provider(alias.provider_id)
    command = list(alias.command) + _strip_separator(extra_args)
    return run_with_profile(store, provider, alias.profile, command)


def _cmd_alias_list(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    aliases = store.list_aliases()
    if not aliases:
        print("no aliases")
        return 0
    for alias in aliases:
        print(_alias_display(alias))
    return 0


def _cmd_alias_set(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    command = _strip_separator(args.command)
    with store.lock():
        alias = store.set_alias(provider, args.name, args.profile, command or None)
        automatic_aliases = store.sync_numbered_aliases(provider)
        alias = store.resolve_alias(alias.name) or alias
        _sync_automatic_alias_links(args, automatic_aliases)
    print(f"saved alias {_alias_display(alias)}")
    return 0


def _cmd_alias_remove(args: argparse.Namespace) -> int:
    if numbered_codex_alias_index(args.name) is not None:
        raise AiAuthSwitchError(
            f"{args.name} is managed automatically; remove its Codex profile instead"
        )
    store = _store_from_args(args)
    with store.lock():
        store.remove_alias(args.name)
    print(f"removed alias {args.name}")
    return 0


def _cmd_alias_sync(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        aliases = store.sync_numbered_aliases(provider)
        bin_dir = (
            Path(args.bin_dir).expanduser()
            if args.bin_dir
            else Path.home() / ".local/bin"
        )
        result = _sync_numbered_alias_executables(
            aliases,
            bin_dir=bin_dir,
            target=_alias_executable_target(args.target),
        )
    _print_alias_link_sync(result)
    for alias in aliases:
        print(f"synced alias {_alias_display(alias)}")
    return 1 if result.conflicts else 0


def _cmd_alias_run(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    alias = store.resolve_alias(args.name)
    if alias is None:
        raise AiAuthSwitchError(f"alias not found: {args.name}")
    provider = _provider_by_id(alias.provider_id, args)
    return _run_alias(store, alias, args.command, provider=provider)


def _cmd_alias_install(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    alias = store.resolve_alias(args.name)
    if alias is None:
        raise AiAuthSwitchError(f"alias not found: {args.name}")

    bin_dir = Path(args.bin_dir).expanduser() if args.bin_dir else Path.home() / ".local/bin"
    target = _alias_executable_target(args.target)

    bin_dir.mkdir(parents=True, exist_ok=True)
    link = bin_dir / alias.name
    if link.exists() or link.is_symlink():
        if not args.force:
            raise AiAuthSwitchError(f"alias executable already exists: {link}")
        link.unlink()
    os.symlink(target, link)
    print(f"installed {alias.name} -> {target}")
    return 0


def _program_alias_name(program_name: str) -> str | None:
    name = Path(program_name).name
    if name in PROGRAM_NAMES or name.startswith("ai-auth-switch"):
        return None
    return name


def _maybe_run_program_alias(
    argv: Sequence[str],
    *,
    program_name: str,
) -> int | None:
    alias_name = _program_alias_name(program_name)
    if alias_name is None:
        return None

    store = AuthStore()
    alias = store.resolve_alias(alias_name)
    if alias is None:
        raise AiAuthSwitchError(
            f"alias not found for executable {alias_name!r}; "
            f"run `ai-auth-switch alias set {alias_name} codex <profile>` first"
        )
    return _run_alias(store, alias, argv)


def build_parser(*, prog: str = "ai-auth-switch") -> argparse.ArgumentParser:
    parser = HelpfulArgumentParser(
        prog=prog,
        description="Switch auth profiles for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--store-dir",
        help="Profile store directory. Defaults to $AI_AUTH_SWITCH_HOME or ~/.local/share/ai-auth-switch.",
    )
    parser.add_argument(
        "--codex-home",
        help="Override Codex config directory for the codex provider.",
    )
    parser.add_argument(
        "--no-dependent-sync",
        action="store_true",
        help="Do not sync Hermes/OpenClaw after changing active Codex auth.",
    )

    subparsers = parser.add_subparsers(dest="command_name", required=True)

    auth = subparsers.add_parser("auth", help="Manage saved auth profiles.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_list = auth_sub.add_parser("list", help="List profiles.")
    auth_list.add_argument("provider", nargs="?", choices=SUPPORTED_PROVIDERS)
    auth_list.add_argument(
        "--usage",
        action="store_true",
        help="Fetch current Codex limits for every saved account.",
    )
    auth_list.add_argument(
        "--usage-timeout",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Per-account usage request timeout (default: 5).",
    )
    auth_list.add_argument(
        "--usage-workers",
        type=_positive_int,
        default=4,
        metavar="COUNT",
        help="Maximum concurrent usage requests (default: 4).",
    )
    auth_list.add_argument(
        "--usage-cache-ttl",
        type=_positive_float,
        default=60.0,
        metavar="SECONDS",
        help="Reuse usage results for this long (default: 60).",
    )
    auth_list.add_argument(
        "--refresh-usage",
        action="store_true",
        help="Ignore cached usage results.",
    )
    auth_list.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON.",
    )
    auth_list.set_defaults(func=_cmd_auth_list)

    auth_current = auth_sub.add_parser("current", help="Show active profile.")
    auth_current.add_argument("provider", nargs="?", choices=SUPPORTED_PROVIDERS)
    auth_current.set_defaults(func=_cmd_auth_current)

    auth_save = auth_sub.add_parser("save", help="Save the active auth file as a profile.")
    auth_save.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_save.add_argument("name", nargs="?")
    auth_save.set_defaults(func=_cmd_auth_save)

    auth_use = auth_sub.add_parser("use", help="Activate a saved profile.")
    auth_use.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_use.add_argument("name")
    auth_use.set_defaults(func=_cmd_auth_use)

    auth_sync = auth_sub.add_parser(
        "sync",
        help="Sync dependent tool auth from the active provider auth.",
    )
    auth_sync.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_sync.add_argument(
        "--no-hermes",
        action="store_true",
        help="Skip Hermes Codex CLI bridge sync.",
    )
    auth_sync.add_argument(
        "--hermes-login",
        action="store_true",
        help="Deprecated compatibility option; Hermes now uses Codex CLI auth via bridge sync.",
    )
    auth_sync.add_argument(
        "--no-hermes-restart",
        action="store_true",
        help="Do not restart hermes-gateway.service after Hermes sync.",
    )
    auth_sync.add_argument(
        "--no-openclaw",
        action="store_true",
        help="Skip OpenClaw auth-state sync.",
    )
    auth_sync.add_argument(
        "--no-openclaw-restart",
        action="store_true",
        help="Do not restart openclaw-gateway.service after sync.",
    )
    auth_sync.set_defaults(func=_cmd_auth_sync)

    auth_login = auth_sub.add_parser("login", help="Run provider login and save the result.")
    auth_login.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_login.add_argument("login_args", nargs=argparse.REMAINDER)
    auth_login.set_defaults(func=_cmd_auth_login)

    auth_rename = auth_sub.add_parser("rename", help="Rename a saved profile.")
    auth_rename.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_rename.add_argument("old")
    auth_rename.add_argument("new")
    auth_rename.set_defaults(func=_cmd_auth_rename)

    auth_remove = auth_sub.add_parser("remove", help="Remove a saved inactive profile.")
    auth_remove.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    auth_remove.add_argument("name")
    auth_remove.set_defaults(func=_cmd_auth_remove)

    run = subparsers.add_parser(
        "run",
        help="Run a command with isolated auth while sharing normal Codex state.",
    )
    run.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    run.add_argument("name")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=_cmd_run)

    alias = subparsers.add_parser("alias", help="Manage command aliases.")
    alias_sub = alias.add_subparsers(dest="alias_command", required=True)

    alias_list = alias_sub.add_parser("list", help="List command aliases.")
    alias_list.set_defaults(func=_cmd_alias_list)

    alias_sync = alias_sub.add_parser(
        "sync",
        help="Create and install contiguous codex1, codex2, ... aliases.",
    )
    alias_sync.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    alias_sync.add_argument("--bin-dir", help="Directory for automatic alias symlinks.")
    alias_sync.add_argument("--target", help="ai-auth-switch executable target.")
    alias_sync.set_defaults(func=_cmd_alias_sync)

    alias_set = alias_sub.add_parser("set", help="Create or update a command alias.")
    alias_set.add_argument("name", help="Alias executable name, for example codex1.")
    alias_set.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    alias_set.add_argument("profile", help="Saved provider profile to activate.")
    alias_set.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after '--'. Defaults to the provider command.",
    )
    alias_set.set_defaults(func=_cmd_alias_set)

    alias_remove = alias_sub.add_parser("remove", help="Remove a command alias.")
    alias_remove.add_argument("name")
    alias_remove.set_defaults(func=_cmd_alias_remove)

    alias_run = alias_sub.add_parser("run", help="Run a command alias by name.")
    alias_run.add_argument("name")
    alias_run.add_argument("command", nargs=argparse.REMAINDER)
    alias_run.set_defaults(func=_cmd_alias_run)

    alias_install = alias_sub.add_parser(
        "install",
        help="Install a symlink so the alias can be invoked directly.",
    )
    alias_install.add_argument("name")
    alias_install.add_argument("--bin-dir", help="Directory for the alias symlink.")
    alias_install.add_argument("--target", help="ai-auth-switch executable target.")
    alias_install.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing alias executable.",
    )
    alias_install.set_defaults(func=_cmd_alias_install)

    return parser


def main(argv: Sequence[str] | None = None, *, program_name: str | None = None) -> int:
    invoked_as = Path(program_name or sys.argv[0]).name
    parser = build_parser(prog="ais" if invoked_as == "ais" else "ai-auth-switch")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if program_name is not None or argv is None:
            alias_status = _maybe_run_program_alias(
                raw_argv,
                program_name=program_name or sys.argv[0],
            )
            if alias_status is not None:
                return int(alias_status)

        args = parser.parse_args(raw_argv)
        return int(args.func(args))
    except AiAuthSwitchError as exc:
        print(f"ai-auth-switch: {exc}", file=sys.stderr)
        return 2
    except CliUsageError:
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
