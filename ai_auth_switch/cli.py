from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from ai_auth_switch import __version__
from ai_auth_switch.complete import (
    bash_completion_script,
    complete_words,
    fish_completion_script,
    zsh_completion_script,
)
from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import ClaudeProvider, Provider, get_provider
from ai_auth_switch.providers.claude import AUTH_OVERRIDE_ENV_VARS
from ai_auth_switch.store import (
    AliasInfo,
    AuthStore,
    ProfileInfo,
    clear_binding,
    numbered_alias_parts,
    numbered_provider_alias_index,
    read_binding,
    resolve_binding,
    sanitize_profile_name,
    write_binding,
)
from ai_auth_switch.pool_config import POOL_PROVIDER_ID
from ai_auth_switch.sync import SyncResult, sync_codex_dependents
from ai_auth_switch.usage import (
    AccountUsage,
    fetch_profile_usage,
    format_usage,
    is_free_plan,
    usage_to_dict,
)
from ai_auth_switch.wrapper import run_with_profile

SUPPORTED_PROVIDERS = ["codex", "claude"]
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
RUN_AUTO_FLAG_OPTIONS = {"--auto", "--auto-refresh-usage"}
RUN_AUTO_VALUE_OPTIONS = {
    "--auto-usage-timeout",
    "--auto-usage-workers",
    "--auto-usage-cache-ttl",
}


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

    def error(self, message: str) -> NoReturn:
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


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
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


def _normalize_run_auto_options(argv: Sequence[str]) -> list[str]:
    args = list(argv)
    try:
        run_index = args.index("run")
    except ValueError:
        return args
    tail = args[run_index + 1 :]
    try:
        separator_index = tail.index("--")
    except ValueError:
        separator_index = len(tail)
    before_command = tail[:separator_index]
    command = tail[separator_index:]
    moved = []
    kept = []
    index = 0
    while index < len(before_command):
        token = before_command[index]
        if token in RUN_AUTO_FLAG_OPTIONS:
            moved.append(token)
            index += 1
            continue
        if token in RUN_AUTO_VALUE_OPTIONS:
            moved.append(token)
            index += 1
            if index < len(before_command):
                moved.append(before_command[index])
                index += 1
            continue
        if any(token.startswith(f"{option}=") for option in RUN_AUTO_VALUE_OPTIONS):
            moved.append(token)
            index += 1
            continue
        kept.append(token)
        index += 1
    return args[: run_index + 1] + moved + kept + command


def _provider_from_args(args: argparse.Namespace) -> Provider:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else None
    claude_config_dir = (
        Path(args.claude_config_dir).expanduser() if args.claude_config_dir else None
    )
    return get_provider(
        args.provider,
        codex_home=codex_home,
        claude_config_dir=claude_config_dir,
    )


def _store_from_args(args: argparse.Namespace) -> AuthStore:
    return AuthStore(Path(args.store_dir).expanduser() if args.store_dir else None)


def _provider_ids(args: argparse.Namespace) -> list[str]:
    provider = getattr(args, "provider", None)
    if provider:
        return [provider]
    return SUPPORTED_PROVIDERS


def _provider_by_id(provider_id: str, args: argparse.Namespace) -> Provider:
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else None
    claude_config_dir = (
        Path(args.claude_config_dir).expanduser() if args.claude_config_dir else None
    )
    return get_provider(
        provider_id,
        codex_home=codex_home,
        claude_config_dir=claude_config_dir,
    )


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
        raise AiAuthSwitchError(
            f"ai-auth-switch executable not found: {candidate}"
        ) from exc
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
    provider_id: str,
    bin_dir: Path,
    target: Path,
) -> AliasLinkSyncResult:
    automatic = {
        alias.name: alias
        for alias in aliases
        if alias.provider_id == provider_id
        and numbered_provider_alias_index(provider_id, alias.name) is not None
    }
    try:
        bin_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AiAuthSwitchError(
            f"failed to create alias directory {bin_dir}: {exc}"
        ) from exc

    installed: list[Path] = []
    updated: list[Path] = []
    removed: list[Path] = []
    conflicts: list[Path] = []
    for name in sorted(
        automatic,
        key=lambda item: numbered_provider_alias_index(provider_id, item) or 0,
    ):
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
        raise AiAuthSwitchError(
            f"failed to inspect alias directory {bin_dir}: {exc}"
        ) from exc
    for link in candidates:
        if (
            numbered_provider_alias_index(provider_id, link.name) is not None
            and link.name not in automatic
            and (_symlink_points_to(link, target) or _is_managed_alias_link(link))
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
    *,
    provider_id: str,
) -> None:
    bin_dir = _automatic_alias_bin_dir(args)
    if bin_dir is None:
        return
    target = _alias_executable_target()
    try:
        result = _sync_numbered_alias_executables(
            aliases,
            provider_id=provider_id,
            bin_dir=bin_dir,
            target=target,
        )
    except AiAuthSwitchError as exc:
        print(f"ai-auth-switch: automatic alias sync failed: {exc}", file=sys.stderr)
        return
    _print_alias_link_sync(result)
    for name in BUILTIN_SHORTCUTS:
        _ensure_builtin_shortcut(name, bin_dir=bin_dir, target=target)


def _profile_sort_key(
    aliases_by_profile: dict[str, str],
    profile: ProfileInfo,
    usages: dict[str, AccountUsage] | None = None,
) -> tuple[int, int, int | str]:
    alias_name = aliases_by_profile.get(profile.name)
    if alias_name is not None:
        parts = numbered_alias_parts(alias_name)
        alias_rank: tuple[int, int | str] = (0, parts[1] if parts is not None else 0)
    else:
        alias_rank = (1, profile.name)
    free_rank = (
        1 if usages is not None and is_free_plan(usages.get(profile.name)) else 0
    )
    return (free_rank, alias_rank[0], alias_rank[1])


