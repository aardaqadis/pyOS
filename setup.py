"""pyOS setup wizard and unattended installer."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import venv
from datetime import datetime
from pathlib import Path, PurePosixPath
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from pyos_config import (
    CONFIG_FILE, LEGACY_CONFIG_FILE, OWNED_TREE as STORAGE_OWNED_TREE,
    StorageOwnershipError, ensure_storage_owner, get_standalone_root, load_config,
    owned_path_entries, register_owned_path, save_config, verify_storage_owner,
)


SOURCE_DIR = Path(__file__).resolve().parent
APPLICATION_FILES = (
    "pyOSgui.py", "pyOScli.py", "pyos_config.py", "pyos_auth.py", "pyos_updater.py",
    "setup.py", "README.md"
)
PYTHON_PACKAGES = (
    "chess>=1.11,<2.0",
    "fido2>=2.2,<3.0",
    "Pillow>=12.0",
    "mido>=1.3",
    "paramiko>=5.0,<6.0",
    "pygame-ce>=2.5",
    "psutil>=6.0",
    "python-vlc>=3.0",
    "pythonmonkey>=1.3,<2.0",
    "tkinterweb[javascript]>=4.25,<5.0",
)
OPTIONAL_PYTHON_PACKAGES = (
    # No CPython 3.14 Windows wheel is currently published; Setup tries it
    # without making the otherwise functional installation fail.
    "miniupnpc>=2.3,<3.0",
)

OPTIONAL_APPS = (
    ("pyai", "pyAI assistant"), ("browser", "Internet browser"),
    ("media", "Media player"), ("messenger", "LAN Messenger"),
    ("games", "Games suite, Chess, Snake and Sudoku"),
    ("weather", "Weather"), ("news", "News reader"),
    ("ide", "Python IDE and App Maker"), ("modding", "Modding tools"),
)

INSTALL_PRODUCT = "pyOS"
INSTALL_SCHEMA_VERSION = 2
OWNERSHIP_MARKER = ".pyos-installation-owner.json"
INSTALL_MANIFEST = "install_manifest.json"
MANIFEST_METADATA_PATHS = {OWNERSHIP_MARKER, INSTALL_MANIFEST}
OWNED_FILE = "file"
OWNED_TREE = "tree"
RECURSIVE_OWNED_PATHS = {".venv"}
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def paths_overlap(first, second):
    """Return whether either resolved directory contains the other."""
    first = Path(first).expanduser().resolve()
    second = Path(second).expanduser().resolve()
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _unsafe_generic_roots():
    """Return generic/system directories that pyOS must never claim wholesale."""
    home = Path.home().expanduser().resolve()
    candidates = {
        home / name for name in (
            "Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos",
            "AppData", "Applications", "Library", "OneDrive",
        )
    }
    candidates.add(Path(tempfile.gettempdir()))
    for variable in (
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PUBLIC", "SystemRoot",
        "ProgramFiles", "ProgramFiles(x86)", "TEMP", "TMP",
    ):
        value = os.environ.get(variable)
        if value:
            candidates.add(Path(value).expanduser())
    if os.name != "nt":
        candidates.update(Path(value) for value in (
            "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64",
            "/media", "/mnt", "/opt", "/proc", "/root", "/run", "/sbin",
            "/srv", "/sys", "/tmp", "/usr", "/var",
        ))
    return {candidate.resolve(strict=False) for candidate in candidates}


def _validate_managed_root(path, label):
    """Reject roots where installer ownership could encompass unrelated data."""
    path = Path(path).expanduser().resolve(strict=False)
    home = Path.home().expanduser().resolve(strict=False)
    filesystem_root = Path(path.anchor).resolve(strict=False)
    if (path == filesystem_root or path == home or home.is_relative_to(path) or
            path in _unsafe_generic_roots()):
        raise ValueError(f"The {label} directory is an unsafe generic or protected root: {path}")
    return path


def _safe_owned_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("The installation manifest contains an unsafe owned path.")
    path = PurePosixPath(value)
    if (path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts or
            not path.parts or any(
                ":" in part or part != part.rstrip(" .") or
                part.rstrip(" .").split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
                for part in path.parts
            )):
        raise ValueError("The installation manifest contains an unsafe owned path.")
    return Path(*path.parts)


def _atomic_write_json(path, payload):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_installation_id(value):
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None
    return str(parsed) if parsed.version == 4 else None


def load_install_ownership(install_dir):
    """Load and cross-check the ownership marker and uninstall manifest."""
    install_dir = Path(install_dir).expanduser().resolve()
    marker_path = install_dir / OWNERSHIP_MARKER
    manifest_path = install_dir / INSTALL_MANIFEST
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The installation ownership marker or manifest is missing or invalid.") from error
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        raise ValueError("The installation ownership metadata is invalid.")
    installation_id = _valid_installation_id(marker.get("installation_id"))
    if (marker.get("product") != INSTALL_PRODUCT or
            marker.get("schema_version") != INSTALL_SCHEMA_VERSION or
            manifest.get("product") != INSTALL_PRODUCT or
            manifest.get("schema_version") != INSTALL_SCHEMA_VERSION or
            not installation_id or manifest.get("installation_id") != installation_id):
        raise ValueError("The installation ownership marker does not match its manifest.")
    try:
        recorded_root = Path(manifest["install_dir"]).expanduser().resolve()
    except (KeyError, TypeError, OSError) as error:
        raise ValueError("The installation manifest has no valid installation root.") from error
    if recorded_root != install_dir:
        raise ValueError("The installation manifest belongs to a different directory.")
    raw_owned = manifest.get("owned_paths")
    if not isinstance(raw_owned, list):
        raise ValueError("The installation manifest has no owned path list.")
    owned = {_safe_owned_relative(value).as_posix() for value in raw_owned}
    if not MANIFEST_METADATA_PATHS.issubset(owned):
        raise ValueError("The installation manifest does not own its metadata files.")
    raw_external = manifest.get("external_files", [])
    if not isinstance(raw_external, list) or not all(isinstance(value, str) for value in raw_external):
        raise ValueError("The installation manifest has an invalid external file list.")
    raw_types = manifest.get("owned_path_types")
    if raw_types is None:
        # A pre-hardening schema-2 manifest is treated conservatively: every
        # entry is a file.  Repair can explicitly reclaim .venv as a tree.
        path_types = {value: OWNED_FILE for value in owned}
    elif not isinstance(raw_types, dict):
        raise ValueError("The installation manifest has an invalid owned path type map.")
    else:
        path_types = {}
        for value, kind in raw_types.items():
            normalized = _safe_owned_relative(value).as_posix()
            if normalized not in owned or kind not in {OWNED_FILE, OWNED_TREE}:
                raise ValueError("The installation manifest has an invalid owned path type.")
            if kind == OWNED_TREE and normalized not in RECURSIVE_OWNED_PATHS:
                raise ValueError("The installation manifest claims an unsafe recursive tree.")
            path_types[normalized] = kind
        if set(path_types) != owned:
            raise ValueError("The installation manifest does not type every owned path.")
    if any(path_types[value] != OWNED_FILE for value in MANIFEST_METADATA_PATHS):
        raise ValueError("Installation ownership metadata must be regular files.")
    manifest["owned_paths"] = sorted(owned)
    manifest["owned_path_types"] = path_types
    manifest["external_files"] = raw_external
    return marker, manifest


def _safe_install_root(install_dir):
    install_dir = Path(install_dir).expanduser().resolve()
    home = Path.home().resolve()
    if (not install_dir.is_dir() or install_dir == Path(install_dir.anchor) or
            install_dir == home or home.is_relative_to(install_dir)):
        raise ValueError("The configured installation directory is unsafe to remove.")
    return install_dir


def _validated_uninstall_targets(install_dir):
    install_dir = _safe_install_root(install_dir)
    _marker, manifest = load_install_ownership(install_dir)
    metadata_targets = []
    content_targets = []
    for value in manifest["owned_paths"]:
        relative = _safe_owned_relative(value)
        target = install_dir / relative
        # A symlink at the target is safe to unlink, but a symlink in a parent
        # could redirect a manifest entry outside the owned installation root.
        if not target.parent.resolve(strict=False).is_relative_to(install_dir):
            raise ValueError("An installation manifest path escapes the owned directory.")
        if relative.as_posix() in MANIFEST_METADATA_PATHS:
            metadata_targets.append((target, manifest["owned_path_types"][relative.as_posix()]))
        else:
            content_targets.append((target, manifest["owned_path_types"][relative.as_posix()]))

    desktop = (Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop").resolve()
    allowed_shortcuts = {desktop / "pyOS GUI.lnk", desktop / "pyOS CLI.lnk"}
    external_targets = []
    for value in manifest["external_files"]:
        target = Path(value).expanduser().resolve(strict=False)
        if target not in allowed_shortcuts:
            raise ValueError("The installation manifest contains an unsafe external path.")
        if target.is_dir() and not target.is_symlink():
            raise ValueError("An owned shortcut path has been replaced by a directory.")
        external_targets.append((target, OWNED_FILE))
    # Preserve marker and manifest until all payload removal has succeeded.
    return install_dir, content_targets + external_targets, metadata_targets


def _remove_manifest_target(target, install_dir, kind):
    target = Path(target)
    if kind not in {OWNED_FILE, OWNED_TREE}:
        raise ValueError("The manifest contains an invalid owned path type.")
    if ((hasattr(target, "is_junction") and target.is_junction()) or
            target.is_symlink()):
        raise ValueError("An owned path has been replaced by a link or junction.")
    if target.is_dir():
        if (kind != OWNED_TREE or target == install_dir or
                not target.is_relative_to(install_dir)):
            raise ValueError("The manifest cannot recursively own this directory.")
        shutil.rmtree(target)
    elif target.is_file():
        if kind != OWNED_FILE:
            raise ValueError("An owned directory has been replaced by a file.")
        target.unlink()
    elif target.exists():
        raise ValueError("An owned path is not a regular file or directory.")


def _matching_legacy_config(path, install_dir):
    """Return true only for a legacy pyOS config for this exact installation."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        required = ("install_dir", "data_dir", "downloads_dir")
        return (
            isinstance(payload, dict) and payload.get("configured") is True and
            all(isinstance(payload.get(key), str) and payload[key].strip() for key in required) and
            Path(payload["install_dir"]).expanduser().resolve() == Path(install_dir).resolve()
        )
    except (OSError, ValueError, TypeError):
        return False


