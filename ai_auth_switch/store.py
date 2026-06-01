from __future__ import annotations

import contextlib
import hashlib
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

    def profile_path(self, provider: Provider, name: str) -> Path:
        clean = sanitize_profile_name(name)
        return self.provider_dir(provider) / f"{clean}.json"

    def lock(self) -> FileLock:
        self.ensure()
        return FileLock(self.lock_path)

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
        path.unlink()

    def rename(self, provider: Provider, old: str, new: str) -> ProfileInfo:
        old_path = self.profile_path(provider, old)
        new_path = self.profile_path(provider, new)
        if not old_path.exists():
            raise AiAuthSwitchError(f"profile not found: {old}")
        if new_path.exists():
            raise AiAuthSwitchError(f"profile already exists: {new}")

        current = self.current_profile(provider)
        os.replace(old_path, new_path)
        set_private_permissions(new_path)
        if current is not None and current.name == old_path.stem:
            self.activate(provider, new_path.stem, backup_existing=False)
            return ProfileInfo(name=new_path.stem, path=new_path, active=True)
        return ProfileInfo(name=new_path.stem, path=new_path, active=False)

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
