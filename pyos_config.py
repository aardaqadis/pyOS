"""Shared installation and storage configuration for pyOS."""

import json
import os
import sys
from pathlib import Path


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
        "configured": False,
    }
    try:
        loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            for key in defaults:
                if key in loaded and isinstance(loaded[key], (str, bool)):
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


def get_downloads_dir(create=True):
    path = Path(load_config()["downloads_dir"]).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_drive_b_dir(create=True):
    config = load_config()
    path = Path(config.get("drive_b_dir") or Path(config["data_dir"]) / "Drive_B").expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def get_gui_settings_path():
    config = load_config()
    if config.get("configured"):
        return get_data_dir() / "gui_settings.json"
    return Path.home() / ".pyos_gui_settings.json"


def get_cli_settings_path():
    config = load_config()
    if config.get("configured"):
        return get_data_dir() / "cli_settings.json"
    return Path.home() / ".pyOS_settings.json"


def relaunch_in_configured_environment(script_path):
    """Re-execute a directly launched script with setup's isolated Python."""
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
