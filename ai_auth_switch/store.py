from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from ai_auth_switch.errors import AiAuthSwitchError
from ai_auth_switch.providers import Provider


SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._@+-]+")
RESERVED_NAMES = {"", ".", "..", "tmp", "backup", "current"}
NUMBERED_CODEX_ALIAS_RE = re.compile(r"^codex([1-9][0-9]*)$")


def default_store_dir() -> Path:
    configured = os.environ.get("AI_AUTH_SWITCH_HOME")
    if configured and configured.strip():
        return Path(configured).expanduser()

    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return Path(xdg).expanduser() / "ai-auth-switch"

    return Path.home() / ".local" / "share" / "ai-auth-switch"


def sanitize_profile_name(name: str) -> str:
    clean = SAFE_NAME_RE.sub("_", name.strip()).strip("_")
    if clean in RESERVED_NAMES:
        raise AiAuthSwitchError(f"invalid profile name: {name!r}")
    if "/" in clean or "\\" in clean:
        raise AiAuthSwitchError(f"invalid profile name: {name!r}")
    return clean


def numbered_codex_alias_index(name: str) -> int | None:
    match = NUMBERED_CODEX_ALIAS_RE.fullmatch(name)
    return int(match.group(1)) if match is not None else None


def set_private_permissions(path: Path) -> None:
    try:
        if path.is_dir():
            path.chmod(0o700)
        else:
            path.chmod(0o600)
    except OSError:
        pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(self.path.parent)
        self._file = self.path.open("a+")
        set_private_permissions(self.path)
        try:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._file is not None:
                try:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                self._file.close()
        finally:
            self._file = None


@dataclass(frozen=True)
class ProfileInfo:
    name: str
    path: Path
    active: bool = False
    by_content: bool = False


@dataclass(frozen=True)
class AliasInfo:
    name: str
    provider_id: str
    profile: str
    command: tuple[str, ...]