def uninstall_managed_install(install_dir, config_file=CONFIG_FILE,
                              legacy_config_file=LEGACY_CONFIG_FILE):
    """Delete only marker-authorized installation paths, then configuration.

    Unknown files and directories beneath the installation root are deliberately
    preserved.  Configuration is removed only after every manifest target has
    been removed successfully.
    """
    install_dir, content_targets, metadata_targets = _validated_uninstall_targets(install_dir)
    for target, kind in content_targets:
        _remove_manifest_target(target, install_dir, kind)
    for target, kind in metadata_targets:
        _remove_manifest_target(target, install_dir, kind)
    try:
        install_dir.rmdir()
    except OSError:
        # A non-empty directory contains files the installer did not own.
        pass
    config_path = Path(config_file).expanduser()
    # pyos_config deliberately recovers a missing primary from this backup, so
    # remove the backup first and the primary last after payload success.
    config_path.with_name(config_path.name + ".bak").unlink(missing_ok=True)
    legacy_path = Path(legacy_config_file).expanduser()
    if (legacy_path.resolve(strict=False) != config_path.resolve(strict=False) and
            _matching_legacy_config(legacy_path, install_dir)):
        legacy_path.unlink(missing_ok=True)
    config_path.unlink(missing_ok=True)
    return {"install_dir": str(install_dir), "removed": len(content_targets) + len(metadata_targets)}


