"""Shared installation and storage configuration for pyOS.

The module deliberately keeps standalone state below one application-owned
directory.  JSON persistence is atomic and guarded by an inter-process lock so
the GUI, CLI, and Setup cannot observe half-written state or trample a locked
read/modify/write operation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath


class ConfigurationError(RuntimeError):
    """Raised when an existing pyOS configuration cannot be trusted."""


class StorageOwnershipError(RuntimeError):
    """Raised when a storage ownership marker is missing or invalid."""


class JSONPersistenceError(RuntimeError):
    """Raised when a locked JSON file cannot be read or validated."""


_ACTIVE_USERNAME = None
_ACTIVE_PROFILE_ID = None
_THREAD_LOCKS = {}
_THREAD_LOCKS_GUARD = threading.Lock()

OWNER_FILENAME = ".pyos-owner.json"
OWNER_APPLICATION = "pyOS"
OWNER_VERSION = 2
LEGACY_OWNER_VERSIONS = {1}
OWNED_FILE = "file"
OWNED_TREE = "tree"


@dataclass(frozen=True)
class OwnedStoragePath:
    """A validated storage-manifest entry with its recorded deletion type."""

    path: Path
    kind: str

LEGACY_HOME = Path.home()
# Explicit storage overrides are commonly used for portable/test instances.  Do
# not merge the real user's legacy state into those isolated roots unless the
# caller deliberately opts in.
MIGRATE_LEGACY_STATE = (
    os.environ.get("PYOS_MIGRATE_LEGACY_STATE") == "1"
    or ("PYOS_HOME" not in os.environ and "PYOS_CONFIG_FILE" not in os.environ)
)
STANDALONE_ROOT = Path(
    os.environ.get("PYOS_HOME", LEGACY_HOME / ".pyos")
).expanduser()
CONFIG_FILE = Path(
    os.environ.get("PYOS_CONFIG_FILE", STANDALONE_ROOT / "install.json")
).expanduser()
LEGACY_CONFIG_FILE = LEGACY_HOME / ".pyos_install.json"


def _thread_lock_for(path):
    try:
        key = str(Path(path).expanduser().resolve(strict=False)).casefold()
    except OSError:
        key = str(Path(path).expanduser().absolute()).casefold()
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _path_lock(path, timeout=15.0):
    """Hold a thread- and process-wide advisory lock for *path*."""
    target = Path(path).expanduser()
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(lock_path)
    with thread_lock:
        handle = open(lock_path, "a+b")
        acquired = False
        started = time.monotonic()
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while not acquired:
                    try:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        acquired = True
                    except OSError:
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError(f"Timed out waiting for {lock_path}")
                        time.sleep(0.025)
            else:
                import fcntl

                while not acquired:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                    except BlockingIOError:
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError(f"Timed out waiting for {lock_path}")
                        time.sleep(0.025)
            yield
        finally:
            if acquired:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def _atomic_write_bytes_unlocked(path, payload, mode=None):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        if os.name != "nt":
            try:
                directory = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _json_bytes(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _atomic_write_json_unlocked(path, data, mode=None):
    _atomic_write_bytes_unlocked(path, _json_bytes(data), mode=mode)


def atomic_write_json(path, data, *, mode=None, backup=False):
    """Atomically write JSON while holding a cross-process lock.

    When ``backup`` is true, a parseable current value is retained at
    ``<filename>.bak`` before replacement.  Domain-specific stores additionally
    validate their backup before relying on it.
    """
    path = Path(path).expanduser()
    with _path_lock(path):
        if backup and path.exists():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
                raise JSONPersistenceError(f"Existing JSON is invalid: {path}") from error
            _atomic_write_json_unlocked(path.with_name(path.name + ".bak"), previous, mode=mode)
        elif backup and not path.with_name(path.name + ".bak").exists():
            _atomic_write_json_unlocked(path.with_name(path.name + ".bak"), data, mode=mode)
        _atomic_write_json_unlocked(path, data, mode=mode)


def _apply_validator(value, validator, path):
    if validator is None:
        return value
    try:
        result = validator(value)
    except Exception as error:
        raise JSONPersistenceError(f"JSON validation failed: {path}") from error
    if result is False or result is None:
        raise JSONPersistenceError(f"JSON validation failed: {path}")
    return value if result is True else result


def update_json_file(path, updater, *, default, validator=None, mode=None, backup=False):
    """Perform a locked, atomic JSON read/modify/write operation."""
    path = Path(path).expanduser()
    backup_path = path.with_name(path.name + ".bak")
    with _path_lock(path):
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current = _apply_validator(current, validator, path)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, JSONPersistenceError) as error:
                if not backup or not backup_path.exists():
                    raise JSONPersistenceError(f"Existing JSON is invalid: {path}") from error
                try:
                    current = json.loads(backup_path.read_text(encoding="utf-8"))
                    current = _apply_validator(current, validator, backup_path)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, JSONPersistenceError) as backup_error:
                    raise JSONPersistenceError(
                        f"JSON and its backup are invalid: {path}"
                    ) from backup_error
                _atomic_write_json_unlocked(path, current, mode=mode)
        else:
            current = copy.deepcopy(default)
        replacement = updater(copy.deepcopy(current)) if callable(updater) else updater
        replacement = _apply_validator(replacement, validator, path)
        if backup:
            _atomic_write_json_unlocked(backup_path, current, mode=mode)
        _atomic_write_json_unlocked(path, replacement, mode=mode)
        return replacement


def _normalise_relative_path(path):
    candidate = Path(path)
    if candidate.is_absolute() or not candidate.parts:
        raise ValueError("Owned paths must be relative to their storage root.")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Owned paths must stay inside their storage root.")
    return Path(*parts).as_posix()


def _approved_owned_tree(relative_path):
    """Return whether *relative_path* is an explicitly approved data subtree."""
    normalized = _normalise_relative_path(relative_path)
    parts = PurePath(normalized).parts
    if normalized in {"Drive_B", "apps", "pending_updates"}:
        return True
    if len(parts) not in {2, 3} or parts[0] != "profiles":
        return False
    profile_id = parts[1]
    if not (1 <= len(profile_id) <= 64) or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in profile_id):
        return False
    return len(parts) == 2 or parts[2] in {"Drive_B", "apps"}


def _validated_owner(data, *, kind=None):
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if (data.get("application") != OWNER_APPLICATION or
            version not in LEGACY_OWNER_VERSIONS | {OWNER_VERSION}):
        return None
    token = data.get("token")
    owner_kind = data.get("kind")
    if not isinstance(token, str) or len(token) != 64:
        return None
    try:
        bytes.fromhex(token)
    except ValueError:
        return None
    if not isinstance(owner_kind, str) or not owner_kind or (kind and owner_kind != kind):
        return None
    raw_paths = data.get("owned_paths", [])
    if not isinstance(raw_paths, list):
        return None
    try:
        paths = {_normalise_relative_path(item) for item in raw_paths}
    except (TypeError, ValueError):
        return None
    # Advisory lock files are pyOS-created artifacts too.  Listing the marker's
    # lock guarantees a manifest-only uninstall can leave the root empty.
    paths.add(OWNER_FILENAME + ".lock")
    raw_types = data.get("owned_path_types")
    if version in LEGACY_OWNER_VERSIONS:
        # Version 1 never recorded deletion types.  Treat every entry as a file
        # rather than inferring recursive authority from what currently exists.
        path_types = {value: OWNED_FILE for value in paths}
    elif not isinstance(raw_types, dict):
        return None
    else:
        path_types = {}
        try:
            for value, entry_kind in raw_types.items():
                normalized = _normalise_relative_path(value)
                if (normalized not in paths or
                        entry_kind not in {OWNED_FILE, OWNED_TREE} or
                        (entry_kind == OWNED_TREE and not _approved_owned_tree(normalized))):
                    return None
                path_types[normalized] = entry_kind
        except (TypeError, ValueError):
            return None
        # The marker lock was implicit in schema 1 and may be omitted by an
        # early schema-2 writer, but every other entry must be explicitly typed.
        path_types.setdefault(OWNER_FILENAME + ".lock", OWNED_FILE)
        if set(path_types) != paths:
            return None
    if path_types[OWNER_FILENAME + ".lock"] != OWNED_FILE:
        return None
    return {
        "application": OWNER_APPLICATION,
        "version": OWNER_VERSION,
        "kind": owner_kind,
        "token": token,
        "owned_paths": sorted(paths),
        "owned_path_types": {
            value: path_types[value] for value in sorted(paths)
        },
    }


def ensure_storage_owner(path=None, *, kind="data", require_empty_new=False):
    """Create or validate the ownership marker for a pyOS storage root."""
    root = Path(path if path is not None else STANDALONE_ROOT).expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / OWNER_FILENAME
    with _path_lock(marker):
        if marker.exists():
            try:
                data = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
                raise StorageOwnershipError(f"Invalid pyOS ownership marker: {marker}") from error
            validated = _validated_owner(data, kind=kind)
            if validated is None:
                raise StorageOwnershipError(f"Invalid pyOS ownership marker: {marker}")
            if validated != data:
                _atomic_write_json_unlocked(marker, validated, mode=0o600)
        else:
            if require_empty_new:
                allowed = {marker.name + ".lock"}
                try:
                    unexpected = [child for child in root.iterdir() if child.name not in allowed]
                except OSError as error:
                    raise StorageOwnershipError(
                        f"Unable to inspect prospective pyOS storage: {root}"
                    ) from error
                if unexpected:
                    raise StorageOwnershipError(
                        f"Refusing to claim non-empty pyOS storage without a marker: {root}"
                    )
            validated = {
                "application": OWNER_APPLICATION,
                "version": OWNER_VERSION,
                "kind": kind,
                "token": secrets.token_hex(32),
                "owned_paths": [OWNER_FILENAME + ".lock"],
                "owned_path_types": {OWNER_FILENAME + ".lock": OWNED_FILE},
            }
            _atomic_write_json_unlocked(marker, validated, mode=0o600)
    return root


def verify_storage_owner(path, *, kind=None):
    """Return true only for a root with a valid pyOS ownership marker."""
    root = Path(path).expanduser().resolve(strict=False)
    marker = root / OWNER_FILENAME
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return _validated_owner(data, kind=kind) is not None
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return False


def register_owned_path(path, root=None, *, kind=OWNED_FILE):
    """Register a typed descendant in an owned storage manifest.

    Recursive ownership requires an explicit ``kind=OWNED_TREE`` call and is
    restricted to application-defined subtree names.  Omitting ``kind`` always
    records a file, including during migration from the legacy untyped schema.
    """
    if kind not in {OWNED_FILE, OWNED_TREE}:
        raise ValueError("Owned storage paths must be registered as a file or tree.")
    storage_root = Path(root if root is not None else get_data_dir()).expanduser().resolve(strict=False)
    target = Path(path).expanduser().resolve(strict=False)
    try:
        relative = target.relative_to(storage_root)
    except ValueError as error:
        raise StorageOwnershipError(f"Path is outside pyOS storage: {target}") from error
    relative_text = _normalise_relative_path(relative)
    if kind == OWNED_TREE and not _approved_owned_tree(relative_text):
        raise StorageOwnershipError(
            f"Refusing to register an unapproved recursive storage tree: {relative_text}"
        )
    marker = storage_root / OWNER_FILENAME
    with _path_lock(marker):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
            raise StorageOwnershipError(f"Missing or invalid pyOS ownership marker: {marker}") from error
        validated = _validated_owner(data)
        if validated is None:
            raise StorageOwnershipError(f"Missing or invalid pyOS ownership marker: {marker}")
        additions = {
            relative_text: kind,
            relative_text + ".lock": OWNED_FILE,
            relative_text + ".bak": OWNED_FILE,
            relative_text + ".bak.lock": OWNED_FILE,
        }
        path_types = dict(validated["owned_path_types"])
        existing_kind = path_types.get(relative_text)
        if existing_kind == OWNED_TREE and kind != OWNED_TREE:
            raise StorageOwnershipError(
                f"Owned storage tree cannot be re-registered as a file: {relative_text}"
            )
        changed = False
        for value, entry_kind in additions.items():
            if path_types.get(value) != entry_kind:
                path_types[value] = entry_kind
                changed = True
        if changed:
            validated["owned_paths"] = sorted(path_types)
            validated["owned_path_types"] = {
                value: path_types[value] for value in sorted(path_types)
            }
            _atomic_write_json_unlocked(marker, validated, mode=0o600)
    return target


def owned_path_entries(root=None, *, kind=None):
    """Return validated typed entries for safe storage cleanup."""
    storage_root = Path(root if root is not None else get_data_dir(create=False)).expanduser().resolve(strict=False)
    marker = storage_root / OWNER_FILENAME
    try:
        data = _validated_owner(json.loads(marker.read_text(encoding="utf-8")), kind=kind)
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        data = None
    if data is None:
        raise StorageOwnershipError(f"Missing or invalid pyOS ownership marker: {marker}")
    results = []
    for item in data["owned_paths"]:
        target = storage_root / item
        if not target.parent.resolve(strict=False).is_relative_to(storage_root):
            raise StorageOwnershipError(f"Owned path escapes pyOS storage: {item}")
        results.append(OwnedStoragePath(target, data["owned_path_types"][item]))
    return tuple(results)


def owned_paths(root=None):
    """Return paths from the validated manifest (not deletion authority)."""
    return tuple(entry.path for entry in owned_path_entries(root))


def remove_owned_storage_paths(root=None, *, preserve=(), kind=None):
    """Remove typed manifest entries without deleting the owner marker or root.

    A file/tree substitution, link, junction, path escape, or unapproved tree
    causes a fail-closed error before that entry is removed recursively.
    Unknown entries outside the manifest are never touched.
    """
    storage_root = Path(root if root is not None else get_data_dir(create=False)).expanduser().resolve(strict=False)

    def identity(value):
        return os.path.normcase(os.path.abspath(os.fspath(Path(value).expanduser())))

    preserved = {identity(value) for value in preserve}
    entries = sorted(
        owned_path_entries(storage_root, kind=kind),
        key=lambda entry: len(entry.path.parts),
        reverse=True,
    )
    removed = []
    for entry in entries:
        target = entry.path
        if identity(target) in preserved:
            continue
        if not target.parent.resolve(strict=False).is_relative_to(storage_root):
            raise StorageOwnershipError(f"Owned path escapes pyOS storage: {target}")
        if target.is_symlink() or (hasattr(target, "is_junction") and target.is_junction()):
            raise StorageOwnershipError(f"Owned storage path was replaced by a link: {target}")
        if entry.kind == OWNED_TREE:
            if target.is_file():
                raise StorageOwnershipError(
                    f"Owned storage tree was replaced by a file: {target}"
                )
            if target.is_dir():
                shutil.rmtree(target)
                removed.append(target)
            elif target.exists():
                raise StorageOwnershipError(f"Owned storage tree has an invalid type: {target}")
        elif entry.kind == OWNED_FILE:
            if target.is_dir():
                raise StorageOwnershipError(
                    f"Owned storage file was replaced by a directory: {target}"
                )
            if target.is_file():
                target.unlink()
                removed.append(target)
            elif target.exists():
                raise StorageOwnershipError(f"Owned storage file has an invalid type: {target}")
        else:  # Defensive: validated manifests cannot reach this branch.
            raise StorageOwnershipError(f"Invalid owned storage type: {entry.kind}")
    return tuple(removed)


def get_standalone_root(create=False):
    """Return the dedicated root used before Setup configures another data path."""
    root = Path(STANDALONE_ROOT).expanduser().resolve(strict=False)
    if create:
        ensure_storage_owner(root, kind="standalone")
        if Path(CONFIG_FILE).expanduser().resolve(strict=False).is_relative_to(root):
            register_owned_path(CONFIG_FILE, root)
    return root


def _default_config():
    root = get_standalone_root(create=False)
    return {
        "install_dir": str(Path(__file__).resolve().parent),
        "data_dir": str(root),
        "downloads_dir": str(LEGACY_HOME / "Downloads"),
        "drive_b_dir": str(root / "Drive_B"),
        "enabled_apps": None,
        "configured": False,
    }


def _validated_config(raw):
    if not isinstance(raw, dict):
        return None
    # Defaults describe the genuinely missing, standalone first-run state.  An
    # on-disk configuration is different: once a config file exists, omitted
    # fields must not silently redirect storage to newly inferred locations.
    # "enabled_apps" is intentionally not required: it was added after the first
    # installer version, and a config written before it (installer_version 1)
    # must stay valid.  A missing value means "all apps enabled" everywhere it is
    # read (see DesktopGUI._app_enabled), so its absence is safe.
    required = {
        "install_dir", "data_dir", "downloads_dir", "drive_b_dir",
        "configured",
    }
    if not required.issubset(raw):
        return None
    result = dict(raw)
    for key in ("install_dir", "data_dir", "downloads_dir", "drive_b_dir"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            return None
    if not isinstance(result.get("configured"), bool):
        return None
    enabled = result.get("enabled_apps")
    if enabled is not None and (
            not isinstance(enabled, list) or any(not isinstance(item, str) for item in enabled)):
        return None
    if isinstance(enabled, list):
        result["enabled_apps"] = list(dict.fromkeys(enabled))
    if not result["configured"]:
        root = get_standalone_root(create=False)
        result["data_dir"] = str(root)
        result["drive_b_dir"] = str(root / "Drive_B")
    try:
        json.dumps(result)
    except (TypeError, ValueError):
        return None
    return result


def _read_valid_config(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        return None
    return _validated_config(raw)


def _save_config_unlocked(config, current=None):
    validated = _validated_config(config)
    if validated is None:
        raise ConfigurationError("Refusing to save invalid pyOS configuration.")
    path = Path(CONFIG_FILE).expanduser()
    backup = path.with_name(path.name + ".bak")
    if current is not None:
        _atomic_write_json_unlocked(backup, current, mode=0o600)
    elif not backup.exists():
        _atomic_write_json_unlocked(backup, validated, mode=0o600)
    _atomic_write_json_unlocked(path, validated, mode=0o600)
    return validated


def _prepare_config_storage(config):
    """Validate/create every ownership marker before config persistence.

    Callers hold the configuration lock while invoking this helper.  Keeping
    ownership preparation ahead of both the backup and primary writes means an
    invalid/unwritable storage root cannot leave a newly committed config that
    points at storage pyOS does not own.
    """
    validated = _validated_config(config)
    if validated is None:
        raise ConfigurationError("Refusing to save invalid pyOS configuration.")

    config_path = Path(CONFIG_FILE).expanduser().resolve(strict=False)
    standalone = get_standalone_root(create=True)
    data_root = Path(validated["data_dir"]).expanduser().resolve(strict=False)
    kind = "standalone" if data_root == standalone else "data"
    ensure_storage_owner(data_root, kind=kind)
    if config_path.is_relative_to(data_root):
        register_owned_path(config_path, data_root)
    return validated


def _copy_legacy_file(source, target):
    source, target = Path(source), Path(target)
    if target.exists() or not source.is_file():
        return False
    with _path_lock(target):
        if target.exists():
            return False
        _atomic_write_bytes_unlocked(target, source.read_bytes(), mode=0o600)
    return True


def _migrate_legacy_standalone_artifacts():
    root = get_standalone_root(create=True)
    if not MIGRATE_LEGACY_STATE:
        return
    legacy_anchor = any((LEGACY_HOME / name).exists() for name in (
        ".pyos_gui_settings.json", ".pyOS_settings.json", ".pyos_credentials.json"
    ))
    mappings = {
        ".pyos_gui_settings.json": "gui_settings.json",
        ".pyOS_settings.json": "cli_settings.json",
        ".pyos_credentials.json": "credentials.json",
        ".pyos_credentials.json.bak": "credentials.json.bak",
        "remembered_session.json": "remembered_session.json",
        "virtual_drives.json": "virtual_drives.json",
        "email_settings.json": "email_settings.json",
    }
    for legacy_name, new_name in mappings.items():
        if legacy_name.startswith(".") or legacy_anchor:
            target = root / new_name
            try:
                if _copy_legacy_file(LEGACY_HOME / legacy_name, target) or target.exists():
                    register_owned_path(target, root)
            except OSError:
                pass
    # A former ~/apps directory was generic.  Copy only files that look like
    # pyOS App Maker apps, and never delete or claim the source directory.
    legacy_apps = LEGACY_HOME / "apps"
    target_apps = root / "apps"
    if legacy_anchor and legacy_apps.is_dir():
        for source in legacy_apps.glob("*.py"):
            try:
                text = source.read_text(encoding="utf-8")
                if "def build(" not in text:
                    continue
                target_apps.mkdir(parents=True, exist_ok=True)
                _copy_legacy_file(source, target_apps / source.name)
            except (OSError, UnicodeError):
                continue
        if target_apps.exists():
            register_owned_path(target_apps, root, kind=OWNED_TREE)


def load_config():
    """Return validated setup configuration.

    A genuinely missing configuration is standalone first-run state.  An
    existing malformed configuration is recovered only from a validated backup;
    otherwise :class:`ConfigurationError` is raised rather than silently
    changing storage locations.
    """
    path = Path(CONFIG_FILE).expanduser()
    backup = path.with_name(path.name + ".bak")
    with _path_lock(path):
        if not path.exists():
            recovered = _read_valid_config(backup) if backup.exists() else None
            if recovered is not None:
                _prepare_config_storage(recovered)
                _atomic_write_json_unlocked(path, recovered, mode=0o600)
                config = recovered
            elif backup.exists():
                raise ConfigurationError(f"Invalid pyOS configuration backup: {backup}")
            elif MIGRATE_LEGACY_STATE \
                    and path.resolve(strict=False) == (get_standalone_root() / "install.json").resolve(strict=False) \
                    and (LEGACY_CONFIG_FILE.exists() or
                         LEGACY_CONFIG_FILE.with_name(LEGACY_CONFIG_FILE.name + ".bak").exists()):
                legacy = _read_valid_config(LEGACY_CONFIG_FILE)
                if legacy is None:
                    legacy = _read_valid_config(
                        LEGACY_CONFIG_FILE.with_name(LEGACY_CONFIG_FILE.name + ".bak")
                    )
                if legacy is None:
                    raise ConfigurationError(
                        f"Legacy pyOS configuration and backup are malformed: {LEGACY_CONFIG_FILE}"
                    )
                _prepare_config_storage(legacy)
                config = _save_config_unlocked(legacy)
            else:
                config = _default_config()
        else:
            config = _read_valid_config(path)
            if config is None:
                recovered = _read_valid_config(backup) if backup.exists() else None
                if recovered is None:
                    raise ConfigurationError(
                        f"pyOS configuration is malformed and no valid backup is available: {path}"
                    )
                _prepare_config_storage(recovered)
                _atomic_write_json_unlocked(path, recovered, mode=0o600)
                config = recovered
    # CONFIG_FILE itself normally lives in the standalone root even after Setup
    # selects a separate data directory, so keep that root owned and manifested.
    get_standalone_root(create=True)
    if not config.get("configured"):
        _migrate_legacy_standalone_artifacts()
    return config


def save_config(config):
    """Validate and atomically persist setup configuration."""
    path = Path(CONFIG_FILE).expanduser()
    with _path_lock(path):
        validated = _validated_config(config)
        if validated is None:
            raise ConfigurationError("Refusing to save invalid pyOS configuration.")
        current = None
        if path.exists():
            current = _read_valid_config(path)
            if current is None:
                backup = _read_valid_config(path.with_name(path.name + ".bak"))
                if backup is None:
                    raise ConfigurationError(
                        f"Refusing to overwrite malformed pyOS configuration: {path}"
                    )
                current = backup
        elif path.with_name(path.name + ".bak").exists():
            current = _read_valid_config(path.with_name(path.name + ".bak"))
            if current is None:
                raise ConfigurationError(
                    f"Refusing to replace invalid pyOS configuration backup: {path}.bak"
                )
        _prepare_config_storage(validated)
        saved = _save_config_unlocked(validated, current=current)
    return saved


def update_config(updater):
    """Perform a cross-process-safe configuration read/modify/write."""
    path = Path(CONFIG_FILE).expanduser()
    with _path_lock(path):
        if path.exists():
            current = _read_valid_config(path)
            if current is None:
                current = _read_valid_config(path.with_name(path.name + ".bak"))
                if current is None:
                    raise ConfigurationError(f"Invalid pyOS configuration: {path}")
        else:
            backup_path = path.with_name(path.name + ".bak")
            if backup_path.exists():
                current = _read_valid_config(backup_path)
                if current is None:
                    raise ConfigurationError(f"Invalid pyOS configuration backup: {backup_path}")
            else:
                current = _default_config()
        replacement = updater(copy.deepcopy(current)) if callable(updater) else {
            **current, **dict(updater)
        }
        validated = _prepare_config_storage(replacement)
        saved = _save_config_unlocked(validated, current=current)
    return saved


def get_data_dir(create=True):
    config = load_config()
    path = Path(config["data_dir"]).expanduser().resolve(strict=False)
    if create:
        kind = "standalone" if path == get_standalone_root(create=False) else "data"
        ensure_storage_owner(path, kind=kind)
    return path


def set_active_user(username, profile_id=None):
    """Select the authenticated profile used by per-user storage helpers."""
    global _ACTIVE_USERNAME, _ACTIVE_PROFILE_ID
    _ACTIVE_USERNAME = str(username).strip() if username else None
    _ACTIVE_PROFILE_ID = str(profile_id).strip() if profile_id else None
    if _ACTIVE_USERNAME:
        _migrate_legacy_profile_data()


def get_active_user():
    return _ACTIVE_USERNAME


def _profile_identifier():
    return _ACTIVE_PROFILE_ID or hashlib.sha256(
        (_ACTIVE_USERNAME or "").casefold().encode("utf-8")
    ).hexdigest()[:24]


def _migrate_legacy_profile_data():
    """Copy former single-user app data into the first profile once."""
    # Setup may have been run before the new standalone root was ever opened.
    # Seed that root from the former home-level files so configured profiles can
    # migrate them through the same validated source layout.
    _migrate_legacy_standalone_artifacts()
    data_root = get_data_dir()
    profile = data_root / "profiles" / _profile_identifier()
    marker = data_root / ".legacy_profile_migration_complete"
    # This marker selects the one profile that receives former single-user
    # data.  Hold its process lock across both the check and migration so two
    # simultaneous first logins cannot seed different profiles.
    with _path_lock(marker):
        if marker.exists():
            return
        profile.mkdir(parents=True, exist_ok=True)
        sources = [data_root]
        standalone = get_standalone_root(create=False)
        if standalone.resolve(strict=False) != data_root.resolve(strict=False):
            sources.append(standalone)
        legacy_names = {
            "gui_settings.json": OWNED_FILE,
            "cli_settings.json": OWNED_FILE,
            "virtual_drives.json": OWNED_FILE,
            "email_settings.json": OWNED_FILE,
            "apps": OWNED_TREE,
        }
        for name, entry_kind in legacy_names.items():
            destination = profile / name
            for source_root in sources:
                source = source_root / name
                try:
                    if source.is_dir() and not destination.exists():
                        shutil.copytree(source, destination)
                        break
                    if source.is_file() and not destination.exists():
                        shutil.copy2(source, destination)
                        break
                except OSError:
                    continue
            if destination.exists():
                register_owned_path(destination, data_root, kind=entry_kind)
        legacy_drive_b = Path(
            load_config().get("drive_b_dir") or data_root / "Drive_B"
        ).expanduser()
        try:
            if legacy_drive_b.is_dir() and not (profile / "Drive_B").exists():
                shutil.copytree(legacy_drive_b, profile / "Drive_B")
        except OSError:
            pass
        if (profile / "Drive_B").exists():
            register_owned_path(profile / "Drive_B", data_root, kind=OWNED_TREE)
        try:
            _atomic_write_bytes_unlocked(
                marker, (_profile_identifier() + "\n").encode("ascii"), mode=0o600
            )
            register_owned_path(marker, data_root)
        except OSError:
            pass


def get_profile_dir(create=True):
    """Return a stable, filesystem-safe directory for the active pyOS user."""
    data_root = get_data_dir(create=create)
    if not _ACTIVE_USERNAME:
        return data_root
    path = data_root / "profiles" / _profile_identifier()
    if create:
        path.mkdir(parents=True, exist_ok=True)
        register_owned_path(path, data_root, kind=OWNED_TREE)
    return path


def get_downloads_dir(create=True):
    path = Path(load_config()["downloads_dir"]).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_drive_b_dir(create=True):
    config = load_config()
    if _ACTIVE_USERNAME:
        path = get_profile_dir(create=create) / "Drive_B"
    else:
        path = Path(config.get("drive_b_dir") or Path(config["data_dir"]) / "Drive_B").expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
        register_owned_path(path, get_data_dir(), kind=OWNED_TREE)
    return path


def _register_profile_artifacts(path):
    data_root = get_data_dir()
    for target in (path, path.with_name("virtual_drives.json"),
                   path.with_name("email_settings.json")):
        register_owned_path(target, data_root)
    register_owned_path(path.parent / "apps", data_root, kind=OWNED_TREE)


def get_gui_settings_path():
    config = load_config()
    path = (get_profile_dir() / "gui_settings.json" if config.get("configured")
            else get_standalone_root(create=True) / "gui_settings.json")
    _register_profile_artifacts(path)
    return path


def get_cli_settings_path():
    config = load_config()
    path = (get_profile_dir() / "cli_settings.json" if config.get("configured")
            else get_standalone_root(create=True) / "cli_settings.json")
    register_owned_path(path, get_data_dir())
    return path


def relaunch_in_configured_environment(script_path):
    """Re-execute a directly launched script with setup's isolated Python."""
    if getattr(sys, "frozen", False):
        return False
    config = load_config()
    if not config.get("configured"):
        return False
    install_dir = Path(config["install_dir"]).expanduser()
    if os.name == "nt":
        runtime = install_dir / ".venv" / "Scripts" / "python.exe"
    else:
        runtime = install_dir / ".venv" / "bin" / "python"
    try:
        if not runtime.is_file() or runtime.resolve() == Path(sys.executable).resolve():
            return False
        script = Path(script_path).resolve()
        os.execv(str(runtime), [str(runtime), str(script), *sys.argv[1:]])
    except OSError:
        return False
    return True
