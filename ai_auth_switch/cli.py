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
from ai_auth_switch.wrapper import run_with_profile


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


def _cmd_auth_list(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profiles = store.list_profiles(provider)
    if not profiles:
        print(f"no {provider.id} profiles")
        return 0
    for profile in profiles:
        mark = "*" if profile.active else " "
        suffix = " (content match)" if profile.by_content else ""
        print(f"{mark} {profile.name}{suffix}")
    return 0


def _cmd_auth_current(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        current = store.current_profile(provider)
    if current is None:
        print("not active")
        return 1
    suffix = " (content match)" if current.by_content else ""
    print(f"{current.name}{suffix}")
    return 0


def _cmd_auth_save(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.save_current(provider, args.name)
    print(f"saved {provider.id} auth as {profile.name}")
    print(f"active {provider.id} auth -> {profile.path}")
    return 0


def _cmd_auth_use(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.activate(provider, args.name)
    print(f"active {provider.id} auth -> {profile.name}")
    return 0


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
    return run_with_profile(store, provider, args.name, command)


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

    subparsers = parser.add_subparsers(dest="command_name", required=True)

    auth = subparsers.add_parser("auth", help="Manage saved auth profiles.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_list = auth_sub.add_parser("list", help="List profiles.")
    auth_list.add_argument("provider", choices=["codex"])
    auth_list.set_defaults(func=_cmd_auth_list)

    auth_current = auth_sub.add_parser("current", help="Show active profile.")
    auth_current.add_argument("provider", choices=["codex"])
    auth_current.set_defaults(func=_cmd_auth_current)

    auth_save = auth_sub.add_parser("save", help="Save the active auth file as a profile.")
    auth_save.add_argument("provider", choices=["codex"])
    auth_save.add_argument("name", nargs="?")
    auth_save.set_defaults(func=_cmd_auth_save)

    auth_use = auth_sub.add_parser("use", help="Activate a saved profile.")
    auth_use.add_argument("provider", choices=["codex"])
    auth_use.add_argument("name")
    auth_use.set_defaults(func=_cmd_auth_use)

    auth_login = auth_sub.add_parser("login", help="Run provider login and save the result.")
    auth_login.add_argument("provider", choices=["codex"])
    auth_login.add_argument("login_args", nargs=argparse.REMAINDER)
    auth_login.set_defaults(func=_cmd_auth_login)

    auth_rename = auth_sub.add_parser("rename", help="Rename a saved profile.")
    auth_rename.add_argument("provider", choices=["codex"])
    auth_rename.add_argument("old")
    auth_rename.add_argument("new")
    auth_rename.set_defaults(func=_cmd_auth_rename)

    auth_remove = auth_sub.add_parser("remove", help="Remove a saved inactive profile.")
    auth_remove.add_argument("provider", choices=["codex"])
    auth_remove.add_argument("name")
    auth_remove.set_defaults(func=_cmd_auth_remove)

    run = subparsers.add_parser(
        "run",
        help="Run a command under a profile, then restore the previous active auth.",
    )
    run.add_argument("provider", choices=["codex"])
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
