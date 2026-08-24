from __future__ import annotations

import argparse
import contextlib
import io
from pathlib import Path

from ai_auth_switch.store import AuthStore


def _store_from_ns(ns: argparse.Namespace) -> AuthStore:
    store_dir = getattr(ns, "store_dir", None)
    return AuthStore(Path(store_dir).expanduser() if store_dir else None)


def _provider_from_ns(provider_id: str, ns: argparse.Namespace):
    from ai_auth_switch.providers import get_provider

    codex_home = getattr(ns, "codex_home", None)
    claude_config_dir = getattr(ns, "claude_config_dir", None)
    return get_provider(
        provider_id,
        codex_home=Path(codex_home).expanduser() if codex_home else None,
        claude_config_dir=(
            Path(claude_config_dir).expanduser() if claude_config_dir else None
        ),
    )


def _profile_names(provider_id: str, ns: argparse.Namespace) -> list[str]:
    store = _store_from_ns(ns)
    try:
        provider = _provider_from_ns(provider_id, ns)
    except Exception:
        return []
    try:
        return [profile.name for profile in store.list_profiles(provider)]
    except Exception:
        return []


def _alias_names(ns: argparse.Namespace) -> list[str]:
    try:
        return [alias.name for alias in _store_from_ns(ns).list_aliases()]
    except Exception:
        return []


def _subparsers_action(
    parser: argparse.ArgumentParser,
) -> argparse._SubParsersAction | None:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _command_path(ns: argparse.Namespace) -> tuple[str, ...]:
    parts = []
    command_name = getattr(ns, "command_name", None)
    if command_name:
        parts.append(command_name)
        if command_name == "auth" and getattr(ns, "auth_command", None):
            parts.append(ns.auth_command)
        if command_name == "alias" and getattr(ns, "alias_command", None):
            parts.append(ns.alias_command)
        if command_name == "desktop" and getattr(ns, "desktop_command", None):
            parts.append(ns.desktop_command)
            if ns.desktop_command == "auto" and getattr(
                ns, "desktop_auto_command", None
            ):
                parts.append(ns.desktop_auto_command)
            if ns.desktop_command == "pool" and getattr(
                ns, "desktop_pool_command", None
            ):
                parts.append(ns.desktop_pool_command)
        if command_name == "pool" and getattr(ns, "pool_command", None):
            parts.append(ns.pool_command)
    return tuple(parts)


def _option_strings(parser: argparse.ArgumentParser) -> list[str]:
    return [option for action in parser._actions for option in action.option_strings]


def _positional_actions(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    return [
        action
        for action in parser._actions
        if not action.option_strings
        and not isinstance(action, argparse._SubParsersAction)
    ]


def _next_positional(
    parser: argparse.ArgumentParser,
    ns: argparse.Namespace,
) -> argparse.Action | None:
    for action in _positional_actions(parser):
        if getattr(ns, action.dest, None) is None:
            return action
    return None


def _is_remainder(action: argparse.Action) -> bool:
    return action.nargs in (argparse.REMAINDER, argparse.PARSER)


def _filter_used(candidates: list[str], consumed: list[str]) -> list[str]:
    used = {word for word in consumed if word.startswith("-")}
    return [candidate for candidate in candidates if candidate not in used]


def _dynamic_candidates(
    ns: argparse.Namespace,
    dest: str,
) -> list[str] | None:
    path = _command_path(ns)
    provider = getattr(ns, "provider", None)
    if path[0:1] == ("run",) and dest == "name" and provider:
        return _profile_names(provider, ns)
    if (
        path[0:1] == ("auth",)
        and path[1:2]
        in (
            ("use",),
            ("remove",),
            ("default",),
            ("bind",),
        )
        and dest == "name"
        and provider
    ):
        return _profile_names(provider, ns)
    if path == ("auth", "rename") and dest in ("old", "new") and provider:
        return _profile_names(provider, ns)
    if path in (("alias", "run"), ("alias", "remove"), ("alias", "install")):
        if dest == "name":
            return _alias_names(ns)
    if path == ("alias", "set"):
        if dest == "name":
            return _alias_names(ns)
        if dest == "profile" and provider:
            return _profile_names(provider, ns)
    return None


def _candidates_for(
    parser: argparse.ArgumentParser,
    ns: argparse.Namespace,
    consumed: list[str],
) -> list[str]:
    root = parser
    current = parser
    # Descend through every nested subparser level (e.g. ``auth`` → ``use``).
    while True:
        suba = _subparsers_action(current)
        if suba is None:
            break
        selected = getattr(ns, suba.dest, None)
        if selected is None:
            candidates = list(suba.choices.keys())
            candidates.extend(_option_strings(current))
            candidates.extend(_option_strings(root))
            return _filter_used(candidates, consumed)
        current = suba.choices[selected]

    candidates: list[str] = []
    candidates.extend(_option_strings(current))
    candidates.extend(_option_strings(root))

    action = _next_positional(current, ns)
    if action is not None and not _is_remainder(action):
        if action.choices:
            candidates.extend(str(choice) for choice in action.choices)
        else:
            dynamic = _dynamic_candidates(ns, action.dest)
            if dynamic is not None:
                candidates.extend(dynamic)
    return _filter_used(candidates, consumed)


def complete_words(words: list[str]) -> list[str]:
    """Return completion candidates for the tokens after the program name.

    The last token in *words* is the (possibly partial) word being completed.
    """
    from ai_auth_switch.cli import CliUsageError, build_parser

    words = list(words)
    prefix = words[-1] if words else ""
    consumed = words[:-1] if words else []
    if "--" in consumed:
        return []

    # Subcommands and leaf positionals are optional while completing, so
    # parse_known_args succeeds on partial input such as ``auth`` or
    # ``auth use codex`` and the candidate walker can suggest the next token.
    parser = build_parser(require_command=False, completion=True)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            ns, _unknown = parser.parse_known_args(consumed)
    except (SystemExit, argparse.ArgumentError, CliUsageError):
        return []
    candidates = _candidates_for(parser, ns, consumed)
    return sorted(
        {candidate for candidate in candidates if candidate.startswith(prefix)}
    )


def bash_completion_script() -> str:
    return """# bash completion for ai-auth-switch / ais
# source this file, or add:  eval "$(ai-auth-switch completion bash)"
_ai_auth_switch_complete() {
    local cur
    cur="${COMP_WORDS[COMP_CWORD]}"
    COMPREPLY=( $(ai-auth-switch __complete "${COMP_WORDS[@]:1}") )
}
complete -o default -F _ai_auth_switch_complete ai-auth-switch ais
"""


def zsh_completion_script() -> str:
    return """#compdef ai-auth-switch ais
# source this file, or add:  eval "$(ai-auth-switch completion zsh)"
_ai_auth_switch() {
    local -a completions
    completions=("${(@f)$(ai-auth-switch __complete "${words[@]:2}")}")
    _describe 'ai-auth-switch' completions
}
compdef _ai_auth_switch ai-auth-switch ais
"""


def fish_completion_script() -> str:
    return """# fish completion for ai-auth-switch / ais
# source this file, or add:  ai-auth-switch completion fish | source
complete -c ai-auth-switch -f -a '(ai-auth-switch __complete (commandline -opc)[2..-1])'
complete -c ais -f -a '(ai-auth-switch __complete (commandline -opc)[2..-1])'
"""