def schedule_managed_uninstall(install_dir, config_file=CONFIG_FILE,
                               legacy_config_file=LEGACY_CONFIG_FILE):
    """Schedule manifest-only removal when the running interpreter is owned."""
    if os.name != "nt":
        raise OSError("Deferred uninstall is only available on Windows.")
    install_dir, content_targets, metadata_targets = _validated_uninstall_targets(install_dir)

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    tree_targets = [target for target, kind in content_targets if kind == OWNED_TREE]
    file_targets = [target for target, kind in content_targets if kind == OWNED_FILE]
    metadata_files = [target for target, kind in metadata_targets if kind == OWNED_FILE]
    recursive_values = ",".join(quote(target) for target in tree_targets)
    payload_file_values = ",".join(quote(target) for target in file_targets)
    metadata_values = ",".join(quote(target) for target in metadata_files)
    legacy_path = Path(legacy_config_file).expanduser()
    remove_legacy = (
        legacy_path.resolve(strict=False) != Path(config_file).expanduser().resolve(strict=False) and
        _matching_legacy_config(legacy_path, install_dir)
    )
    legacy_script = (
        f"$legacy={quote(legacy_path)};"
        "if(Test-Path -LiteralPath $legacy){Remove-Item -LiteralPath $legacy -Force -ErrorAction Stop};"
        if remove_legacy else ""
    )
    script = (
        "$ErrorActionPreference='Stop';Start-Sleep -Seconds 2;"
        f"$trees=@({recursive_values});"
        "foreach($tree in $trees){if(Test-Path -LiteralPath $tree){"
        "$item=Get-Item -LiteralPath $tree -Force;"
        "if(-not $item.PSIsContainer -or $item.LinkType){throw 'Owned tree has an unexpected type'};"
        "Remove-Item -LiteralPath $tree -Recurse -Force -ErrorAction Stop}};"
        f"$files=@({payload_file_values});"
        "foreach($file in $files){if(Test-Path -LiteralPath $file){"
        "$item=Get-Item -LiteralPath $file -Force;"
        "if($item.PSIsContainer -or $item.LinkType){throw 'Owned file has an unexpected type'};"
        "Remove-Item -LiteralPath $file -Force -ErrorAction Stop}};"
        f"$metadata=@({metadata_values});"
        "foreach($file in $metadata){if(Test-Path -LiteralPath $file){"
        "$item=Get-Item -LiteralPath $file -Force;"
        "if($item.PSIsContainer -or $item.LinkType){throw 'Ownership metadata has an unexpected type'};"
        "Remove-Item -LiteralPath $file -Force -ErrorAction Stop}};"
        f"$root={quote(install_dir)};"
        "if((Test-Path -LiteralPath $root)-and -not(Get-ChildItem -LiteralPath $root -Force|Select-Object -First 1)){"
        "Remove-Item -LiteralPath $root -Force -ErrorAction Stop};"
        f"$config={quote(Path(config_file).expanduser())};"
        "$backup=$config+'.bak';"
        "if(Test-Path -LiteralPath $backup){Remove-Item -LiteralPath $backup -Force -ErrorAction Stop};"
        f"{legacy_script}"
        "if(Test-Path -LiteralPath $config){Remove-Item -LiteralPath $config -Force -ErrorAction Stop}"
    )
    return subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def default_locations():
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    roaming = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return {
        "install_dir": local / "pyOS",
        "data_dir": roaming / "pyOS",
        "downloads_dir": Path.home() / "Downloads",
    }