class AuthStore:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = (base_dir or default_store_dir()).expanduser()
        self.lock_path = self.base_dir / ".lock"

    def ensure(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        set_private_permissions(self.base_dir)

    def provider_dir(self, provider: Provider) -> Path:
        return self.base_dir / "profiles" / provider.id

    def backups_dir(self, provider: Provider) -> Path:
        return self.base_dir / "backups" / provider.id

    def aliases_path(self) -> Path:
        return self.base_dir / "aliases.json"

    def profile_path(self, provider: Provider, name: str) -> Path:
        clean = sanitize_profile_name(name)
        return self.provider_dir(provider) / f"{clean}.json"

    def lock(self) -> FileLock:
        self.ensure()
        return FileLock(self.lock_path)

    def profile_lock(self, provider: Provider, name: str) -> FileLock:
        """Return a lock for short credential updates to one profile.

        Codex wrappers use this only while installing or reconciling auth
        state. The lock is deliberately not held while the child runs, so
        multiple processes can use either the same or different accounts.
        """
        self.ensure()
        clean = sanitize_profile_name(name)
        digest = hashlib.sha256(
            f"{provider.id}\0{clean}".encode("utf-8")
        ).hexdigest()
        # Keep short update locks separate from the legacy lifetime-lock
        # namespace. Already-running wrappers from an older release may still
        # hold those old locks until their Codex child exits; reusing them would
        # defeat same-account concurrency during a live upgrade.
        path = (
            self.base_dir
            / "profile-update-locks"
            / provider.id
            / f"{digest}.lock"
        )
        return FileLock(path)

    def list_profiles(self, provider: Provider) -> list[ProfileInfo]:
        self.ensure()
        root = self.provider_dir(provider)
        root.mkdir(parents=True, exist_ok=True)
        set_private_permissions(root)

        active = self.current_profile(provider)
        profiles = []
        for path in sorted(root.glob("*.json")):
            name = path.stem
            profiles.append(
                ProfileInfo(
                    name=name,
                    path=path,
                    active=active is not None and active.name == name,
                    by_content=active is not None
                    and active.name == name
                    and active.by_content,
                )
            )
        return profiles

    def current_profile(self, provider: Provider) -> ProfileInfo | None:
        active = provider.active_auth_path
        if not active.exists() and not active.is_symlink():
            return None

        root = self.provider_dir(provider)
        try:
            resolved = active.resolve(strict=True)
        except OSError:
            resolved = None

        if resolved is not None and root.exists():
            try:
                if resolved.parent == root and resolved.suffix == ".json":
                    return ProfileInfo(name=resolved.stem, path=resolved, active=True)
            except OSError:
                pass

        if active.exists() and root.exists():
            try:
                active_hash = sha256_file(active)
            except OSError:
                return None
            for path in sorted(root.glob("*.json")):
                try:
                    if sha256_file(path) == active_hash:
                        return ProfileInfo(
                            name=path.stem,
                            path=path,
                            active=True,
                            by_content=True,
                        )
                except OSError:
                    continue
        identity_match = self._profile_matching_active_identity(provider, active)
        if identity_match is not None:
            return ProfileInfo(
                name=identity_match.stem,
                path=identity_match,
                active=True,
            )
        return None

    def save_current(self, provider: Provider, name: str | None = None) -> ProfileInfo:
        active = provider.active_auth_path
        if not active.exists():
            raise AiAuthSwitchError(f"active auth file not found: {active}")

        if not name:
            name = provider.infer_profile_name(active)
        if not name:
            raise AiAuthSwitchError("could not infer a profile name; pass one explicitly")

        path = self.profile_path(provider, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(path.parent)

        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
        shutil.copyfile(active, tmp, follow_symlinks=True)
        set_private_permissions(tmp)
        os.replace(tmp, path)
        set_private_permissions(path)
        self.activate(provider, path.stem, backup_existing=False)
        if provider.id == "codex":
            self.sync_numbered_aliases(provider)
        return ProfileInfo(name=path.stem, path=path, active=True)

    def activate(
        self,
        provider: Provider,
        name: str,
        *,
        backup_existing: bool = True,
    ) -> ProfileInfo:
        path = self.profile_path(provider, name)
        if not path.exists():
            raise AiAuthSwitchError(f"profile not found: {name}")

        active = provider.active_auth_path
        active.parent.mkdir(parents=True, exist_ok=True)
        set_private_permissions(active.parent)

        self._sync_replaced_active_back_to_profile(provider)

        if backup_existing:
            self._backup_active_if_needed(provider, replacing_with=path)

        tmp = active.with_name(f".{active.name}.tmp.{os.getpid()}.{time.time_ns()}")
        try:
            os.symlink(path, tmp)
        except OSError as exc:
            raise AiAuthSwitchError(f"failed to create auth symlink: {exc}") from exc
        os.replace(tmp, active)
        return ProfileInfo(name=path.stem, path=path, active=True)

    def remove(self, provider: Provider, name: str) -> None:
        path = self.profile_path(provider, name)
        if not path.exists():
            raise AiAuthSwitchError(f"profile not found: {name}")
        current = self.current_profile(provider)
        if current is not None and current.name == path.stem:
            raise AiAuthSwitchError(f"refusing to remove active profile: {name}")

        aliases = self._read_aliases()
        path.unlink()
        aliases = {
            alias_name: alias
            for alias_name, alias in aliases.items()
            if not (alias.provider_id == provider.id and alias.profile == path.stem)
        }
        self._write_aliases_if_changed(aliases)
        if provider.id == "codex":
            self.sync_numbered_aliases(provider)

    def rename(self, provider: Provider, old: str, new: str) -> ProfileInfo:
        old_path = self.profile_path(provider, old)
        new_path = self.profile_path(provider, new)
        if not old_path.exists():
            raise AiAuthSwitchError(f"profile not found: {old}")
        if new_path.exists():
            raise AiAuthSwitchError(f"profile already exists: {new}")

        aliases = self._read_aliases()
        current = self.current_profile(provider)
        os.replace(old_path, new_path)
        set_private_permissions(new_path)
        renamed_aliases = {
            alias_name: (
                AliasInfo(
                    name=alias.name,
                    provider_id=alias.provider_id,
                    profile=new_path.stem,
                    command=alias.command,
                )
                if alias.provider_id == provider.id and alias.profile == old_path.stem
                else alias
            )
            for alias_name, alias in aliases.items()
        }
        self._write_aliases_if_changed(renamed_aliases)
        if provider.id == "codex":
            self.sync_numbered_aliases(provider)
        if current is not None and current.name == old_path.stem:
            self.activate(provider, new_path.stem, backup_existing=False)
            return ProfileInfo(name=new_path.stem, path=new_path, active=True)
        return ProfileInfo(name=new_path.stem, path=new_path, active=False)

    def list_aliases(self) -> list[AliasInfo]:
        aliases = self._read_aliases()
        return sorted(aliases.values(), key=self._alias_sort_key)

    def sync_numbered_aliases(self, provider: Provider) -> list[AliasInfo]:
        """Keep codex1, codex2, ... contiguous and aligned with saved profiles."""
        if provider.id != "codex":
            return []

        aliases = self._read_aliases()
        profile_paths = self._profile_paths_in_initial_alias_order(provider)
        profile_names = {path.stem for path in profile_paths}

        numbered = sorted(
            (
                (index, alias)
                for alias in aliases.values()
                if (index := numbered_codex_alias_index(alias.name)) is not None
            ),
            key=lambda item: item[0],
        )

        ordered_profiles: list[tuple[str, tuple[str, ...]]] = []
        assigned_profiles: set[str] = set()
        for _, alias in numbered:
            if (
                alias.provider_id == provider.id
                and alias.profile in profile_names
                and alias.profile not in assigned_profiles
            ):
                ordered_profiles.append((alias.profile, alias.command))
                assigned_profiles.add(alias.profile)

        default_command = tuple(str(part) for part in provider.login_command)
        if not default_command and profile_paths:
            raise AiAuthSwitchError("automatic Codex alias command cannot be empty")
        for path in profile_paths:
            if path.stem not in assigned_profiles:
                ordered_profiles.append((path.stem, default_command))
                assigned_profiles.add(path.stem)

        updated = {
            name: alias
            for name, alias in aliases.items()
            if numbered_codex_alias_index(name) is None
        }
        automatic: list[AliasInfo] = []
        for index, (profile, command) in enumerate(ordered_profiles, start=1):
            name = f"codex{index}"
            alias = AliasInfo(
                name=name,
                provider_id=provider.id,
                profile=profile,
                command=command,
            )
            updated[name] = alias
            automatic.append(alias)

        self._write_aliases_if_changed(updated)
        return automatic

    def resolve_alias(self, name: str) -> AliasInfo | None:
        clean = sanitize_profile_name(name)
        return self._read_aliases().get(clean)

    def set_alias(
        self,
        provider: Provider,
        name: str,
        profile: str,
        command: list[str] | tuple[str, ...] | None = None,
    ) -> AliasInfo:
        clean_name = sanitize_profile_name(name)
        clean_profile = sanitize_profile_name(profile)
        profile_path = self.profile_path(provider, clean_profile)
        if not profile_path.exists():
            raise AiAuthSwitchError(f"profile not found: {profile}")

        normalized_command = tuple(str(part) for part in (command or provider.login_command))
        if not normalized_command:
            raise AiAuthSwitchError(f"alias command cannot be empty: {name}")

        aliases = self._read_aliases()
        aliases[clean_name] = AliasInfo(
            name=clean_name,
            provider_id=provider.id,
            profile=profile_path.stem,
            command=normalized_command,
        )
        self._write_aliases(aliases)
        return aliases[clean_name]

    def remove_alias(self, name: str) -> None:
        clean = sanitize_profile_name(name)
        aliases = self._read_aliases()
        if clean not in aliases:
            raise AiAuthSwitchError(f"alias not found: {name}")
        del aliases[clean]
        self._write_aliases(aliases)

    @staticmethod
    def _alias_sort_key(alias: AliasInfo) -> tuple[int, int | str]:
        index = numbered_codex_alias_index(alias.name)
        if index is not None:
            return (0, index)
        return (1, alias.name)

    def _profile_paths_in_initial_alias_order(self, provider: Provider) -> list[Path]:
        root = self.provider_dir(provider)
        if not root.exists():
            return []

        def saved_order(path: Path) -> tuple[int, str]:
            try:
                modified_ns = path.stat().st_mtime_ns
            except OSError:
                modified_ns = 0
            return (modified_ns, path.name)

        return sorted(root.glob("*.json"), key=saved_order)

    @contextlib.contextmanager
    def activated_temporarily(self, provider: Provider, name: str) -> Iterator[ProfileInfo]:
        active = provider.active_auth_path
        profile_path = self.profile_path(provider, name)
        backup = active.with_name(f".{active.name}.restore.{os.getpid()}.{time.time_ns()}")
        had_active = active.exists() or active.is_symlink()
        if had_active:
            os.replace(active, backup)
        try:
            info = self.activate(provider, name, backup_existing=False)
            yield info
        finally:
            self._sync_active_back_to_profile_if_replaced(active, profile_path)
            if active.exists() or active.is_symlink():
                active.unlink()
            if had_active:
                os.replace(backup, active)

    def _backup_active_if_needed(self, provider: Provider, replacing_with: Path) -> None:
        active = provider.active_auth_path
        if not active.exists():
            return

        try:
            active_resolved = active.resolve(strict=True)
            replace_resolved = replacing_with.resolve(strict=True)
            if active_resolved == replace_resolved:
                return
        except OSError:
            pass

        backup_root = self.backups_dir(provider)
        backup_root.mkdir(parents=True, exist_ok=True)
        set_private_permissions(backup_root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_root / f"auth-{stamp}-{time.time_ns()}.json"
        shutil.copyfile(active, backup, follow_symlinks=True)
        set_private_permissions(backup)

    def _sync_replaced_active_back_to_profile(self, provider: Provider) -> None:
        active = provider.active_auth_path
        if not active.exists() or active.is_symlink():
            return

        path = self._profile_matching_active_identity(provider, active)
        if path is None:
            return
        self._sync_active_back_to_profile_if_replaced(active, path)

    def _profile_matching_active_identity(self, provider: Provider, active: Path) -> Path | None:
        try:
            active_identity = provider.auth_identity(active)
        except OSError:
            return None
        if not active_identity:
            return None

        root = self.provider_dir(provider)
        if not root.exists():
            return None
        for path in sorted(root.glob("*.json")):
            try:
                if provider.auth_identity(path) == active_identity:
                    return path
            except OSError:
                continue
        return None

    def _sync_active_back_to_profile_if_replaced(self, active: Path, profile_path: Path) -> None:
        if not active.exists():
            return
        try:
            if active.resolve(strict=True) == profile_path.resolve(strict=True):
                return
        except OSError:
            pass

        tmp = profile_path.with_name(
            f".{profile_path.name}.refresh-sync.{os.getpid()}.{time.time_ns()}"
        )
        shutil.copyfile(active, tmp, follow_symlinks=True)
        set_private_permissions(tmp)
        os.replace(tmp, profile_path)
        set_private_permissions(profile_path)

    def _preserve_rejected_profile_auth(
        self,
        provider: Provider,
        name: str,
        active: Path,
    ) -> Path:
        backup_root = self.backups_dir(provider) / "rejected"
        backup_root.mkdir(parents=True, exist_ok=True)
        set_private_permissions(backup_root)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = backup_root / (
            f"{sanitize_profile_name(name)}-{stamp}-{time.time_ns()}.json"
        )
        shutil.copyfile(active, backup, follow_symlinks=True)
        set_private_permissions(backup)
        return backup

    def sync_profile_auth(
        self,
        provider: Provider,
        name: str,
        active: Path,
        *,
        expected_identity: str | None = None,
        initial_digest: str | None = None,
    ) -> None:
        """Reconcile profile-scoped auth state without crossing accounts."""
        if not active.exists():
            return

        profile_path = self.profile_path(provider, name)
        try:
            candidate_digest = sha256_file(active)
        except OSError:
            return

        # An unchanged detached credential must never replace a profile that a
        # concurrent process refreshed while this process was running.
        if initial_digest is not None and candidate_digest == initial_digest:
            return

        current_digest: str | None = None
        if profile_path.exists():
            try:
                current_digest = sha256_file(profile_path)
            except OSError:
                current_digest = None
        if current_digest == candidate_digest:
            return

        try:
            candidate_identity = provider.auth_identity(active)
        except OSError:
            candidate_identity = None
        try:
            current_identity = (
                provider.auth_identity(profile_path) if profile_path.exists() else None
            )
        except OSError:
            current_identity = None

        required_identities = {
            identity
            for identity in (expected_identity, current_identity)
            if identity is not None
        }
        if required_identities and (
            candidate_identity is None
            or any(candidate_identity != identity for identity in required_identities)
        ):
            rejected = self._preserve_rejected_profile_auth(
                provider,
                name,
                active,
            )
            found = candidate_identity or "unknown"
            expected = ", ".join(sorted(required_identities))
            raise AiAuthSwitchError(
                f"refusing to overwrite profile {name!r}: auth identity changed "
                f"(expected [{expected}], candidate {found!r}); preserved candidate "
                f"at {rejected}"
            )

        self._sync_active_back_to_profile_if_replaced(active, profile_path)

    def _read_aliases(self) -> dict[str, AliasInfo]:
        path = self.aliases_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise AiAuthSwitchError(f"failed to read aliases: {path}: {exc}") from exc

        raw_aliases = data.get("aliases") if isinstance(data, dict) else None
        if not isinstance(raw_aliases, dict):
            return {}

        aliases: dict[str, AliasInfo] = {}
        for raw_name, raw_info in raw_aliases.items():
            if not isinstance(raw_name, str) or not isinstance(raw_info, dict):
                continue
            provider_id = raw_info.get("provider")
            profile = raw_info.get("profile")
            command = raw_info.get("command")
            if (
                not isinstance(provider_id, str)
                or not provider_id.strip()
                or not isinstance(profile, str)
                or not profile.strip()
            ):
                continue
            if not isinstance(command, list) or not all(
                isinstance(part, str) and part for part in command
            ):
                continue
            aliases[sanitize_profile_name(raw_name)] = AliasInfo(
                name=sanitize_profile_name(raw_name),
                provider_id=provider_id.strip(),
                profile=sanitize_profile_name(profile),
                command=tuple(command),
            )
        return aliases

    def _write_aliases(self, aliases: dict[str, AliasInfo]) -> None:
        self.ensure()
        path = self.aliases_path()
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{time.time_ns()}")
        payload = {
            "version": 1,
            "aliases": {
                alias.name: {
                    "provider": alias.provider_id,
                    "profile": alias.profile,
                    "command": list(alias.command),
                }
                for alias in sorted(aliases.values(), key=lambda item: item.name)
            },
        }
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        set_private_permissions(tmp)
        os.replace(tmp, path)
        set_private_permissions(path)

    def _write_aliases_if_changed(self, aliases: dict[str, AliasInfo]) -> None:
        if aliases != self._read_aliases():
            self._write_aliases(aliases)