def _auth_hint(provider: Provider) -> str:
    active = provider.active_auth_path
    if not active.exists() and not active.is_symlink():
        if provider.id == "claude":
            override = "set CLAUDE_CONFIG_DIR or pass --claude-config-dir"
        else:
            override = "set CODEX_HOME or pass --codex-home"
        return (
            f"auth file not found at {active}; {override} if {provider.id} "
            "uses another config directory"
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
    if provider.id == "claude":
        overrides = [name for name in AUTH_OVERRIDE_ENV_VARS if os.environ.get(name)]
        if overrides:
            print(
                "ai-auth-switch: warning: Claude environment authentication "
                f"overrides the active OAuth profile: {', '.join(overrides)}; "
                "unset it for permanent switching, or use claudeN/`run claude` "
                "which isolates these variables",
                file=sys.stderr,
            )
        return []
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
        results = [
            SyncResult(target="dependent-sync", status="error", message=str(exc))
        ]
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
            and numbered_provider_alias_index(provider.id, alias.name) is not None
        }
        profiles = sorted(
            profiles,
            key=lambda p: _profile_sort_key(aliases_by_profile, p, usages),
        )
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
            if actual_name and actual_name.casefold() != profile.name.casefold():
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


def _cmd_auth_default(args: argparse.Namespace) -> int:
    """Show, set, or clear the default profile per provider.

    The default profile is used by ``run`` (and other profile consumers) when
    no explicit profile is given. Bindings take precedence over the default.
    """
    store = _store_from_args(args)
    provider_ids = _provider_ids(args)
    if args.clear:
        for provider_id in provider_ids:
            store.clear_default(_provider_by_id(provider_id, args))
        print(f"cleared default profile for {', '.join(provider_ids)}")
        return 0
    if args.name:
        if not args.provider:
            raise AiAuthSwitchError(
                "provider is required when setting a default profile"
            )
        provider = _provider_by_id(args.provider, args)
        store.set_default(provider, args.name)
        print(f"default {provider.id} profile -> {args.name}")
        return 0
    if args.json:
        print(
            json.dumps(
                {
                    "providers": {
                        provider_id: {
                            "default": store.get_default(
                                _provider_by_id(provider_id, args)
                            )
                        }
                        for provider_id in provider_ids
                    }
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for provider_id in provider_ids:
        provider = _provider_by_id(provider_id, args)
        current = store.get_default(provider)
        prefix = f"{provider.id}: " if len(provider_ids) > 1 else ""
        if current:
            print(f"{prefix}default profile -> {current}")
        else:
            print(f"{prefix}no default profile")
    return 0


def _cmd_auth_bind(args: argparse.Namespace) -> int:
    """Show, set, or clear the directory binding for a provider.

    Bindings are stored in ``.ai-auth-switch.json`` in the target directory and
    resolved from the nearest ancestor directory.
    """
    store = _store_from_args(args)
    target = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()
    provider_ids = _provider_ids(args)
    if args.clear:
        for provider_id in provider_ids:
            clear_binding(target, provider_id)
        print(f"cleared binding for {', '.join(provider_ids)} in {target}")
        return 0
    if args.name:
        if not args.provider:
            raise AiAuthSwitchError(
                "provider is required when setting a directory binding"
            )
        provider = _provider_by_id(args.provider, args)
        if not store.profile_path(provider, args.name).exists():
            raise AiAuthSwitchError(f"profile not found: {args.name}")
        bindings = read_binding(target)
        bindings[provider.id] = sanitize_profile_name(args.name)
        write_binding(target, bindings)
        print(f"bound {provider.id} profile {args.name} to {target}")
        return 0
    if args.json:
        print(
            json.dumps(
                {
                    "providers": {
                        provider_id: {
                            "binding": resolve_binding(provider_id, target),
                            "dir": str(target),
                        }
                        for provider_id in provider_ids
                    }
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    for provider_id in provider_ids:
        provider = _provider_by_id(provider_id, args)
        bound = resolve_binding(provider_id, target)
        prefix = f"{provider.id}: " if len(provider_ids) > 1 else ""
        if bound:
            print(f"{prefix}bound profile -> {bound} (resolved from {target})")
        else:
            print(f"{prefix}no binding for {target}")
    return 0


def _cmd_auth_export(args: argparse.Namespace) -> int:
    """Export saved profiles as JSON for migration to another machine."""
    store = _store_from_args(args)
    provider_ids = _provider_ids(args)
    payload = {
        "version": 2,
        "providers": {
            provider_id: store.export_provider_profiles(
                _provider_by_id(provider_id, args)
            )
            for provider_id in provider_ids
        },
        "profile_metadata": {
            provider_id: store.export_provider_metadata(
                _provider_by_id(provider_id, args)
            )
            for provider_id in provider_ids
        },
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if args.output:
        from ai_auth_switch.utils import atomic_write

        output = Path(args.output).expanduser()
        atomic_write(output, text)
        print(f"exported {', '.join(provider_ids)} profiles to {output}")
    else:
        print(text, end="")
    print(
        "warning: the export contains credentials; keep it private",
        file=sys.stderr,
    )
    return 0


def _cmd_auth_import(args: argparse.Namespace) -> int:
    """Import profiles from a JSON export produced by ``auth export``.

    Pass ``-`` as the file to read the export from standard input, enabling
    ``ai-auth-switch auth export | ai-auth-switch auth import -`` pipelines.
    """
    store = _store_from_args(args)
    if args.file == "-":
        text = sys.stdin.read()
        source = "<stdin>"
    else:
        path = Path(args.file).expanduser()
        source = str(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AiAuthSwitchError(f"failed to read {source}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiAuthSwitchError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise AiAuthSwitchError(f"expected JSON object in {source}")
    providers_data = data.get("providers")
    if not isinstance(providers_data, dict):
        raise AiAuthSwitchError(f"missing 'providers' object in {source}")
    all_metadata = data.get("profile_metadata")
    if not isinstance(all_metadata, dict):
        all_metadata = {}

    total_imported = 0
    total_skipped = 0
    for provider_id, profiles in sorted(providers_data.items()):
        if not isinstance(provider_id, str) or not isinstance(profiles, dict):
            continue
        try:
            provider = _provider_by_id(provider_id, args)
        except AiAuthSwitchError:
            print(
                f"skipped unsupported provider in export: {provider_id}",
                file=sys.stderr,
            )
            continue
        with store.lock():
            imported, skipped = store.import_provider_profiles(
                provider,
                profiles,
                force=args.force,
            )
            provider_metadata = all_metadata.get(provider_id)
            if isinstance(provider_metadata, dict):
                for name in imported:
                    metadata = provider_metadata.get(name)
                    if isinstance(metadata, dict):
                        store.write_profile_metadata(provider, name, metadata)
            automatic_aliases = store.sync_numbered_aliases(provider)
            _sync_automatic_alias_links(
                args,
                automatic_aliases,
                provider_id=provider.id,
            )
        for name in imported:
            print(f"imported {provider_id} profile {name}")
        for name in skipped:
            print(f"skipped existing {provider_id} profile {name}")
        total_imported += len(imported)
        total_skipped += len(skipped)
    if total_imported == 0 and total_skipped == 0:
        print("no profiles imported")
    return 0


def _cmd_auth_save(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.save_current(provider, args.name)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
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
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
    print(f"active {provider.id} auth -> {profile.name}")
    _sync_after_auth_change(args, provider, store, profile_name=profile.name)
    return 0


def _cmd_auth_sync(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    if provider.id != "codex":
        print(f"no dependent auth sync is configured for {provider.id}")
        return 0
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


def _cmd_auth_refresh(args: argparse.Namespace) -> int:
    from ai_auth_switch.refresh import (
        LOGIN_REQUIRED,
        REFRESHED,
        refresh_profiles,
        result_to_dict,
        supports_refresh,
    )

    provider = _provider_from_args(args)
    store = _store_from_args(args)
    if not supports_refresh(provider):
        raise AiAuthSwitchError(f"token refresh is not supported for {provider.id}")
    known = {profile.name for profile in store.list_profiles(provider)}
    if args.all:
        names = sorted(known)
    elif args.name:
        names = [sanitize_profile_name(name) for name in args.name]
        unknown = [name for name in names if name not in known]
        if unknown:
            raise AiAuthSwitchError(
                f"unknown {provider.id} profile(s): {', '.join(unknown)}"
            )
    else:
        current = store.current_profile(provider)
        if current is None:
            raise AiAuthSwitchError(
                "no active profile; name one explicitly or pass --all"
            )
        names = [current.name]
    if not names:
        raise AiAuthSwitchError(f"no saved {provider.id} profiles")

    results = refresh_profiles(
        store,
        provider,
        names,
        force=args.force,
        timeout=args.timeout,
        workers=args.workers,
    )
    if args.json:
        print(
            json.dumps(
                {"results": [result_to_dict(result) for result in results]},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        width = max(len(result.profile) for result in results)
        status_width = max(len(result.status) for result in results)
        for result in results:
            print(
                f"{result.profile:{width}}  "
                f"{result.status:<{status_width}}  {result.message}"
            )
        needs_login = [r.profile for r in results if r.status == LOGIN_REQUIRED]
        if needs_login:
            print()
            print(
                "these refresh tokens are permanently rejected; log in again with "
                f"`ais auth login {provider.id} <profile>`:"
            )
            for name in needs_login:
                print(f"  {name}")
    changed = {result.profile for result in results if result.status == REFRESHED}
    active = store.current_profile(provider)
    if active is not None and active.name in changed and not args.json:
        # The active auth file is a symlink to the profile, so it already
        # carries the new token; dependent tools copy it and must be resynced.
        _sync_after_auth_change(args, provider, store, profile_name=active.name)
    return 0 if all(result.status != LOGIN_REQUIRED for result in results) else 1


def _cmd_auth_remove(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        store.remove(provider, args.name)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
    print(f"removed {provider.id} profile {args.name}")
    return 0


def _cmd_auth_rename(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    with store.lock():
        profile = store.rename(provider, args.old, args.new)
        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
    print(f"renamed {provider.id} profile {args.old} -> {profile.name}")
    return 0


def _run_login(provider: Provider, login_args: Sequence[str]) -> int:
    command = (
        list(provider.login_command)
        + list(provider.login_args)
        + _strip_separator(login_args)
    )
    if isinstance(provider, ClaudeProvider):
        env = os.environ.copy()
        for name in AUTH_OVERRIDE_ENV_VARS:
            env.pop(name, None)
        if provider.explicit_config_dir:
            env["CLAUDE_CONFIG_DIR"] = str(provider.config_dir)
        return subprocess.call(command, env=env)
    return subprocess.call(command)


def _cmd_auth_login(args: argparse.Namespace) -> int:
    provider = _provider_from_args(args)
    store = _store_from_args(args)
    name, login_args = _split_login_name_and_args(args.login_args)
    active = provider.active_auth_path
    backup = active.with_name(
        f".{active.name}.login-backup.{os.getpid()}.{time.time_ns()}"
    )
    had_active = False
    state_path = (
        provider.config_state_path if isinstance(provider, ClaudeProvider) else None
    )
    state_backup = (
        state_path.with_name(
            f".{state_path.name}.login-backup.{os.getpid()}.{time.time_ns()}"
        )
        if state_path is not None
        else None
    )
    had_state = bool(state_path is not None and state_path.is_file())

    with store.lock():
        active.parent.mkdir(parents=True, exist_ok=True)
        if had_state and state_path is not None and state_backup is not None:
            shutil.copy2(state_path, state_backup)
        if active.exists() or active.is_symlink():
            os.replace(active, backup)
            had_active = True

        status = _run_login(provider, login_args)
        if status != 0:
            if active.exists() or active.is_symlink():
                active.unlink()
            if had_active:
                os.replace(backup, active)
            if had_state and state_path is not None and state_backup is not None:
                os.replace(state_backup, state_path)
            elif state_path is not None and state_path.exists():
                state_path.unlink()
            return status

        try:
            profile = store.save_current(provider, name)
        except Exception:
            if active.exists() or active.is_symlink():
                active.unlink()
            if had_active:
                os.replace(backup, active)
            if had_state and state_path is not None and state_backup is not None:
                os.replace(state_backup, state_path)
            elif state_path is not None and state_path.exists():
                state_path.unlink()
            raise

        if had_active and backup.exists():
            backup.unlink()
        if state_backup is not None and state_backup.exists():
            state_backup.unlink()

        automatic_aliases = store.sync_numbered_aliases(provider)
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
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
    command_parts = list(args.command)
    if args.auto and args.name:
        command_parts.insert(0, args.name)
    command = _strip_separator(command_parts)
    if not command:
        command = list(provider.login_command)

    if args.auto:
        from ai_auth_switch.auto_run import AutoRunConfig, acquire_auto_run_profile

        config = AutoRunConfig(
            usage_timeout=args.auto_usage_timeout,
            usage_workers=args.auto_usage_workers,
            usage_cache_ttl=args.auto_usage_cache_ttl,
            refresh_usage=args.auto_refresh_usage,
        )
        with acquire_auto_run_profile(store, provider, config) as selection:
            print(
                f"ai-auth-switch: auto-selected {provider.id} profile "
                f"{selection.profile} ({selection.remaining_percent:g}% remaining, "
                f"{selection.active_leases} active auto run(s))",
                file=sys.stderr,
            )
            return run_with_profile(
                store,
                provider,
                selection.profile,
                command,
            )

    name = args.name
    if not name:
        name = store.get_default(provider) or resolve_binding(provider.id, Path.cwd())
    if not name:
        raise AiAuthSwitchError(
            f"no profile specified; pass a profile name, or set a default with "
            f"`ai-auth-switch auth default {provider.id} <name>` or bind one to "
            f"this directory with `ai-auth-switch auth bind {provider.id} <name>`"
        )

    return run_with_profile(
        store,
        provider,
        name,
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
        _sync_automatic_alias_links(args, automatic_aliases, provider_id=provider.id)
    print(f"saved alias {_alias_display(alias)}")
    return 0


def _cmd_alias_remove(args: argparse.Namespace) -> int:
    parts = numbered_alias_parts(args.name)
    if parts is not None:
        raise AiAuthSwitchError(
            f"{args.name} is managed automatically; remove its {parts[0]} profile instead"
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
            provider_id=provider.id,
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

    bin_dir = (
        Path(args.bin_dir).expanduser() if args.bin_dir else Path.home() / ".local/bin"
    )
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


def _desktop_provider_and_store(args: argparse.Namespace) -> tuple[Provider, AuthStore]:
    return _provider_by_id("codex", args), _store_from_args(args)


def _cmd_desktop_auto_install(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop import DesktopAutoConfig, install_desktop_auto

    provider, store = _desktop_provider_and_store(args)
    config = DesktopAutoConfig(
        idle_seconds=args.idle_seconds,
        cooldown_seconds=args.cooldown_seconds,
        poll_seconds=args.poll_seconds,
        switch_below_remaining=args.switch_below,
        min_improvement=args.min_improvement,
        usage_timeout=args.usage_timeout,
        usage_workers=args.usage_workers,
        usage_cache_ttl=args.usage_cache_ttl,
    )
    paths = install_desktop_auto(
        store,
        provider,
        _alias_executable_target(),
        config,
        enable=not args.no_enable,
    )
    print(f"installed desktop auto-rotation service: {paths.service}")
    print(f"installed managed ChatGPT launcher: {paths.launcher}")
    if args.no_enable:
        print("service installed but not enabled")
    print("restart ChatGPT Desktop once to enter managed daemon mode")
    return 0


def _cmd_desktop_auto_status(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop import desktop_auto_status

    provider, store = _desktop_provider_and_store(args)
    status = desktop_auto_status(store, provider)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    print(f"installed: {'yes' if status['installed'] else 'no'}")
    print(f"service enabled: {'yes' if status['service_enabled'] else 'no'}")
    print(f"service active: {'yes' if status['service_active'] else 'no'}")
    print(f"desktop mode: {status['desktop_mode']}")
    print(f"current profile: {status['current_profile'] or 'not active'}")
    active_threads = status["active_threads"]
    if active_threads is not None:
        print(f"active desktop turns: {len(active_threads)}")
    if status["app_server_error"]:
        print(f"app-server: {status['app_server_error']}")
    if status["restart_required"]:
        print("restart required: close and reopen ChatGPT Desktop once")
    return 0


def _cmd_desktop_auto_disable(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop import disable_desktop_auto

    _provider, store = _desktop_provider_and_store(args)
    paths = disable_desktop_auto(store)
    print(f"disabled desktop auto-rotation service: {paths.service}")
    print("restart ChatGPT Desktop once to return to its direct app-server")
    return 0


def _cmd_desktop_auto_run(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop import run_desktop_auto

    provider, store = _desktop_provider_and_store(args)
    return run_desktop_auto(store, provider)


def _cmd_desktop_rotate(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop import (
        desktop_paths,
        load_desktop_config,
        load_desktop_state,
        rotate_desktop_account,
        save_desktop_state,
    )

    provider, store = _desktop_provider_and_store(args)
    paths = desktop_paths(store)
    config = load_desktop_config(paths.config)
    state = load_desktop_state(paths.state)
    result = rotate_desktop_account(
        store,
        provider,
        config,
        state,
        force=args.now,
    )
    save_desktop_state(paths.state, state)
    payload = {
        "changed": result.changed,
        "previous_profile": result.previous_profile,
        "profile": result.profile,
        "reason": result.reason,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif result.changed:
        print(
            f"desktop account: {result.previous_profile or '<none>'} "
            f"-> {result.profile}"
        )
    else:
        print(f"desktop account unchanged: {result.reason}")
    return 0


def _cmd_desktop_pool_install(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop_pool import install_desktop_pool

    provider, store = _desktop_provider_and_store(args)
    paths = install_desktop_pool(store, provider, port=args.pool_port)
    print(f"installed desktop pool launcher: {paths.launcher}")
    print(f"installed desktop pool service: {paths.service}")
    if paths.config_backup:
        print(f"saved previous Codex config: {paths.config_backup}")
    print("restart ChatGPT Desktop once to use the local account pool")
    return 0


def _cmd_desktop_pool_disable(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop_pool import disable_desktop_pool

    provider, store = _desktop_provider_and_store(args)
    paths = disable_desktop_pool(store, provider)
    print(f"disabled desktop pool service: {paths.service}")
    print("restart ChatGPT Desktop once to restore its original launcher")
    return 0


def _cmd_desktop_pool_status(args: argparse.Namespace) -> int:
    from ai_auth_switch.desktop_pool import desktop_pool_status

    provider, store = _desktop_provider_and_store(args)
    status = desktop_pool_status(store, provider)
    if args.json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(f"installed: {'yes' if status['installed'] else 'no'}")
        print(f"launcher: {status['launcher']}")
        print(f"service: {status['service']}")
        print(f"token file: {status['token_file']}")
    return 0


def _cmd_pool_app_server(args: argparse.Namespace) -> int:
    from ai_auth_switch.pool_server import PoolAppServer, PoolServerConfig

    provider, store = _desktop_provider_and_store(args)
    command = [args.codex_bin] if args.codex_bin else None
    server = PoolAppServer(
        store,
        provider,
        command=command,
        config=PoolServerConfig(
            usage_timeout=args.pool_usage_timeout,
            usage_workers=args.pool_usage_workers,
            usage_cache_ttl=args.pool_usage_cache_ttl,
            refresh_usage=args.pool_refresh_usage,
            backend_timeout=args.pool_backend_timeout,
            auto_refresh=args.pool_auto_refresh,
        ),
    )
    return server.run()


def _cmd_pool_responses(args: argparse.Namespace) -> int:
    from ai_auth_switch.pool_responses import PoolResponsesProxy, ResponsesProxyConfig

    provider, store = _desktop_provider_and_store(args)
    proxy = PoolResponsesProxy(
        store,
        provider,
        config=ResponsesProxyConfig(
            upstream_url=args.pool_upstream_url,
            host=args.pool_host,
            port=args.pool_port,
            usage_timeout=args.pool_usage_timeout,
            usage_workers=args.pool_usage_workers,
            usage_cache_ttl=args.pool_usage_cache_ttl,
            refresh_usage=args.pool_refresh_usage,
            max_retries=args.pool_max_retries,
            request_timeout=args.pool_request_timeout,
            token_file=Path(args.pool_token_file).expanduser()
            if args.pool_token_file
            else None,
            auto_refresh=args.pool_auto_refresh,
        ),
    )
    address = f"http://{args.pool_host}:{args.pool_port}"
    print(f"pool Responses endpoint: {address}/v1/responses")
    print(f"pool token file: {proxy.token_path}")
    print(
        "set the custom provider API key from that file; keep the listener loopback-only"
    )
    proxy.serve_forever()
    return 0


def _cmd_pool_configure(args: argparse.Namespace) -> int:
    from ai_auth_switch.pool_config import install_pool_provider

    provider = _provider_by_id("codex", args)
    result = install_pool_provider(
        provider.active_auth_path.parent,
        base_url=f"http://127.0.0.1:{args.pool_port}/v1",
        provider_id=args.pool_provider_id,
        env_key=args.pool_env_key,
        backup=not args.pool_no_backup,
    )
    print(
        f"configured Codex custom provider {result.provider_id}: {result.config_path}"
    )
    if result.backup_path:
        print(f"saved previous config: {result.backup_path}")
    if not result.changed:
        print("configuration already matched")
    token_file = Path(args.pool_token_file).expanduser()
    print(f'export {args.pool_env_key}="$(cat {token_file})"')
    return 0


def _cmd_pool_restore(args: argparse.Namespace) -> int:
    from ai_auth_switch.pool_config import restore_codex_config

    provider = _provider_by_id("codex", args)
    path = restore_codex_config(
        provider.active_auth_path.parent,
        Path(args.pool_backup),
    )
    print(f"restored Codex config: {path}")
    return 0


def _cmd_pool_status(args: argparse.Namespace) -> int:
    from ai_auth_switch.pool import PoolCoordinator

    provider = _provider_by_id("codex", args)
    store = _store_from_args(args)
    coordinator = PoolCoordinator(store, provider)
    state = coordinator.load()
    payload = state.to_dict()
    payload["provider"] = provider.id
    payload["state_path"] = str(coordinator.path)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"provider: {provider.id}")
    print(f"state: {payload['state_path']}")
    print(f"leases: {len(state.leases)}")
    print(f"routes: {len(state.routes)}")
    for profile, health in sorted(state.health.items()):
        print(f"{profile}: {health.status}")
    return 0


def _cmd_completion(args: argparse.Namespace) -> int:
    scripts = {
        "bash": bash_completion_script,
        "zsh": zsh_completion_script,
        "fish": fish_completion_script,
    }
    print(scripts[args.shell](), end="")
    return 0


def _cmd_complete(args: argparse.Namespace) -> int:
    for candidate in complete_words(args.words):
        print(candidate)
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
            f"run `ai-auth-switch alias set {alias_name} <provider> <profile>` first"
        )
    return _run_alias(store, alias, argv)


def build_parser(
    *,
    prog: str = "ai-auth-switch",
    require_command: bool = True,
    completion: bool = False,
) -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    *require_command* controls whether a top-level command is mandatory.
    *completion* builds a tolerant variant for shell completion where every
    positional is optional, so ``parse_known_args`` succeeds on partial input
    such as ``auth use codex`` and the completion walker can suggest the next
    token.
    """

    def opt(nargs: object) -> object:
        return "?" if completion else nargs

    parser = HelpfulArgumentParser(
        prog=prog,
        description="Switch auth profiles for AI coding agents.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--store-dir",
        help="Profile store directory. Defaults to $AI_AUTH_SWITCH_HOME or ~/.local/share/ai-auth-switch.",
    )
    parser.add_argument(
        "--codex-home",
        help="Override Codex config directory for the codex provider.",
    )
    parser.add_argument(
        "--claude-config-dir",
        help="Override Claude Code config directory for the claude provider.",
    )
    parser.add_argument(
        "--no-dependent-sync",
        action="store_true",
        help="Do not sync Hermes/OpenClaw after changing active Codex auth.",
    )

    subparsers = parser.add_subparsers(dest="command_name", required=require_command)

    auth = subparsers.add_parser("auth", help="Manage saved auth profiles.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=require_command)

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

    auth_save = auth_sub.add_parser(
        "save", help="Save the active auth file as a profile."
    )
    auth_save.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    auth_save.add_argument("name", nargs="?")
    auth_save.set_defaults(func=_cmd_auth_save)

    auth_use = auth_sub.add_parser("use", help="Activate a saved profile.")
    auth_use.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    auth_use.add_argument("name", nargs=opt(None))
    auth_use.set_defaults(func=_cmd_auth_use)

    auth_sync = auth_sub.add_parser(
        "sync",
        help="Sync dependent tool auth from the active provider auth.",
    )
    auth_sync.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
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

    auth_refresh = auth_sub.add_parser(
        "refresh",
        help="Exchange saved Codex refresh tokens for new access tokens.",
    )
    auth_refresh.add_argument(
        "provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS
    )
    auth_refresh.add_argument(
        "name",
        nargs="*",
        help="Profiles to refresh. Defaults to the active profile.",
    )
    auth_refresh.add_argument(
        "--all",
        action="store_true",
        help="Refresh every saved profile.",
    )
    auth_refresh.add_argument(
        "--force",
        action="store_true",
        help="Refresh even when the stored access token has not expired.",
    )
    auth_refresh.add_argument(
        "--timeout",
        type=_positive_float,
        default=30.0,
        metavar="SECONDS",
        help="Per-profile token request timeout (default: 30).",
    )
    auth_refresh.add_argument(
        "--workers",
        type=_positive_int,
        default=4,
        metavar="COUNT",
        help="Maximum concurrent token requests (default: 4).",
    )
    auth_refresh.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON.",
    )
    auth_refresh.set_defaults(func=_cmd_auth_refresh)

    auth_login = auth_sub.add_parser(
        "login", help="Run provider login and save the result."
    )
    auth_login.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    auth_login.add_argument("login_args", nargs=argparse.REMAINDER)
    auth_login.set_defaults(func=_cmd_auth_login)

    auth_rename = auth_sub.add_parser("rename", help="Rename a saved profile.")
    auth_rename.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    auth_rename.add_argument("old", nargs=opt(None))
    auth_rename.add_argument("new", nargs=opt(None))
    auth_rename.set_defaults(func=_cmd_auth_rename)

    auth_remove = auth_sub.add_parser("remove", help="Remove a saved inactive profile.")
    auth_remove.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    auth_remove.add_argument("name", nargs=opt(None))
    auth_remove.set_defaults(func=_cmd_auth_remove)

    auth_default = auth_sub.add_parser(
        "default",
        help="Show, set, or clear the default profile for a provider.",
    )
    auth_default.add_argument("provider", nargs="?", choices=SUPPORTED_PROVIDERS)
    auth_default.add_argument("name", nargs="?")
    auth_default.add_argument(
        "--clear",
        action="store_true",
        help="Clear the default profile.",
    )
    auth_default.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON.",
    )
    auth_default.set_defaults(func=_cmd_auth_default)

    auth_bind = auth_sub.add_parser(
        "bind",
        help="Show, set, or clear the profile bound to a directory.",
    )
    auth_bind.add_argument("provider", nargs="?", choices=SUPPORTED_PROVIDERS)
    auth_bind.add_argument("name", nargs="?")
    auth_bind.add_argument(
        "--clear",
        action="store_true",
        help="Clear the binding in the target directory.",
    )
    auth_bind.add_argument(
        "--dir",
        help="Directory to bind instead of the current directory.",
    )
    auth_bind.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON.",
    )
    auth_bind.set_defaults(func=_cmd_auth_bind)

    auth_export = auth_sub.add_parser(
        "export",
        help="Export saved profiles as JSON for migration.",
    )
    auth_export.add_argument("provider", nargs="?", choices=SUPPORTED_PROVIDERS)
    auth_export.add_argument(
        "-o",
        "--output",
        help="Write the export to FILE instead of stdout.",
    )
    auth_export.set_defaults(func=_cmd_auth_export)

    auth_import = auth_sub.add_parser(
        "import",
        help="Import profiles from a JSON export.",
    )
    auth_import.add_argument("file", help="Export file produced by `auth export`.")
    auth_import.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing profiles with the same name.",
    )
    auth_import.set_defaults(func=_cmd_auth_import)

    run = subparsers.add_parser(
        "run",
        help="Run a command with isolated auth while sharing provider state.",
    )
    run.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    run.add_argument(
        "--auto",
        action="store_true",
        help="Automatically select a Codex profile by quota and active auto runs.",
    )
    run.add_argument(
        "--auto-usage-timeout",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Per-account auto-selection usage timeout (default: 5).",
    )
    run.add_argument(
        "--auto-usage-workers",
        type=_positive_int,
        default=4,
        metavar="COUNT",
        help="Maximum concurrent auto-selection usage requests (default: 4).",
    )
    run.add_argument(
        "--auto-usage-cache-ttl",
        type=_nonnegative_float,
        default=60.0,
        metavar="SECONDS",
        help="Reuse auto-selection usage results for this long (default: 60).",
    )
    run.add_argument(
        "--auto-refresh-usage",
        action="store_true",
        help="Bypass cached usage during automatic profile selection.",
    )
    run.add_argument(
        "name",
        nargs="?",
        help="Saved profile to activate; defaults to the provider default or "
        "nearest directory binding. With --auto, this begins the child command.",
    )
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=_cmd_run)

    alias = subparsers.add_parser("alias", help="Manage command aliases.")
    alias_sub = alias.add_subparsers(dest="alias_command", required=require_command)

    alias_list = alias_sub.add_parser("list", help="List command aliases.")
    alias_list.set_defaults(func=_cmd_alias_list)

    alias_sync = alias_sub.add_parser(
        "sync",
        help="Create and install contiguous provider1, provider2, ... aliases.",
    )
    alias_sync.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    alias_sync.add_argument("--bin-dir", help="Directory for automatic alias symlinks.")
    alias_sync.add_argument("--target", help="ai-auth-switch executable target.")
    alias_sync.set_defaults(func=_cmd_alias_sync)

    alias_set = alias_sub.add_parser("set", help="Create or update a command alias.")
    alias_set.add_argument(
        "name",
        nargs=opt(None),
        help="Alias executable name, for example codex-work or claude-work.",
    )
    alias_set.add_argument("provider", nargs=opt(None), choices=SUPPORTED_PROVIDERS)
    alias_set.add_argument(
        "profile", nargs=opt(None), help="Saved provider profile to activate."
    )
    alias_set.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Command to run after '--'. Defaults to the provider command.",
    )
    alias_set.set_defaults(func=_cmd_alias_set)

    alias_remove = alias_sub.add_parser("remove", help="Remove a command alias.")
    alias_remove.add_argument("name", nargs=opt(None))
    alias_remove.set_defaults(func=_cmd_alias_remove)

    alias_run = alias_sub.add_parser("run", help="Run a command alias by name.")
    alias_run.add_argument("name", nargs=opt(None))
    alias_run.add_argument("command", nargs=argparse.REMAINDER)
    alias_run.set_defaults(func=_cmd_alias_run)

    alias_install = alias_sub.add_parser(
        "install",
        help="Install a symlink so the alias can be invoked directly.",
    )
    alias_install.add_argument("name", nargs=opt(None))
    alias_install.add_argument("--bin-dir", help="Directory for the alias symlink.")
    alias_install.add_argument("--target", help="ai-auth-switch executable target.")
    alias_install.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing alias executable.",
    )
    alias_install.set_defaults(func=_cmd_alias_install)

    desktop = subparsers.add_parser(
        "desktop",
        help="Manage ChatGPT Desktop account rotation.",
    )
    desktop_sub = desktop.add_subparsers(
        dest="desktop_command",
        required=require_command,
    )

    desktop_auto = desktop_sub.add_parser(
        "auto",
        help="Install or manage idle account auto-rotation.",
    )
    desktop_auto_sub = desktop_auto.add_subparsers(
        dest="desktop_auto_command",
        required=require_command,
    )
    desktop_auto_install = desktop_auto_sub.add_parser(
        "install",
        help="Install and enable idle account auto-rotation.",
    )
    desktop_auto_install.add_argument(
        "--idle-seconds",
        type=_nonnegative_float,
        default=60.0,
        metavar="SECONDS",
        help="Require this much continuous desktop idle time (default: 60).",
    )
    desktop_auto_install.add_argument(
        "--cooldown-seconds",
        type=_nonnegative_float,
        default=1800.0,
        metavar="SECONDS",
        help="Minimum time between account switches (default: 1800).",
    )
    desktop_auto_install.add_argument(
        "--poll-seconds",
        type=_positive_float,
        default=15.0,
        metavar="SECONDS",
        help="Desktop state polling interval (default: 15).",
    )
    desktop_auto_install.add_argument(
        "--switch-below",
        type=_nonnegative_float,
        default=10.0,
        metavar="PERCENT",
        help="Switch when current remaining quota is at or below this value "
        "(default: 10).",
    )
    desktop_auto_install.add_argument(
        "--min-improvement",
        type=_nonnegative_float,
        default=5.0,
        metavar="PERCENT",
        help="Required remaining-quota improvement before switching (default: 5).",
    )
    desktop_auto_install.add_argument(
        "--usage-timeout",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
        help="Per-account usage request timeout (default: 5).",
    )
    desktop_auto_install.add_argument(
        "--usage-workers",
        type=_positive_int,
        default=4,
        metavar="COUNT",
        help="Maximum concurrent usage requests (default: 4).",
    )
    desktop_auto_install.add_argument(
        "--usage-cache-ttl",
        type=_nonnegative_float,
        default=60.0,
        metavar="SECONDS",
        help="Reuse usage results for this long (default: 60).",
    )
    desktop_auto_install.add_argument(
        "--no-enable",
        action="store_true",
        help="Write integration files without enabling the user service.",
    )
    desktop_auto_install.set_defaults(func=_cmd_desktop_auto_install)

    desktop_auto_status_parser = desktop_auto_sub.add_parser(
        "status",
        help="Show desktop auto-rotation and app-server state.",
    )
    desktop_auto_status_parser.add_argument("--json", action="store_true")
    desktop_auto_status_parser.set_defaults(func=_cmd_desktop_auto_status)

    desktop_auto_disable = desktop_auto_sub.add_parser(
        "disable",
        help="Disable auto-rotation and restore the desktop launcher.",
    )
    desktop_auto_disable.set_defaults(func=_cmd_desktop_auto_disable)

    desktop_auto_run = desktop_auto_sub.add_parser(
        "run",
        help="Run the installed auto-rotation worker.",
    )
    desktop_auto_run.set_defaults(func=_cmd_desktop_auto_run)

    desktop_rotate = desktop_sub.add_parser(
        "rotate",
        help="Safely choose another desktop account while idle.",
    )
    desktop_rotate.add_argument(
        "--now",
        action="store_true",
        help="Ignore quota threshold and cooldown, but never interrupt an active turn.",
    )
    desktop_rotate.add_argument("--json", action="store_true")
    desktop_rotate.set_defaults(func=_cmd_desktop_rotate)

    desktop_pool = desktop_sub.add_parser(
        "pool",
        help="Connect ChatGPT Desktop to the local account pool.",
    )
    desktop_pool_sub = desktop_pool.add_subparsers(
        dest="desktop_pool_command", required=require_command
    )
    desktop_pool_install = desktop_pool_sub.add_parser(
        "install", help="Install and enable the desktop pool launcher and service."
    )
    desktop_pool_install.add_argument("--pool-port", type=_positive_int, default=8765)
    desktop_pool_install.set_defaults(func=_cmd_desktop_pool_install)
    desktop_pool_disable = desktop_pool_sub.add_parser(
        "disable", help="Disable the desktop pool and restore the launcher."
    )
    desktop_pool_disable.set_defaults(func=_cmd_desktop_pool_disable)
    desktop_pool_status = desktop_pool_sub.add_parser(
        "status", help="Show desktop pool installation state."
    )
    desktop_pool_status.add_argument("--json", action="store_true")
    desktop_pool_status.set_defaults(func=_cmd_desktop_pool_status)

    pool = subparsers.add_parser(
        "pool",
        help="Run the local multi-account pool router.",
    )
    pool_sub = pool.add_subparsers(
        dest="pool_command",
        required=require_command,
    )
    pool_server = pool_sub.add_parser(
        "app-server",
        help="Run the pool as an app-server JSONL multiplexer.",
    )
    pool_server.add_argument(
        "--codex-bin",
        help="Codex executable used for isolated backend app-servers.",
    )
    pool_server.add_argument(
        "--pool-usage-timeout",
        type=_positive_float,
        default=5.0,
        metavar="SECONDS",
    )
    pool_server.add_argument(
        "--pool-usage-workers",
        type=_positive_int,
        default=4,
        metavar="COUNT",
    )
    pool_server.add_argument(
        "--pool-usage-cache-ttl",
        type=_nonnegative_float,
        default=60.0,
        metavar="SECONDS",
    )
    pool_server.add_argument(
        "--pool-refresh-usage",
        action="store_true",
    )
    pool_server.add_argument(
        "--pool-backend-timeout",
        type=_positive_float,
        default=10.0,
        metavar="SECONDS",
    )
    pool_server.add_argument(
        "--no-auto-refresh",
        dest="pool_auto_refresh",
        action="store_false",
        help="Do not renew expired access tokens while routing requests.",
    )
    pool_server.set_defaults(func=_cmd_pool_app_server)

    pool_responses = pool_sub.add_parser(
        "responses",
        help="Run a loopback Responses API pool proxy.",
    )
    pool_responses.add_argument("--pool-host", default="127.0.0.1")
    pool_responses.add_argument("--pool-port", type=_positive_int, default=8765)
    pool_responses.add_argument(
        "--pool-upstream-url",
        default="https://chatgpt.com/backend-api/codex/responses",
    )
    pool_responses.add_argument("--pool-token-file")
    pool_responses.add_argument(
        "--pool-usage-timeout", type=_positive_float, default=5.0, metavar="SECONDS"
    )
    pool_responses.add_argument(
        "--pool-usage-workers", type=_positive_int, default=4, metavar="COUNT"
    )
    pool_responses.add_argument(
        "--pool-usage-cache-ttl",
        type=_nonnegative_float,
        default=60.0,
        metavar="SECONDS",
    )
    pool_responses.add_argument("--pool-refresh-usage", action="store_true")
    pool_responses.add_argument("--pool-max-retries", type=_nonnegative_int, default=2)
    pool_responses.add_argument(
        "--pool-request-timeout", type=_positive_float, default=120.0, metavar="SECONDS"
    )
    pool_responses.add_argument(
        "--no-auto-refresh",
        dest="pool_auto_refresh",
        action="store_false",
        help="Do not renew expired access tokens while routing requests.",
    )
    pool_responses.set_defaults(func=_cmd_pool_responses)

    pool_configure = pool_sub.add_parser(
        "configure",
        help="Install the loopback pool as the active Codex custom provider.",
    )
    pool_configure.add_argument("--pool-port", type=_positive_int, default=8765)
    pool_configure.add_argument("--pool-provider-id", default=POOL_PROVIDER_ID)
    pool_configure.add_argument("--pool-env-key", default="AI_AUTH_SWITCH_POOL_TOKEN")
    pool_configure.add_argument(
        "--pool-token-file",
        default="~/.local/share/ai-auth-switch/pool/responses.token",
    )
    pool_configure.add_argument("--pool-no-backup", action="store_true")
    pool_configure.set_defaults(func=_cmd_pool_configure)

    pool_restore = pool_sub.add_parser(
        "restore",
        help="Restore a Codex config.toml from a pool-config backup.",
    )
    pool_restore.add_argument("pool_backup")
    pool_restore.set_defaults(func=_cmd_pool_restore)

    pool_status = pool_sub.add_parser(
        "status", help="Show persistent pool leases, routes, and health."
    )
    pool_status.add_argument("--json", action="store_true")
    pool_status.set_defaults(func=_cmd_pool_status)

    completion = subparsers.add_parser(
        "completion",
        help="Print a shell completion script for bash, zsh, or fish.",
    )
    completion.add_argument("shell", nargs=opt(None), choices=["bash", "zsh", "fish"])
    completion.set_defaults(func=_cmd_completion)

    hidden = subparsers.add_parser(
        "__complete",
        help=argparse.SUPPRESS,
        description="Internal command used by shell completion.",
    )
    hidden.add_argument("words", nargs=argparse.REMAINDER)
    hidden.set_defaults(func=_cmd_complete)

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

        raw_argv = _normalize_run_auto_options(raw_argv)
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


def pool_app_server_main() -> int:
    forwarded = list(sys.argv[1:])
    forwarded = [token for token in forwarded if token != "app-server"]
    ignored_flags = {"--analytics-default-enabled", "--stdio", "--strict-config"}
    ignored_with_value = {
        "--listen",
        "--code-mode-host",
        "--enable",
        "--disable",
        "-c",
        "--config",
    }
    filtered: list[str] = []
    index = 0
    while index < len(forwarded):
        token = forwarded[index]
        if token in ignored_flags:
            index += 1
            continue
        if token in ignored_with_value:
            index += 2
            continue
        filtered.append(token)
        index += 1
    return main(["pool", "app-server", *filtered], program_name="ai-auth-switch")


def pool_responses_main() -> int:
    return main(["pool", "responses", *sys.argv[1:]], program_name="ai-auth-switch")


if __name__ == "__main__":
    raise SystemExit(main())
