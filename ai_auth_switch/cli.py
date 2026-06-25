from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Sequence

from ai_auth_switch import __version__
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider, get_provider
from ai_auth_switch.store import AuthStore
from ai_auth_switch.sync import SyncResult, sync_codex_dependents
from ai_auth_switch.wrapper import run_with_profile


SUPPORTED_PROVIDERS = ["codex"]


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
    with store.lock():
        for index, provider_id in enumerate(provider_ids):
            provider = _provider_by_id(provider_id, args)
            profiles = store.list_profiles(provider)
            if len(provider_ids) > 1:
                if index:
                    print()
                print(f"{provider.id}:")
            if not profiles:
                prefix = "  " if len(provider_ids) > 1 else ""
                print(f"{prefix}no profiles")
                print(f"{prefix}{_auth_hint(provider)}")
                continue
            for profile in profiles:
                mark = "*" if profile.active else " "
                suffix = " (content match)" if profile.by_content else ""
                prefix = "  " if len(provider_ids) > 1 else ""
                print(f"{prefix}{mark} {profile.name}{suffix}")
    return 0


def _cmd_auth_current(args: argparse.Namespace) -> int:
    store = _store_from_args(args)
    provider_ids = _provider_ids(args)
    missing = False
    with store.lock():
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
    print(f"saved {provider.id} auth as {profile.name}")
    print(f"active {provider.id} auth -> {profile.path}")
    _sync_after_auth_change(args, provider, store, profile_name=profile.name)
    return 0


def _cmd_auth_use(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.activate(provider, args.name)
    print(f"active {provider.id} auth -> {profile.name}")
    _sync_after_auth_change(args, provider, store, profile_name=profile.name)
    return 0


def _cmd_auth_sync(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
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
    print(f"removed {provider.id} profile {args.name}")
    return 0


def _cmd_auth_rename(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.rename(provider, args.old, args.new)
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

    def sync_selected() -> None:
        _sync_after_auth_change(args, provider, store, profile_name=args.name)

    def sync_current() -> None:
        _sync_after_auth_change(args, provider, store)

    return run_with_profile(
        store,
        provider,
        args.name,
        command,
        on_activated=sync_selected,
        on_restored=sync_current,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-auth-switch",
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
        help="Run a command under a profile, then restore the previous active auth.",
    )
    run.add_argument("provider", choices=SUPPORTED_PROVIDERS)
    run.add_argument("name")
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=_cmd_run)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except AiAuthSwitchError as exc:
        print(f"ai-auth-switch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
