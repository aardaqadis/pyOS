"""Shared installation and storage configuration for pyOS."""

import json
import os
import sys
import hashlib
import shutil
from pathlib import Path


_ACTIVE_USERNAME = None
_ACTIVE_PROFILE_ID = None


CONFIG_FILE = Path(
    os.environ.get("PYOS_CONFIG_FILE", Path.home() / ".pyos_install.json")
).expanduser()


def load_config():
    """Return validated setup configuration, or legacy-compatible defaults."""
    defaults = {
        "install_dir": str(Path(__file__).resolve().parent),
        "data_dir": str(Path.home()),
        "downloads_dir": str(Path.home() / "Downloads"),
        "drive_b_dir": str(Path.home() / ".pyOS_Drive_B"),
        "enabled_apps": None,
        "configured": False,
    }
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key in defaults:
                if key == "enabled_apps" and isinstance(loaded.get(key), list):
                    defaults[key] = [str(item) for item in loaded[key] if isinstance(item, str)]
                elif key in loaded and isinstance(loaded[key], (str, bool)):
                    defaults[key] = loaded[key]
    except (OSError, ValueError, TypeError):
        pass
    return defaults


def save_config(config):
    """Atomically persist setup configuration."""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix(CONFIG_FILE.suffix + ".tmp")
    temporary.write_text(json.dumps(config, indent=2), encoding="utf-8")
    temporary.replace(CONFIG_FILE)


def get_data_dir(create=True):
    path = Path(load_config()["data_dir"]).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
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


def _migrate_legacy_profile_data():
    """Copy former single-user app data into the first profile once."""
    data_root = get_data_dir()
    profile_id = _ACTIVE_PROFILE_ID or hashlib.sha256(
        _ACTIVE_USERNAME.casefold().encode("utf-8")
    ).hexdigest()[:24]
    profile = data_root / "profiles" / profile_id
    marker = data_root / ".legacy_profile_migration_complete"
    if marker.exists():
        return
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("gui_settings.json", "cli_settings.json", "virtual_drives.json",
                 "email_settings.json", "apps"):
        source, destination = data_root / name, profile / name
        try:
            if source.is_dir() and not destination.exists():
                shutil.copytree(source, destination)
            elif source.is_file() and not destination.exists():
                shutil.copy2(source, destination)
        except OSError:
            pass
    legacy_drive_b = Path(load_config().get("drive_b_dir") or data_root / "Drive_B").expanduser()
    try:
        if legacy_drive_b.is_dir() and not (profile / "Drive_B").exists():
            shutil.copytree(legacy_drive_b, profile / "Drive_B")
    except OSError:
        pass
    try:
        marker.touch(exist_ok=True)
    except OSError:
        pass


def get_profile_dir(create=True):
    """Return a stable, filesystem-safe directory for the active pyOS user."""
    if not _ACTIVE_USERNAME:
        return get_data_dir(create=create)
    profile_id = _ACTIVE_PROFILE_ID or hashlib.sha256(
        _ACTIVE_USERNAME.casefold().encode("utf-8")
    ).hexdigest()[:24]
    path = get_data_dir(create=create) / "profiles" / profile_id
    if create:
        path.mkdir(parents=True, exist_ok=True)
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
    return path


def get_gui_settings_path():
    config = load_config()
    if config.get("configured"):
        return get_profile_dir() / "gui_settings.json"
    return Path.home() / ".pyos_gui_settings.json"


def get_cli_settings_path():
    config = load_config()
    if config.get("configured"):
        return get_profile_dir() / "cli_settings.json"
    return Path.home() / ".pyOS_settings.json"


def relaunch_in_configured_environment(script_path):
    """Re-execute a directly launched script with setup's isolated Python."""
    if getattr(sys, "frozen", False):
        # A PyInstaller executable already contains its selected runtime and dependencies.
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
