"""Give a packaged release a profile isolated from developer/user source runs."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path


FACTORY_NAMESPACE = "pyOS-Release-2.0-Factory"


def _state_parent() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))


root = _state_parent() / FACTORY_NAMESPACE
data = root / "data"
config_path = root / "install.json"

# These variables are consumed when pyos_config is imported, after PyInstaller
# has executed this runtime hook.  Explicit overrides also disable migration of
# unrelated legacy developer state into the release profile.
os.environ["PYOS_HOME"] = str(root)
os.environ["PYOS_CONFIG_FILE"] = str(config_path)
os.environ["PYOS_MIGRATE_LEGACY_STATE"] = "0"

if not config_path.exists():
    root.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_name(
        f".{config_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(
                {
                    "install_dir": str(Path(sys.executable).resolve().parent),
                    "data_dir": str(data),
                    "downloads_dir": str(Path.home() / "Downloads"),
                    "drive_b_dir": str(data / "Drive_B"),
                    "enabled_apps": None,
                    "configured": True,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)