class InstallerCore:
    """Performs installation independently from the wizard UI."""

    def __init__(self, install_dir, data_dir, downloads_dir, install_vlc=True,
                 install_ollama=True, create_shortcuts=True, enabled_apps=None,
                 dry_run=False, logger=print):
        self.install_dir = Path(install_dir).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.downloads_dir = Path(downloads_dir).expanduser().resolve()
        self.install_vlc = install_vlc
        self.install_ollama = install_ollama
        self.create_shortcuts = create_shortcuts
        self.enabled_apps = list(
            (app_id for app_id, _label in OPTIONAL_APPS)
            if enabled_apps is None else enabled_apps
        )
        self.dry_run = dry_run
        self.log = logger
        self.warnings = []
        self.installation_id = None
        self._owned_paths = set(MANIFEST_METADATA_PATHS)
        self._owned_path_types = {value: OWNED_FILE for value in MANIFEST_METADATA_PATHS}
        self._external_files = set()
        self._previous_owned_paths = set()
        self._previous_owned_path_types = {}
        self._previous_external_files = set()
        self._existing_install = False
        standalone = get_standalone_root(create=False)
        self._data_owner_kind = "standalone" if self.data_dir == standalone else "data"

    @property
    def venv_dir(self):
        return self.install_dir / ".venv"

    @property
    def python_executable(self):
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"

    def validate(self):
        missing = [name for name in APPLICATION_FILES[:5] if not (SOURCE_DIR / name).is_file()]
        if missing:
            raise FileNotFoundError(f"Setup source files are missing: {', '.join(missing)}")
        _validate_managed_root(self.install_dir, "installation")
        _validate_managed_root(self.data_dir, "data")
        for path in (self.install_dir, self.data_dir, self.downloads_dir):
            if path.exists() and not path.is_dir():
                raise ValueError(f"A file already exists at directory location: {path}")
        locations = {
            "installation": self.install_dir,
            "data": self.data_dir,
            "downloads": self.downloads_dir,
        }
        location_items = list(locations.items())
        for index, (first_name, first) in enumerate(location_items):
            for second_name, second in location_items[index + 1:]:
                if paths_overlap(first, second):
                    raise ValueError(
                        f"The {first_name} and {second_name} directories must not be the same "
                        "or contain one another."
                    )

        if self.install_dir.is_dir() and any(self.install_dir.iterdir()):
            try:
                _marker, manifest = load_install_ownership(self.install_dir)
            except ValueError as error:
                raise ValueError(
                    "The installation directory is not empty and is not owned by this pyOS installer."
                ) from error
            self._existing_install = True
            self.installation_id = manifest["installation_id"]
            self._previous_owned_paths = set(manifest["owned_paths"])
            self._previous_owned_path_types = dict(manifest["owned_path_types"])
            self._previous_external_files = {
                str(Path(value).expanduser().resolve(strict=False))
                for value in manifest["external_files"]
            }
            self._owned_paths.update(manifest["owned_paths"])
            self._owned_path_types.update(manifest["owned_path_types"])
            self._external_files.update(manifest["external_files"])
        else:
            self._existing_install = False
            self.installation_id = str(uuid.uuid4())

        if self.data_dir.is_dir() and any(self.data_dir.iterdir()):
            if not verify_storage_owner(self.data_dir, kind=self._data_owner_kind):
                raise ValueError(
                    "The data directory is not empty and has no matching pyOS ownership marker."
                )
            try:
                data_entries = owned_path_entries(
                    self.data_dir, kind=self._data_owner_kind
                )
            except StorageOwnershipError as error:
                raise ValueError("The data ownership manifest is invalid.") from error
            entries_by_path = {entry.path: entry for entry in data_entries}
            drive_b = self.data_dir / "Drive_B"
            if drive_b.exists() or drive_b.is_symlink():
                entry = entries_by_path.get(drive_b)
                if entry is None:
                    raise ValueError(
                        "The existing Drive_B directory is not owned by this pyOS data manifest."
                    )
                if (entry.kind != STORAGE_OWNED_TREE or drive_b.is_symlink() or
                        (hasattr(drive_b, "is_junction") and drive_b.is_junction()) or
                        not drive_b.is_dir()):
                    raise ValueError("The owned Drive_B tree has an unexpected filesystem type.")

        planned_files = {
            name for name in APPLICATION_FILES if (SOURCE_DIR / name).exists()
        } | {"pyOS GUI.cmd", "pyOS CLI.cmd"}
        for relative in sorted(planned_files):
            self._assert_install_destination(relative, OWNED_FILE)
        self._assert_install_destination(".venv", OWNED_TREE)

        if self.create_shortcuts and os.name == "nt":
            desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
            for shortcut in (desktop / "pyOS GUI.lnk", desktop / "pyOS CLI.lnk"):
                self._assert_external_destination(shortcut)

    def _assert_install_destination(self, relative_path, expected_kind):
        """Refuse an overwrite unless the prior manifest owned this exact type."""
        normalized = _safe_owned_relative(str(relative_path)).as_posix()
        target = self.install_dir / _safe_owned_relative(normalized)
        if not target.parent.resolve(strict=False).is_relative_to(self.install_dir):
            raise ValueError(f"Installation destination escapes its owned root: {normalized}")
        previous_kind = self._previous_owned_path_types.get(normalized)
        if previous_kind is not None and previous_kind != expected_kind:
            raise ValueError(
                f"Installation destination changed manifest type: {normalized}"
            )
        exists = target.exists() or target.is_symlink()
        if not exists:
            return
        if normalized not in self._previous_owned_paths:
            raise ValueError(
                f"Refusing to overwrite an existing item absent from the prior manifest: {normalized}"
            )
        if (target.is_symlink() or
                (hasattr(target, "is_junction") and target.is_junction())):
            raise ValueError(f"Owned installation destination was replaced by a link: {normalized}")
        if expected_kind == OWNED_FILE and not target.is_file():
            raise ValueError(f"Owned installation file was replaced by a directory: {normalized}")
        if expected_kind == OWNED_TREE and not target.is_dir():
            raise ValueError(f"Owned installation tree was replaced by a file: {normalized}")

    def _assert_external_destination(self, path):
        target = Path(path).expanduser().resolve(strict=False)
        if not (target.exists() or target.is_symlink()):
            return
        if str(target) not in self._previous_external_files:
            raise ValueError(f"Refusing to overwrite an unowned shortcut: {target}")
        if (target.is_dir() or target.is_symlink() or
                (hasattr(target, "is_junction") and target.is_junction())):
            raise ValueError(f"Owned shortcut has an unexpected filesystem type: {target}")

    def _configuration(self):
        return {
            "configured": True,
            "install_dir": str(self.install_dir),
            "data_dir": str(self.data_dir),
            "downloads_dir": str(self.downloads_dir),
            "drive_b_dir": str(self.data_dir / "Drive_B"),
            "python_executable": str(self.python_executable),
            "installed_at": datetime.now().isoformat(timespec="seconds"),
            "installer_version": INSTALL_SCHEMA_VERSION,
            "enabled_apps": self.enabled_apps,
        }

    def _write_ownership_metadata(self, configuration=None):
        if self.dry_run:
            return
        if not self.installation_id:
            raise RuntimeError("Installation ownership was not initialized.")
        marker = {
            "product": INSTALL_PRODUCT,
            "schema_version": INSTALL_SCHEMA_VERSION,
            "installation_id": self.installation_id,
        }
        manifest = {
            **(configuration or self._configuration()),
            **marker,
            "owned_paths": sorted(self._owned_paths),
            "owned_path_types": {
                value: self._owned_path_types[value] for value in sorted(self._owned_paths)
            },
            "external_files": sorted(self._external_files),
            "packages": PYTHON_PACKAGES,
            "optional_packages": OPTIONAL_PYTHON_PACKAGES,
        }
        _atomic_write_json(self.install_dir / OWNERSHIP_MARKER, marker)
        _atomic_write_json(self.install_dir / INSTALL_MANIFEST, manifest)

    def _claim_owned(self, *relative_paths):
        normalized_paths = [
            _safe_owned_relative(str(value)).as_posix() for value in relative_paths
        ]
        for normalized in normalized_paths:
            self._assert_install_destination(normalized, OWNED_FILE)
        for normalized in normalized_paths:
            existing = self._owned_path_types.get(normalized)
            if existing not in {None, OWNED_FILE}:
                raise ValueError(f"Owned tree cannot be reclaimed as a file: {normalized}")
            self._owned_paths.add(normalized)
            self._owned_path_types[normalized] = OWNED_FILE
        self._write_ownership_metadata()

    def _claim_owned_tree(self, relative_path):
        normalized = _safe_owned_relative(str(relative_path)).as_posix()
        if normalized not in RECURSIVE_OWNED_PATHS:
            raise ValueError(f"Refusing to claim an unsafe recursive tree: {normalized}")
        self._assert_install_destination(normalized, OWNED_TREE)
        existing = self._owned_path_types.get(normalized)
        if existing not in {None, OWNED_TREE}:
            raise ValueError(f"Owned file cannot be reclaimed as a tree: {normalized}")
        self._owned_paths.add(normalized)
        self._owned_path_types[normalized] = OWNED_TREE
        self._write_ownership_metadata()

    def _claim_external(self, *paths):
        resolved = [Path(path).expanduser().resolve(strict=False) for path in paths]
        for target in resolved:
            self._assert_external_destination(target)
        self._external_files.update(str(target) for target in resolved)
        self._write_ownership_metadata()

    def run_command(self, command, label):
        self.log(label)
        self.log("  " + subprocess.list2cmdline([str(part) for part in command]))
        if self.dry_run:
            return
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=str(self.install_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in process.stdout:
            self.log("  " + line.rstrip())
        return_code = process.wait()
        if return_code:
            raise RuntimeError(f"{label} failed with exit code {return_code}")

    def create_directories(self):
        self.log(f"Creating install directory: {self.install_dir}")
        self.log(f"Creating data directory: {self.data_dir}")
        self.log(f"Configuring downloads directory: {self.downloads_dir}")
        if not self.dry_run:
            self.install_dir.mkdir(parents=True, exist_ok=True)
            if not self._existing_install and any(self.install_dir.iterdir()):
                raise ValueError(
                    "The new installation directory became non-empty before ownership was established."
                )
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            ensure_storage_owner(
                self.data_dir, kind=self._data_owner_kind, require_empty_new=True
            )
            drive_b = self.data_dir / "Drive_B"
            current_entries = {
                entry.path: entry for entry in owned_path_entries(
                    self.data_dir, kind=self._data_owner_kind
                )
            }
            if drive_b.exists() or drive_b.is_symlink():
                existing_entry = current_entries.get(drive_b)
                if (existing_entry is None or
                        existing_entry.kind != STORAGE_OWNED_TREE or
                        drive_b.is_symlink() or
                        (hasattr(drive_b, "is_junction") and drive_b.is_junction()) or
                        not drive_b.is_dir()):
                    raise ValueError(
                        "Refusing to claim or replace an existing unowned Drive_B item."
                    )
            register_owned_path(
                drive_b, root=self.data_dir, kind=STORAGE_OWNED_TREE
            )
            drive_b.mkdir(parents=True, exist_ok=True)
            self._write_ownership_metadata()

    def copy_application(self):
        self.log("Copying pyOS application files")
        self._claim_owned(*(name for name in APPLICATION_FILES if (SOURCE_DIR / name).exists()))
        for name in APPLICATION_FILES:
            source = SOURCE_DIR / name
            if not source.exists():
                continue
            destination = self.install_dir / name
            self.log(f"  {name}")
            if not self.dry_run and source.resolve() != destination.resolve():
                self._assert_install_destination(name, OWNED_FILE)
                shutil.copy2(source, destination)

    def create_environment(self):
        self.log(f"Creating isolated Python environment: {self.venv_dir}")
        self._claim_owned_tree(".venv")
        if not self.dry_run:
            self._assert_install_destination(".venv", OWNED_TREE)
            venv.EnvBuilder(with_pip=True, clear=False).create(self.venv_dir)
        self.run_command(
            [self.python_executable, "-m", "pip", "install", "--upgrade", "pip"],
            "Updating package installer",
        )
        self.run_command(
            [self.python_executable, "-m", "pip", "install", *PYTHON_PACKAGES],
            "Downloading and installing pyOS Python components",
        )
        for package in OPTIONAL_PYTHON_PACKAGES:
            try:
                self.run_command(
                    [self.python_executable, "-m", "pip", "install", package],
                    f"Installing optional Python component: {package}",
                )
            except RuntimeError as error:
                warning = f"Optional component {package} was unavailable: {error}"
                self.warnings.append(warning)
                self.log("WARNING: " + warning)

    @staticmethod
    def vlc_installed():
        if sys.platform == "darwin":
            candidates = (
                Path("/Applications/VLC.app/Contents/MacOS/lib/libvlccore.dylib"),
                Path.home() / "Applications/VLC.app/Contents/MacOS/lib/libvlccore.dylib",
            )
        else:
            candidates = (
                Path(os.environ.get("ProgramFiles", "")) / "VideoLAN" / "VLC" / "libvlc.dll",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN" / "VLC" / "libvlc.dll",
            )
        return any(path.is_file() for path in candidates)

    def install_media_runtime(self):
        if not self.install_vlc or self.vlc_installed():
            self.log("VLC media runtime is already available" if self.vlc_installed() else "VLC installation skipped")
            return
        if sys.platform == "darwin":
            self.install_media_runtime_macos()
            return
        winget = shutil.which("winget")
        if not winget:
            warning = "VLC was not installed because winget is unavailable; media playback will require VLC."
            self.warnings.append(warning)
            self.log("WARNING: " + warning)
            return
        self.run_command(
            [winget, "install", "--id", "VideoLAN.VLC", "--exact", "--silent",
             "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
            "Downloading and installing VLC media runtime",
        )

    def install_media_runtime_macos(self):
        brew = shutil.which("brew")
        if not brew:
            warning = ("VLC was not installed because Homebrew is unavailable; install VLC from "
                       "https://www.videolan.org/vlc/ for media playback.")
            self.warnings.append(warning)
            self.log("WARNING: " + warning)
            return
        self.run_command(
            [brew, "install", "--cask", "vlc"],
            "Downloading and installing VLC media runtime",
        )

    @staticmethod
    def ollama_installed():
        # winget/brew installs may not be on PATH in the current session,
        # so also probe the known install locations.
        if shutil.which("ollama"):
            return True
        candidates = [
            Path("/Applications/Ollama.app"),
            Path.home() / "Applications" / "Ollama.app",
            Path("/usr/local/bin/ollama"),
            Path("/usr/bin/ollama"),
        ]
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Programs" / "Ollama" / "ollama.exe")
        return any(path.exists() for path in candidates)

    def install_ai_runtime(self):
        if not self.install_ollama or self.ollama_installed():
            self.log("Ollama AI runtime is already available" if self.ollama_installed()
                     else "Ollama installation skipped")
            return
        if sys.platform == "darwin":
            brew = shutil.which("brew")
            if not brew:
                warning = ("Ollama was not installed because Homebrew is unavailable; install it from "
                           "https://ollama.com/download for the pyAI app.")
                self.warnings.append(warning)
                self.log("WARNING: " + warning)
                return
            self.run_command(
                [brew, "install", "ollama"],
                "Downloading and installing Ollama AI runtime",
            )
            self.log("Start the AI server with 'ollama serve' (or 'brew services start ollama').")
            return
        if os.name == "nt":
            winget = shutil.which("winget")
            if not winget:
                warning = ("Ollama was not installed because winget is unavailable; "
                           "the pyAI app will require Ollama from https://ollama.com/download.")
                self.warnings.append(warning)
                self.log("WARNING: " + warning)
                return
            self.run_command(
                [winget, "install", "--id", "Ollama.Ollama", "--exact", "--silent",
                 "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                "Downloading and installing Ollama AI runtime",
            )
            return
        warning = ("Ollama was not installed automatically on this platform; install it with "
                   "'curl -fsSL https://ollama.com/install.sh | sh' for the pyAI app.")
        self.warnings.append(warning)
        self.log("NOTE: " + warning)

    def write_launchers(self):
        self.log("Creating pyOS launchers")
        gui_launcher = self.install_dir / "pyOS GUI.cmd"
        cli_launcher = self.install_dir / "pyOS CLI.cmd"
        self._claim_owned(gui_launcher.name, cli_launcher.name)
        gui_command = f'@echo off\r\n"{self.python_executable}" "{self.install_dir / "pyOSgui.py"}"\r\n'
        cli_command = f'@echo off\r\n"{self.python_executable}" "{self.install_dir / "pyOScli.py"}"\r\n'
        if not self.dry_run:
            self._assert_install_destination(gui_launcher.name, OWNED_FILE)
            self._assert_install_destination(cli_launcher.name, OWNED_FILE)
            gui_launcher.write_text(gui_command, encoding="utf-8")
            cli_launcher.write_text(cli_command, encoding="utf-8")
        if self.create_shortcuts and os.name == "nt":
            self.create_desktop_shortcuts(gui_launcher, cli_launcher)

    def create_desktop_shortcuts(self, gui_launcher, cli_launcher):
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
        self._claim_external(desktop / "pyOS GUI.lnk", desktop / "pyOS CLI.lnk")
        script = (
            "$shell = New-Object -ComObject WScript.Shell;"
            f"$s=$shell.CreateShortcut('{desktop / 'pyOS GUI.lnk'}');"
            f"$s.TargetPath='{gui_launcher}';$s.WorkingDirectory='{self.install_dir}';$s.Save();"
            f"$s=$shell.CreateShortcut('{desktop / 'pyOS CLI.lnk'}');"
            f"$s.TargetPath='{cli_launcher}';$s.WorkingDirectory='{self.install_dir}';$s.Save();"
        )
        self.run_command(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            "Creating desktop shortcuts",
        )

    def write_configuration(self):
        config = self._configuration()
        self.log(f"Writing shared configuration: {CONFIG_FILE}")
        if not self.dry_run:
            # Ownership metadata must be durable before a shared configuration
            # advertises the installation as complete.
            self._write_ownership_metadata(config)
            save_config(config)

    def install(self):
        self.validate()
        self.create_directories()
        self.copy_application()
        self.create_environment()
        self.install_media_runtime()
        self.install_ai_runtime()
        self.write_launchers()
        self.write_configuration()
        self.log("pyOS installation completed")
        return {
            "install_dir": str(self.install_dir),
            "python_executable": str(self.python_executable),
            "warnings": self.warnings,
        }


class SetupWizard:
    """Monochrome multi-page setup wizard."""

    def __init__(self, root):
        self.root = root
        self.root.title("pyOS Setup")
        self.root.geometry("720x520")
        self.root.minsize(640, 460)
        self.root.configure(bg="white")
        self.root.option_add("*Font", ("Courier New", 9))
        self.page = 0
        self.installing = False
        self.install_result = None
        existing = load_config()
        self.existing_config = existing if existing.get("configured") else None
        defaults = default_locations()
        if self.existing_config:
            defaults.update({key: Path(self.existing_config[key]) for key in defaults})
        self.install_var = tk.StringVar(value=str(defaults["install_dir"]))
        self.data_var = tk.StringVar(value=str(defaults["data_dir"]))
        self.downloads_var = tk.StringVar(value=str(defaults["downloads_dir"]))
        self.vlc_var = tk.BooleanVar(value=True)
        self.ollama_var = tk.BooleanVar(value=True)
        self.shortcuts_var = tk.BooleanVar(value=True)
        self.launch_var = tk.BooleanVar(value=True)
        configured_apps = existing.get("enabled_apps") if self.existing_config else None
        selected_apps = set(configured_apps or ())
        if configured_apps is None:
            selected_apps = {app_id for app_id, _label in OPTIONAL_APPS}
        self.app_vars = {
            app_id: tk.BooleanVar(value=app_id in selected_apps)
            for app_id, _label in OPTIONAL_APPS
        }

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background="white", foreground="black", font=("Courier New", 9))
        style.configure("TButton", background="white", foreground="black", bordercolor="black")
        style.configure("TProgressbar", background="black", troughcolor="white", bordercolor="black")

        header = tk.Frame(root, bg="black", height=64)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="pyOS SETUP", bg="black", fg="white",
                 font=("Courier New", 17, "bold")).pack(side=tk.LEFT, padx=18)
        self.page_title = tk.Label(header, text="", bg="black", fg="white", anchor=tk.E)
        self.page_title.pack(side=tk.RIGHT, padx=18, fill=tk.Y)

        self.content = tk.Frame(root, bg="white")
        self.content.pack(fill=tk.BOTH, expand=True, padx=22, pady=18)
        footer = tk.Frame(root, bg="white", relief=tk.RAISED, bd=1)
        footer.pack(fill=tk.X)
        self.cancel_button = ttk.Button(footer, text="Cancel", command=self.cancel)
        self.cancel_button.pack(side=tk.LEFT, padx=10, pady=10)
        self.next_button = ttk.Button(footer, text="Next >", command=self.next_page)
        self.next_button.pack(side=tk.RIGHT, padx=10, pady=10)
        self.back_button = ttk.Button(footer, text="< Back", command=self.previous_page)
        self.back_button.pack(side=tk.RIGHT, padx=2, pady=10)
        self.render_page()

    def clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()

    def render_page(self):
        self.clear_content()
        titles = ("Welcome", "Locations", "Components", "Install", "Complete")
        self.page_title.configure(text=f"{self.page + 1}/5  {titles[self.page]}")
        self.back_button.configure(state=tk.NORMAL if 0 < self.page < 3 else tk.DISABLED)
        self.next_button.configure(text="Finish" if self.page == 4 else "Next >", state=tk.NORMAL)
        if self.page == 0:
            self.render_welcome()
        elif self.page == 1:
            self.render_locations()
        elif self.page == 2:
            self.render_components()
        elif self.page == 3:
            self.render_installation()
        else:
            self.render_complete()

    def render_welcome(self):
        heading = "Maintain Python OS" if self.existing_config else "Install Python OS"
        tk.Label(self.content, text=heading, bg="white", fg="black",
                 font=("Courier New", 16, "bold"), anchor=tk.W).pack(fill=tk.X, pady=(16, 14))
        if self.existing_config:
            tk.Label(
                self.content,
                text=f"pyOS is installed at:\n{self.existing_config['install_dir']}",
                bg="white", fg="black", justify=tk.LEFT, anchor=tk.W,
            ).pack(fill=tk.X, pady=(0, 16))
            actions = tk.Frame(self.content, bg="white")
            actions.pack(fill=tk.X, pady=8)
            ttk.Button(actions, text="Repair / Modify", command=self.start_repair).pack(fill=tk.X, pady=4)
            ttk.Button(actions, text="Clear Cached Memory", command=self.clear_cached_memory).pack(fill=tk.X, pady=4)
            ttk.Button(actions, text="Uninstall pyOS", command=self.uninstall_existing).pack(fill=tk.X, pady=4)
            self.next_button.configure(state=tk.DISABLED)
            return
        text = (
            "This wizard installs pyOS GUI and CLI, downloads their Python components, "
            "configures shared storage, and creates launchers.\n\n"
            "The existing source project is not modified or removed."
        )
        tk.Label(self.content, text=text, bg="white", fg="black", justify=tk.LEFT,
                 wraplength=620, anchor=tk.NW).pack(fill=tk.BOTH, expand=True)

    def location_row(self, label, variable):
        row = tk.Frame(self.content, bg="white")
        row.pack(fill=tk.X, pady=8)
        tk.Label(row, text=label, bg="white", fg="black", width=18, anchor=tk.W).pack(side=tk.LEFT)
        tk.Entry(row, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        ttk.Button(row, text="Browse", command=lambda: self.browse_directory(variable)).pack(side=tk.RIGHT)

    def render_locations(self):
        tk.Label(self.content, text="Choose Locations", bg="white", fg="black",
                 font=("Courier New", 14, "bold"), anchor=tk.W).pack(fill=tk.X, pady=(4, 12))
        self.location_row("Install pyOS to:", self.install_var)
        self.location_row("Store pyOS data:", self.data_var)
        self.location_row("Downloads folder:", self.downloads_var)
        tk.Label(self.content, text="Drive B and both applications' settings use the data location.",
                 bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=10)

    def render_components(self):
        tk.Label(self.content, text="Installation Components", bg="white", fg="black",
                 font=("Courier New", 14, "bold"), anchor=tk.W).pack(fill=tk.X, pady=(4, 12))
        tk.Checkbutton(self.content, text="pyOS GUI and CLI (required)", variable=tk.BooleanVar(value=True),
                       state=tk.DISABLED, bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=5)
        tk.Checkbutton(self.content, text="Python media, browser, image, MIDI, and system packages (required)",
                       variable=tk.BooleanVar(value=True), state=tk.DISABLED,
                       bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=5)
        tk.Checkbutton(self.content, text="Install VLC media runtime when missing", variable=self.vlc_var,
                       bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=5)
        tk.Checkbutton(self.content, text="Install Ollama local AI runtime when missing (pyAI)",
                       variable=self.ollama_var,
                       bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=5)
        tk.Checkbutton(self.content, text="Create desktop shortcuts", variable=self.shortcuts_var,
                       bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=5)
        apps = tk.LabelFrame(self.content, text="pyOS APPS", bg="white", fg="black", padx=8, pady=5)
        apps.pack(fill=tk.X, pady=(10, 0))
        for index, (app_id, label) in enumerate(OPTIONAL_APPS):
            tk.Checkbutton(
                apps, text=label, variable=self.app_vars[app_id], bg="white", fg="black", anchor=tk.W,
            ).grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 18), pady=2)

    def render_installation(self):
        self.back_button.configure(state=tk.DISABLED)
        self.next_button.configure(state=tk.DISABLED)
        self.cancel_button.configure(state=tk.DISABLED)
        tk.Label(self.content, text="Installing pyOS", bg="white", fg="black",
                 font=("Courier New", 14, "bold"), anchor=tk.W).pack(fill=tk.X)
        self.progress = ttk.Progressbar(self.content, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=10)
        self.install_log = scrolledtext.ScrolledText(
            self.content, bg="black", fg="white", insertbackground="white",
            font=("Courier New", 8), state=tk.DISABLED, height=16
        )
        self.install_log.pack(fill=tk.BOTH, expand=True)
        self.progress.start(12)
        self.installing = True
        threading.Thread(target=self.run_installation, daemon=True).start()

    def append_log(self, message):
        def write():
            if not self.install_log.winfo_exists():
                return
            self.install_log.configure(state=tk.NORMAL)
            self.install_log.insert(tk.END, message + "\n")
            self.install_log.see(tk.END)
            self.install_log.configure(state=tk.DISABLED)
        self.root.after(0, write)

    def run_installation(self):
        try:
            installer = InstallerCore(
                self.install_var.get(), self.data_var.get(), self.downloads_var.get(),
                install_vlc=self.vlc_var.get(), install_ollama=self.ollama_var.get(),
                create_shortcuts=self.shortcuts_var.get(),
                enabled_apps=[app_id for app_id, variable in self.app_vars.items() if variable.get()],
                logger=self.append_log,
            )
            self.install_result = installer.install()
            self.root.after(0, lambda: self.install_finished(None))
        except Exception as error:
            self.root.after(0, lambda message=str(error): self.install_finished(message))

    def install_finished(self, error):
        self.progress.stop()
        self.installing = False
        if error:
            self.append_log("ERROR: " + error)
            self.cancel_button.configure(text="Close", state=tk.NORMAL)
            messagebox.showerror("pyOS Setup", f"Installation failed:\n{error}")
            return
        self.page = 4
        self.cancel_button.configure(state=tk.NORMAL)
        self.render_page()

    def render_complete(self):
        tk.Label(self.content, text="pyOS Is Ready", bg="white", fg="black",
                 font=("Courier New", 16, "bold"), anchor=tk.W).pack(fill=tk.X, pady=(16, 14))
        install_dir = self.install_result["install_dir"] if self.install_result else self.install_var.get()
        tk.Label(self.content, text=f"Installed to:\n{install_dir}", bg="white", fg="black",
                 justify=tk.LEFT, anchor=tk.W).pack(fill=tk.X, pady=8)
        tk.Checkbutton(self.content, text="Launch pyOS GUI when setup closes", variable=self.launch_var,
                       bg="white", fg="black", anchor=tk.W).pack(fill=tk.X, pady=12)

    def start_repair(self):
        """Reuse the normal installer to verify files, libraries, runtimes and app choices."""
        self.page = 2
        self.next_button.configure(state=tk.NORMAL)
        self.render_page()

    def clear_cached_memory(self):
        """Remove regenerable Python/browser caches while preserving accounts and user files."""
        config = self.existing_config or {}
        roots = [Path(config[key]).expanduser().resolve() for key in ("install_dir", "data_dir") if config.get(key)]
        home = Path.home().resolve()
        removed_files = 0
        removed_dirs = 0
        try:
            for root in roots:
                if not root.is_dir() or root in {Path(root.anchor), home}:
                    continue
                for cache in list(root.rglob("__pycache__")) + list(root.rglob("cache")):
                    if cache.is_dir() and cache.resolve().is_relative_to(root):
                        removed_files += sum(1 for item in cache.rglob("*") if item.is_file())
                        shutil.rmtree(cache)
                        removed_dirs += 1
                for compiled in root.rglob("*.py[co]"):
                    if compiled.is_file() and compiled.resolve().is_relative_to(root):
                        compiled.unlink()
                        removed_files += 1
        except OSError as error:
            messagebox.showerror("pyOS Maintenance", f"Cache cleanup stopped:\n{error}")
            return
        messagebox.showinfo(
            "pyOS Maintenance",
            f"Cleared {removed_files} cached files from {removed_dirs} cache folders.\n"
            "Accounts, settings, custom apps, downloads, and Drive B were preserved.",
        )

    def uninstall_existing(self):
        """Remove installed program files and configuration while preserving user data."""
        config = self.existing_config or {}
        install_dir = Path(config.get("install_dir", "")).expanduser().resolve()
        try:
            _safe_install_root(install_dir)
            load_install_ownership(install_dir)
        except ValueError:
            messagebox.showerror("Uninstall pyOS", "The configured installation directory is unsafe to remove.")
            return
        if not messagebox.askyesno(
            "Uninstall pyOS",
            f"Remove pyOS and its installed libraries from:\n{install_dir}\n\n"
            "Your account, settings, custom apps, Drive B, and downloads will be preserved?",
            icon=messagebox.WARNING,
        ):
            return
        try:
            runtime = Path(sys.executable).resolve()
            if os.name == "nt" and runtime.is_relative_to(install_dir):
                schedule_managed_uninstall(install_dir)
                self.root.destroy()
            else:
                uninstall_managed_install(install_dir)
                self.existing_config = None
                messagebox.showinfo("Uninstall pyOS", "pyOS was uninstalled. User data was preserved.")
                self.root.destroy()
        except (OSError, ValueError) as error:
            messagebox.showerror("Uninstall pyOS", f"Uninstall failed:\n{error}")

    def browse_directory(self, variable):
        selected = filedialog.askdirectory(parent=self.root, initialdir=variable.get() or str(Path.home()))
        if selected:
            variable.set(selected)

    def validate_locations(self):
        values = (self.install_var.get(), self.data_var.get(), self.downloads_var.get())
        if not all(value.strip() for value in values):
            messagebox.showerror("pyOS Setup", "All locations are required.")
            return False
        paths = [Path(value).expanduser().resolve() for value in values]
        for index, first in enumerate(paths):
            for second in paths[index + 1:]:
                if paths_overlap(first, second):
                    messagebox.showerror(
                        "pyOS Setup",
                        "Installation, data, and downloads locations must not be the same "
                        "or contain one another.",
                    )
                    return False
        return True

    def next_page(self):
        if self.page == 4:
            if self.launch_var.get() and self.install_result:
                subprocess.Popen([
                    self.install_result["python_executable"],
                    str(Path(self.install_result["install_dir"]) / "pyOSgui.py"),
                ])
            self.root.destroy()
            return
        if self.page == 1 and not self.validate_locations():
            return
        self.page += 1
        self.render_page()

    def previous_page(self):
        if self.page > 0 and not self.installing:
            self.page -= 1
            self.render_page()

    def cancel(self):
        if self.installing:
            return
        if messagebox.askyesno("pyOS Setup", "Exit setup?"):
            self.root.destroy()


def parse_arguments():
    defaults = default_locations()
    parser = argparse.ArgumentParser(description="Install pyOS GUI and CLI")
    parser.add_argument("--quiet", action="store_true", help="Run without the graphical wizard")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print actions without changing files")
    parser.add_argument("--install-dir", default=str(defaults["install_dir"]))
    parser.add_argument("--data-dir", default=str(defaults["data_dir"]))
    parser.add_argument("--downloads-dir", default=str(defaults["downloads_dir"]))
    parser.add_argument("--no-vlc", action="store_true")
    parser.add_argument("--no-ollama", action="store_true")
    parser.add_argument("--no-shortcuts", action="store_true")
    return parser.parse_args()


def main():
    args = parse_arguments()
    if args.quiet or args.dry_run:
        installer = InstallerCore(
            args.install_dir, args.data_dir, args.downloads_dir,
            install_vlc=not args.no_vlc, install_ollama=not args.no_ollama,
            create_shortcuts=not args.no_shortcuts,
            dry_run=args.dry_run,
        )
        installer.install()
        return
    root = tk.Tk()
    SetupWizard(root)
    root.mainloop()


if __name__ == "__main__":
    main()
