"""Consent-first, fail-closed GitHub update support for pyOS."""

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPOSITORY = "aardaqadis/pyOS"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
USER_AGENT = "pyOS-Updater/2.0"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 10000
SOURCE_TRANSACTION_FILE = ".pyos-source-update-transaction.json"
SOURCE_TRANSACTION_VERSION = 1
SOURCE_INVENTORY_FILE = ".pyos-source-files.json"
EXCLUDED_PARTS = {".git", ".github", ".idea", ".venv", "venv", "__pycache__", "build", "dist"}
EXCLUDED_FILES = {".pyos-installation-owner.json", ".pyos-owner.json", "install_manifest.json"}
INSTALL_OWNER_FILE = ".pyos-installation-owner.json"
INSTALL_MANIFEST_FILE = "install_manifest.json"
SOURCE_ASSET_NAMES = {"pyos-source.zip", "pyos-sources.zip", "pyos-source-update.zip"}
REQUIRED_SOURCE_FILES = {
    "pyOSgui.py", "pyOScli.py", "pyos_config.py", "pyos_auth.py",
    "pyos_updater.py", "setup.py",
}
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
IMMUTABLE_REF = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
SHA256_DIGEST = re.compile(r"[0-9a-fA-F]{64}\Z")
CERTIFICATE_THUMBPRINT = re.compile(r"[0-9a-fA-F]{40}\Z")
# Populate this at build time with the official code-signing certificate's
# SHA-1 thumbprint.  Empty is deliberately non-installable, not trust-on-first-use.
TRUSTED_AUTHENTICODE_THUMBPRINTS = frozenset()
# Populate this at build time with ``(immutable commit, archive SHA-256)``
# pairs produced by the official release process.  GitHub's release ``digest``
# field remains useful transport metadata, but it is not an independent
# signature over the commit-to-archive binding.  Empty is deliberately
# non-installable.
TRUSTED_SOURCE_RELEASE_BINDINGS = frozenset()

# Conservative first-update fallback for an unpacked source tree that predates
# SOURCE_INVENTORY_FILE and is not a Git checkout.  Unknown files are never
# inferred as managed and are therefore never removed.
KNOWN_SOURCE_FILES = frozenset({
    ".gitignore", "LICENSE.md", "README.md", "build_pyos.ps1", "pyOS.spec",
    "pyOScli.py", "pyOSgui.py", "pyos_auth.py", "pyos_config.py",
    "pyos_updater.py", "pyos2.0.png", "pyproject.toml", "setup.py",
    "requirements.lock", "requirements-optional.lock", "requirements-dev.lock",
    "requirements-build.lock", "exe_tools/Build-pyOSExe.ps1",
    "exe_tools/Test-pyOSExe.ps1", "exe_tools/factory_runtime.py",
    "exe_tools/version_info.txt", "test_pyos_updater_trust_9c41.py",
    "test_setup_install_safety_9c41.py", "tests/test_gui_safety.py",
    "tests/test_gui_tasks.py", "tests/test_storage_auth.py",
})


def _json_request(url):
    request = urllib.request.Request(
        url, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        final_url = urllib.parse.urlsplit(str(getattr(response, "geturl", lambda: url)()))
        if final_url.scheme.casefold() != "https" or not final_url.netloc:
            raise ValueError("GitHub update metadata was redirected to an unauthenticated URL.")
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


def _commit_sha(ref):
    """Resolve a branch or tag once and return the immutable commit object id."""
    encoded = urllib.parse.quote(str(ref), safe="")
    commit = _json_request(f"{API_ROOT}/commits/{encoded}")
    if not isinstance(commit, dict):
        raise ValueError("GitHub returned invalid commit metadata.")
    sha = str(commit.get("sha") or "")
    if not IMMUTABLE_REF.fullmatch(sha):
        raise ValueError("GitHub did not return an immutable commit reference.")
    return sha.lower(), commit


def _normalized_asset(item, ref):
    return {
        "name": str(item.get("name") or ""),
        "url": str(item.get("browser_download_url") or ""),
        "size": int(item.get("size") or 0),
        "digest": str(item.get("digest") or ""),
        "asset_id": int(item.get("id") or 0),
        "ref": ref,
    }


def latest_update(channel):
    """Return normalized metadata pinned to an immutable commit.

    GitHub-generated source archives do not publish a SHA-256 digest.  A release
    therefore has an installable source update only when it publishes one of the
    supported source ZIP assets and this build pins the asset's exact
    commit-to-digest binding.  Callers may still display other releases, but
    installation will fail closed.
    """
    if channel == "stable":
        release = _json_request(f"{API_ROOT}/releases/latest")
        if not isinstance(release, dict):
            raise ValueError("GitHub returned invalid release metadata.")
        tag = str(release["tag_name"])
        sha, _commit = _commit_sha(tag)
        assets = [
            _normalized_asset(item, sha)
            for item in release.get("assets", []) if isinstance(item, dict)
        ]
        source_asset = next(
            (item for item in assets if item["name"].casefold() in SOURCE_ASSET_NAMES), None
        )
        return {
            "channel": "stable", "ref": sha, "version": tag,
            "name": str(release.get("name") or tag),
            "notes": str(release.get("body") or "No release notes supplied."),
            "date": str(release.get("published_at") or ""),
            "archive_url": str(source_asset["url"] if source_asset else
                               f"https://github.com/{REPOSITORY}/archive/{sha}.zip"),
            "digest": str(source_asset["digest"] if source_asset else ""),
            "assets": assets,
            "page_url": str(release.get("html_url") or ""),
        }
    if channel == "unstable":
        repository = _json_request(API_ROOT)
        if not isinstance(repository, dict):
            raise ValueError("GitHub returned invalid repository metadata.")
        branch = str(repository.get("default_branch") or "main")
        sha, commit = _commit_sha(branch)
        details = commit.get("commit", {})
        if not isinstance(details, dict):
            raise ValueError("GitHub returned invalid commit details.")
        committer = details.get("committer", {})
        if not isinstance(committer, dict):
            committer = {}
        return {
            "channel": "unstable", "ref": sha,
            "name": f"Commit {sha[:8]}",
            "notes": str(details.get("message") or "No commit message supplied."),
            "date": str(committer.get("date") or ""),
            "archive_url": f"https://github.com/{REPOSITORY}/archive/{sha}.zip",
            # GitHub does not provide a SHA-256 digest for generated archives.
            # Keeping this empty makes install_source_update reject it.
            "digest": "", "assets": [],
            "page_url": f"https://github.com/{REPOSITORY}/commit/{sha}",
        }
    raise ValueError("Update channel must be stable or unstable.")


def current_git_ref(project_dir):
    """Return the current Git commit when running from a checkout."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(project_dir), capture_output=True,
            text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = result.stdout.strip()
        return value if result.returncode == 0 and IMMUTABLE_REF.fullmatch(value) else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def git_has_local_changes(project_dir):
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(project_dir), capture_output=True,
            text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def _report_progress(callback, phase, completed=None, total=None):
    if callback:
        callback(phase, completed, total)


def _required_sha256(metadata):
    """Return a normalized mandatory SHA-256 digest or fail before downloading."""
    if not isinstance(metadata, dict):
        raise ValueError("Trusted update metadata is required.")
    value = str(metadata.get("digest") or metadata.get("sha256") or "").strip()
    if value.casefold().startswith("sha256:"):
        value = value[7:]
    if not SHA256_DIGEST.fullmatch(value):
        raise ValueError("The update has no valid trusted SHA-256 digest; refusing to install it.")
    return value.lower()


def _required_immutable_ref(metadata):
    if not isinstance(metadata, dict):
        raise ValueError("Trusted update metadata is required.")
    ref = str(metadata.get("ref") or "").strip()
    if not IMMUTABLE_REF.fullmatch(ref):
        raise ValueError("The update is not pinned to an immutable commit reference.")
    return ref.lower()


def _validate_source_release_binding(ref, digest):
    """Require a build-time-pinned commit-to-source-archive binding."""
    try:
        bindings = tuple(TRUSTED_SOURCE_RELEASE_BINDINGS)
    except TypeError as error:
        raise ValueError("The official pyOS source trust allowlist is malformed.") from error

    trusted_digests = set()
    for binding in bindings:
        if not isinstance(binding, (tuple, list)) or len(binding) != 2:
            raise ValueError("The official pyOS source trust allowlist is malformed.")
        pinned_ref = str(binding[0]).strip().lower()
        pinned_digest = str(binding[1]).strip().lower()
        if pinned_digest.startswith("sha256:"):
            pinned_digest = pinned_digest[7:]
        if (not IMMUTABLE_REF.fullmatch(pinned_ref) or
                not SHA256_DIGEST.fullmatch(pinned_digest)):
            raise ValueError("The official pyOS source trust allowlist is malformed.")
        if hmac.compare_digest(pinned_ref, ref):
            trusted_digests.add(pinned_digest)

    if not trusted_digests:
        raise ValueError(
            f"No official pyOS source release binding is pinned for commit {ref}; "
            "refusing the source update."
        )
    if not any(hmac.compare_digest(value, digest) for value in trusted_digests):
        raise ValueError(
            "The source archive digest does not match the official "
            "commit-to-digest allowlist; refusing the source update."
        )


def _url_path_is_bound_to_ref(parsed_url, ref):
    path = urllib.parse.unquote(parsed_url.path).casefold()
    return re.search(
        rf"(?<![0-9a-f]){re.escape(ref.casefold())}(?![0-9a-f])", path
    ) is not None


def _validate_archive_reference(update, ref):
    url = str(update.get("archive_url") or "")
    if not url:
        raise ValueError("The update does not contain a source archive URL.")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ValueError("Update downloads must use an authenticated HTTPS URL.")
    if not _url_path_is_bound_to_ref(parsed, ref):
        # Release assets use an immutable numeric asset id rather than the
        # commit in their URL.  latest_update retains that id when applicable.
        source_asset = update if update.get("asset_id") else next(
            (asset for asset in update.get("assets", [])
             if str(asset.get("url") or "") == url and asset.get("asset_id")), None
        )
        if source_asset is None:
            raise ValueError("The source archive URL is not bound to the immutable update reference.")
        if (_required_immutable_ref(source_asset) != ref or
                _required_sha256(source_asset) != _required_sha256(update)):
            raise ValueError("The source asset metadata does not match the immutable update.")


def _validate_executable_reference(asset, ref):
    parsed = urllib.parse.urlsplit(str(asset.get("url") or ""))
    if parsed.scheme.casefold() != "https" or not parsed.netloc:
        raise ValueError("Update downloads must use an authenticated HTTPS URL.")
    try:
        asset_id = int(asset.get("asset_id") or 0)
    except (TypeError, ValueError):
        asset_id = 0
    if asset_id <= 0 and not _url_path_is_bound_to_ref(parsed, ref):
        raise ValueError("The executable asset is not bound to immutable release metadata.")


def _download(url, destination, progress=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
        final_url = urllib.parse.urlsplit(str(getattr(response, "geturl", lambda: url)()))
        if final_url.scheme.casefold() != "https" or not final_url.netloc:
            raise ValueError("The update download was redirected to an unauthenticated URL.")
        try:
            expected_value = int(response.headers.get("Content-Length", 0))
            expected = expected_value if expected_value > 0 else None
        except (TypeError, ValueError):
            expected = None
        if expected is not None and expected > MAX_ARCHIVE_BYTES:
            raise ValueError("The update download exceeds the 1 GB safety limit.")
        _report_progress(progress, "Downloading update", 0, expected)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("The update download exceeds the 1 GB safety limit.")
            digest.update(chunk)
            output.write(chunk)
            _report_progress(progress, "Downloading update", total, expected)
    return digest.hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_link_or_junction(path):
    path = Path(path)
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _copy_durable(source, destination):
    source, destination = Path(source), Path(destination)
    with source.open("rb") as input_file, destination.open("xb") as output_file:
        shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        output_file.flush()
        os.fsync(output_file.fileno())
    shutil.copystat(source, destination)
    _fsync_directory(destination.parent)


def _fsync_directory(path):
    descriptor = None
    try:
        descriptor = os.open(
            str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        os.fsync(descriptor)
    except OSError:
        # Windows does not generally allow directories to be opened this way.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _durable_unlink(path):
    path = Path(path)
    path.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _transaction_relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("The update transaction contains an unsafe path.")
    relative = PurePosixPath(value)
    if (relative.is_absolute() or not relative.parts or ".." in relative.parts or
            any(
                not part or ":" in part or part != part.rstrip(" .") or
                part.rstrip(" .").split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
                for part in relative.parts
            )):
        raise ValueError("The update transaction contains an unsafe path.")
    return Path(*relative.parts)


def _source_path_is_installable(relative):
    """Return whether a normalized project path belongs to the source payload."""
    return (
        relative.name.casefold() not in EXCLUDED_FILES
        and not any(part.casefold() in EXCLUDED_PARTS for part in relative.parts)
    )


def _validated_source_path_list(values, label, *, allow_excluded=False):
    if not isinstance(values, list):
        raise ValueError(f"The {label} has no valid managed-file list.")
    paths = set()
    folded = set()
    for value in values:
        relative = _transaction_relative(value)
        canonical = relative.as_posix()
        normalized = canonical.casefold()
        if (value != canonical or
                (not allow_excluded and not _source_path_is_installable(relative))
                or normalized in folded):
            raise ValueError(f"The {label} contains an unsafe or duplicate path.")
        paths.add(relative)
        folded.add(normalized)
    return paths


def _installed_source_inventory(install_dir):
    inventory_path = install_dir / SOURCE_INVENTORY_FILE
    if _is_link_or_junction(inventory_path):
        raise ValueError("The installed source-file inventory is an unsafe link.")
    if not inventory_path.exists():
        return None
    if not inventory_path.is_file():
        raise ValueError("The installed source-file inventory is not a regular file.")
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The installed source-file inventory is invalid.") from error
    if (not isinstance(payload, dict) or payload.get("product") != "pyOS"
            or payload.get("version") != 1):
        raise ValueError("The installed source-file inventory is invalid.")
    return _validated_source_path_list(payload.get("files"), "installed source-file inventory")


def _manifest_managed_source_paths(install_dir):
    marker_path = install_dir / INSTALL_OWNER_FILE
    manifest_path = install_dir / INSTALL_MANIFEST_FILE
    if not marker_path.exists() and not manifest_path.exists():
        return None
    # _stage_updated_install_manifest performs the full ownership cross-check
    # before any mutation.  This early read is used only to construct the
    # deletion plan and therefore remains deliberately conservative.
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The installation manifest is invalid; refusing to update.") from error
    if not isinstance(manifest, dict):
        raise ValueError("The installation manifest is invalid; refusing to update.")
    owned = manifest.get("owned_paths")
    path_types = manifest.get("owned_path_types")
    paths = _validated_source_path_list(
        owned, "installation manifest", allow_excluded=True,
    )
    if path_types is not None:
        if not isinstance(path_types, dict):
            raise ValueError("The installation manifest has an invalid owned path type map.")
        typed_files = set()
        for relative in paths:
            kind = path_types.get(relative.as_posix())
            if kind not in {"file", "tree"}:
                raise ValueError("The installation manifest does not type every owned path.")
            if kind == "file":
                typed_files.add(relative)
        paths = typed_files
    return {
        relative for relative in paths
        if _source_path_is_installable(relative)
        and relative.as_posix() not in {INSTALL_OWNER_FILE, INSTALL_MANIFEST_FILE}
    }


def _git_managed_source_paths(install_dir):
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=str(install_dir),
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if top.returncode or Path(top.stdout.strip()).resolve() != install_dir:
            return None
        tracked = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(install_dir), capture_output=True,
            timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    if tracked.returncode:
        return None
    try:
        values = [value.decode("utf-8") for value in tracked.stdout.split(b"\0") if value]
        paths = _validated_source_path_list(
            values, "Git source-file inventory", allow_excluded=True,
        )
    except (UnicodeError, ValueError):
        return None
    return {
        relative for relative in paths
        if _source_path_is_installable(relative) and (install_dir / relative).is_file()
    }


def _previous_managed_source_paths(install_dir):
    inventory = _installed_source_inventory(install_dir)
    if inventory is not None:
        return inventory
    manifest = _manifest_managed_source_paths(install_dir)
    if manifest is not None:
        return manifest
    tracked = _git_managed_source_paths(install_dir)
    if tracked is not None:
        return tracked
    return {
        _transaction_relative(value) for value in KNOWN_SOURCE_FILES
        if (install_dir / _transaction_relative(value)).is_file()
    }


def _stage_source_inventory(source, candidates, ref):
    managed = sorted(
        {candidate.relative_to(source).as_posix() for candidate in candidates}
        | {SOURCE_INVENTORY_FILE}
    )
    inventory_path = source / SOURCE_INVENTORY_FILE
    inventory_path.write_text(json.dumps({
        "product": "pyOS", "version": 1, "ref": ref, "files": managed,
    }, indent=2), encoding="utf-8")
    return sorted(
        [candidate for candidate in candidates if candidate != inventory_path] + [inventory_path],
        key=lambda item: item.relative_to(source).as_posix(),
    )


def _active_transaction_path(data_dir):
    return Path(data_dir) / SOURCE_TRANSACTION_FILE


def _update_directories_overlap(install_dir, data_dir):
    return (
        install_dir == data_dir or install_dir.is_relative_to(data_dir) or
        data_dir.is_relative_to(install_dir)
    )


def _validate_recovery_target(target, entry):
    if not target.exists() and not _is_link_or_junction(target):
        if entry["operation"] == "write" and entry["existed"]:
            raise ValueError(
                f"Recovery target is missing after the interrupted update: {entry['path']}"
            )
        return
    if _is_link_or_junction(target) or not target.is_file():
        raise ValueError(f"Recovery target has an unexpected type: {entry['path']}")
    current_digest = _sha256_file(target)
    allowed = set()
    if entry["operation"] == "write":
        allowed.add(entry["installed_sha256"])
    if entry["existed"]:
        allowed.add(entry["backup_sha256"])
    if current_digest not in allowed:
        raise ValueError(
            f"Recovery target changed after the interrupted update: {entry['path']}"
        )


def _recover_source_transaction(install_dir, data_dir):
    """Rollback a durable, incomplete source transaction under the update lock."""
    active_path = _active_transaction_path(data_dir)
    if _is_link_or_junction(active_path):
        raise ValueError("The active source-update transaction marker is unsafe.")
    if not active_path.exists():
        return None
    if not active_path.is_file():
        raise ValueError("The active source-update transaction marker is unsafe.")
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The active source-update transaction marker is invalid.") from error
    transaction_id = active.get("transaction_id") if isinstance(active, dict) else None
    if (not isinstance(active, dict) or active.get("version") != SOURCE_TRANSACTION_VERSION or
            str(active.get("install_dir") or "") != str(install_dir) or
            not isinstance(transaction_id, str) or
            not re.fullmatch(r"[0-9a-f]{32}", transaction_id)):
        raise ValueError("The active source-update transaction does not match this installation.")
    journal_relative = _transaction_relative(active.get("journal"))
    if active["journal"] != journal_relative.as_posix():
        raise ValueError("The active source-update journal path is not canonical.")
    journal_path = data_dir / journal_relative
    backup_root_path = data_dir / "update_backups"
    if (_is_link_or_junction(backup_root_path) or
            not backup_root_path.resolve(strict=False).is_relative_to(data_dir)):
        raise ValueError("The source-update backup root is an unsafe symbolic link.")
    backup_root = backup_root_path.resolve(strict=False)
    if (_is_link_or_junction(journal_path) or
            not journal_path.resolve(strict=False).is_relative_to(backup_root) or
            journal_path.name != "transaction.json"):
        raise ValueError("The active source-update journal path is unsafe.")
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The active source-update journal is missing or invalid.") from error
    if (not isinstance(journal, dict) or journal.get("version") != SOURCE_TRANSACTION_VERSION or
            journal.get("transaction_id") != active["transaction_id"] or
            str(journal.get("install_dir") or "") != str(install_dir)):
        raise ValueError("The active source-update journal does not match its marker.")
    state = journal.get("state")
    if state in {"committed", "rolled_back", "recovered"}:
        _durable_unlink(active_path)
        return {"state": state, "transaction_id": active["transaction_id"]}
    if state not in {"prepared", "applying", "rollback_failed", "recovery_failed"}:
        raise ValueError("The active source-update journal has an invalid state.")

    raw_entries = journal.get("entries")
    raw_applied = journal.get("applied")
    raw_created_dirs = journal.get("created_dirs", [])
    if (not isinstance(raw_entries, list) or not isinstance(raw_applied, list) or
            not isinstance(raw_created_dirs, list)):
        raise ValueError("The active source-update journal has an invalid plan.")
    entries = {}
    normalized_entry_keys = set()
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("The active source-update journal has an invalid entry.")
        relative = _transaction_relative(raw_entry.get("path"))
        key = relative.as_posix()
        existed = raw_entry.get("existed")
        operation = raw_entry.get("operation", "write")
        installed_digest = str(raw_entry.get("installed_sha256") or "")
        backup_digest = str(raw_entry.get("backup_sha256") or "")
        normalized_key = key.casefold()
        if (raw_entry.get("path") != key or key in entries or
                normalized_key in normalized_entry_keys or
                not isinstance(existed, bool) or
                operation not in {"write", "delete"} or
                (operation == "write" and not SHA256_DIGEST.fullmatch(installed_digest)) or
                (operation == "delete" and (installed_digest or not existed)) or
                (existed and not SHA256_DIGEST.fullmatch(backup_digest)) or
                (not existed and backup_digest)):
            raise ValueError("The active source-update journal has an invalid entry.")
        entries[key] = {
            "path": key, "relative": relative, "existed": existed,
            "operation": operation,
            "installed_sha256": installed_digest.lower(),
            "backup_sha256": backup_digest.lower(),
        }
        normalized_entry_keys.add(normalized_key)
    applied = []
    applied_keys = set()
    for value in raw_applied:
        relative = _transaction_relative(value)
        key = relative.as_posix()
        if value != key or key not in entries or key in applied_keys:
            raise ValueError("The active source-update journal has an invalid applied list.")
        applied.append(entries[key])
        applied_keys.add(key)
    if [entry["path"] for entry in applied] != list(entries)[:len(applied)]:
        raise ValueError("The active source-update journal has a non-sequential applied list.")
    created_relatives = [_transaction_relative(value) for value in raw_created_dirs]
    if (any(raw != relative.as_posix()
            for raw, relative in zip(raw_created_dirs, created_relatives)) or
            len({value.as_posix().casefold() for value in created_relatives}) !=
            len(created_relatives)):
        raise ValueError("The active source-update journal has an invalid directory list.")
    created_dirs = [install_dir / value for value in created_relatives]
    if any(not directory.resolve(strict=False).is_relative_to(install_dir)
           for directory in created_dirs):
        raise ValueError("A recovery directory escapes the installation directory.")

    backup = journal_path.parent.resolve()
    for entry in applied:
        target = install_dir / entry["relative"]
        if not target.parent.resolve(strict=False).is_relative_to(install_dir):
            raise ValueError("A recovery target escapes the installation directory.")
        _validate_recovery_target(target, entry)
        if entry["existed"]:
            saved = backup / entry["relative"]
            if (_is_link_or_junction(saved) or
                    not saved.parent.resolve(strict=False).is_relative_to(backup) or
                    not saved.is_file() or
                    _sha256_file(saved) != entry["backup_sha256"]):
                raise ValueError(f"Recovery backup is missing or corrupt: {entry['path']}")

    rollback_plan = [(entry["relative"], entry["existed"]) for entry in applied]
    errors = _rollback_overlay(rollback_plan, backup, install_dir, created_dirs)
    if errors:
        journal["state"] = "recovery_failed"
        journal["recovery_errors"] = errors
        _atomic_write_json(journal_path, journal)
        raise ValueError("Interrupted source update recovery was incomplete: " + "; ".join(errors))
    journal["state"] = "recovered"
    journal["recovered_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(journal_path, journal)
    _durable_unlink(active_path)
    return {"state": "recovered", "transaction_id": active["transaction_id"]}


def recover_source_update(install_dir, data_dir):
    """Recover an interrupted source overlay, if one is durably journaled."""
    try:
        install_dir = Path(install_dir).expanduser().resolve()
        data_dir = Path(data_dir).expanduser().resolve()
    except RuntimeError as error:
        raise ValueError("An update directory contains an unsafe symbolic-link loop.") from error
    if _update_directories_overlap(install_dir, data_dir):
        raise ValueError("The install and data directories must not overlap during updates.")
    with _update_lock(data_dir):
        return _recover_source_transaction(install_dir, data_dir)


@contextmanager
def _update_lock(data_dir):
    """Hold an OS-backed, non-blocking lock for all update mutations."""
    lock_dir = Path(data_dir).expanduser().resolve()
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".pyos-update.lock"
    if (_is_link_or_junction(lock_path) or
            (lock_path.exists() and not lock_path.is_file())):
        raise ValueError("The pyOS update lock path is unsafe.")
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise ValueError("Another pyOS update is already in progress.") from error
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise ValueError("Another pyOS update is already in progress.") from error
        acquired = True
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n".encode("ascii"))
        handle.flush()
        yield lock_path
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _safe_extract(bundle, destination, progress=None):
    members = bundle.infolist()
    if not members or len(members) > MAX_ARCHIVE_FILES:
        raise ValueError("The update archive has an invalid file count.")
    total_size = 0
    checked = []
    normalized_names = set()
    destination = destination.resolve()
    for member in members:
        if "\\" in member.filename or "\0" in member.filename:
            raise ValueError("The update archive contains an unsafe path.")
        path = PurePosixPath(member.filename)
        if (path.is_absolute() or not path.parts or ".." in path.parts or
                any(
                    ":" in part or part != part.rstrip(" .") or
                    part.rstrip(" .").split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
                    for part in path.parts
                )):
            raise ValueError("The update archive contains an unsafe path.")
        normalized_parts = [part.rstrip(" .").casefold() for part in path.parts]
        normalized_name = "/".join(normalized_parts)
        if (not normalized_name or any(not part for part in normalized_parts) or
                normalized_name in normalized_names):
            raise ValueError("The update archive contains duplicate or ambiguous paths.")
        normalized_names.add(normalized_name)
        # Do not permit ZIP entries marked as Unix symbolic links.
        if ((member.external_attr >> 16) & 0o170000) == 0o120000:
            raise ValueError("The update archive contains an unsupported symbolic link.")
        if member.file_size < 0 or member.file_size > MAX_ARCHIVE_BYTES:
            raise ValueError("The update archive contains an oversized file.")
        total_size += member.file_size
        if total_size > MAX_ARCHIVE_BYTES:
            raise ValueError("The expanded update archive exceeds the 1 GB safety limit.")
        target = destination.joinpath(*path.parts)
        if not target.resolve(strict=False).is_relative_to(destination):
            raise ValueError("The update archive contains an unsafe path.")
        checked.append((member, target))
    for index, (member, target) in enumerate(checked, 1):
        if member.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
        _report_progress(progress, "Extracting update", index, len(checked))


def _rollback_overlay(applied, backup, install_dir, created_dirs=()):
    errors = []
    for relative, existed in reversed(applied):
        target = install_dir / relative
        temporary = target.with_name(f".{target.name}.rollback-{uuid.uuid4().hex}")
        try:
            if existed:
                saved = backup / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                _copy_durable(saved, temporary)
                os.replace(temporary, target)
                _fsync_directory(target.parent)
            else:
                target.unlink(missing_ok=True)
                _fsync_directory(target.parent)
        except OSError as error:
            errors.append(f"{relative}: {error}")
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                errors.append(f"{relative} temporary cleanup: {error}")
    # Remove directories created only for new files, but never recursively.
    new_parents = set(created_dirs)
    for directory in sorted(new_parents, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    return errors


def _stage_updated_install_manifest(source, candidates, install_dir, ref, retired_paths=()):
    """Keep Setup's uninstall manifest in sync with files added by an update."""
    marker_path = install_dir / INSTALL_OWNER_FILE
    manifest_path = install_dir / INSTALL_MANIFEST_FILE
    if not marker_path.exists() and not manifest_path.exists():
        return candidates
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("The installation ownership metadata is invalid; refusing to update.") from error
    if not isinstance(marker, dict) or not isinstance(manifest, dict):
        raise ValueError("The installation ownership metadata is invalid; refusing to update.")
    installation_id = marker.get("installation_id")
    try:
        parsed_installation_id = uuid.UUID(str(installation_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("The installation ownership marker has an invalid identifier.") from error
    if (marker.get("product") != "pyOS" or manifest.get("product") != "pyOS" or
            parsed_installation_id.version != 4 or str(parsed_installation_id) != installation_id or
            manifest.get("installation_id") != installation_id or
            marker.get("schema_version") != 2 or manifest.get("schema_version") != 2):
        raise ValueError("The installation ownership marker does not match its manifest.")
    try:
        if Path(manifest["install_dir"]).expanduser().resolve() != install_dir:
            raise ValueError("The installation manifest belongs to a different directory.")
    except (KeyError, TypeError, OSError, RuntimeError) as error:
        raise ValueError("The installation manifest has no valid installation root.") from error
    owned = manifest.get("owned_paths")
    if not isinstance(owned, list):
        raise ValueError("The installation manifest has no owned path list.")
    external_files = manifest.get("external_files", [])
    if (not isinstance(external_files, list) or
            not all(isinstance(value, str) for value in external_files)):
        raise ValueError("The installation manifest has an invalid external file list.")
    normalized_owned = set()
    for value in owned:
        if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
            raise ValueError("The installation manifest contains an unsafe owned path.")
        relative = PurePosixPath(value)
        if (relative.is_absolute() or ".." in relative.parts or not relative.parts or
                any(
                    ":" in part or part != part.rstrip(" .") or
                    part.rstrip(" .").split(".", 1)[0].casefold() in WINDOWS_RESERVED_NAMES
                    for part in relative.parts
                )):
            raise ValueError("The installation manifest contains an unsafe owned path.")
        normalized_owned.add(relative.as_posix())
    if not {INSTALL_OWNER_FILE, INSTALL_MANIFEST_FILE}.issubset(normalized_owned):
        raise ValueError("The installation manifest does not own its metadata files.")
    raw_types = manifest.get("owned_path_types")
    if raw_types is None:
        path_types = {value: "file" for value in normalized_owned}
    elif not isinstance(raw_types, dict):
        raise ValueError("The installation manifest has an invalid owned path type map.")
    else:
        path_types = {}
        for value, kind in raw_types.items():
            relative = PurePosixPath(str(value)).as_posix()
            if (relative not in normalized_owned or kind not in {"file", "tree"} or
                    (kind == "tree" and relative != ".venv")):
                raise ValueError("The installation manifest has an invalid owned path type.")
            path_types[relative] = kind
        if set(path_types) != normalized_owned:
            raise ValueError("The installation manifest does not type every owned path.")
    new_files = {candidate.relative_to(source).as_posix() for candidate in candidates}
    retired_files = {Path(value).as_posix() for value in retired_paths}
    normalized_owned.difference_update(retired_files)
    for value in retired_files:
        path_types.pop(value, None)
    normalized_owned.update(new_files)
    normalized_owned.update({INSTALL_OWNER_FILE, INSTALL_MANIFEST_FILE})
    path_types.update({value: "file" for value in new_files})
    path_types[INSTALL_OWNER_FILE] = "file"
    path_types[INSTALL_MANIFEST_FILE] = "file"
    manifest["owned_paths"] = sorted(normalized_owned)
    manifest["owned_path_types"] = {
        value: path_types[value] for value in sorted(normalized_owned)
    }
    manifest["last_update_ref"] = ref
    manifest["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    staged_manifest = source / INSTALL_MANIFEST_FILE
    staged_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return sorted(
        [*candidates, staged_manifest],
        key=lambda item: item.relative_to(source).as_posix(),
    )


def install_source_update(update, install_dir, data_dir, progress=None):
    """Verify, stage, and transactionally overlay a pinned source update."""
    expected_digest = _required_sha256(update)
    ref = _required_immutable_ref(update)
    _validate_archive_reference(update, ref)
    _validate_source_release_binding(ref, expected_digest)
    try:
        install_dir = Path(install_dir).expanduser().resolve()
        data_dir = Path(data_dir).expanduser().resolve()
    except RuntimeError as error:
        raise ValueError("An update directory contains an unsafe symbolic-link loop.") from error
    if _update_directories_overlap(install_dir, data_dir):
        raise ValueError("The install and data directories must not overlap during updates.")

    with _update_lock(data_dir):
        recovery = _recover_source_transaction(install_dir, data_dir)
        if not (install_dir / "pyOSgui.py").is_file():
            raise ValueError("The configured installation folder does not contain pyOSgui.py.")
        work = Path(tempfile.mkdtemp(prefix="pyos-update-"))
        applied = []
        try:
            archive = work / "update.zip"
            checksum = _download(update["archive_url"], archive, progress)
            if not hmac.compare_digest(checksum.casefold(), expected_digest):
                raise ValueError("The source archive checksum does not match trusted update metadata.")

            extracted = work / "source"
            extracted.mkdir()
            try:
                with zipfile.ZipFile(archive) as bundle:
                    _safe_extract(bundle, extracted, progress)
            except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                raise ValueError("The downloaded source update is not a valid ZIP archive.") from error
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            source = roots[0] if len(roots) == 1 else extracted
            if any(not (source / name).is_file() for name in REQUIRED_SOURCE_FILES):
                raise ValueError("The archive is not a recognizable pyOS update.")

            candidates = sorted(
                (candidate for candidate in source.rglob("*")
                 if candidate.is_file()
                 and candidate.name.casefold() not in EXCLUDED_FILES
                 and not any(part.casefold() in EXCLUDED_PARTS
                             for part in candidate.relative_to(source).parts)),
                key=lambda item: item.relative_to(source).as_posix(),
            )
            if not candidates:
                raise ValueError("The source update contains no installable files.")
            candidates = _stage_source_inventory(source, candidates, ref)
            previous_managed = _previous_managed_source_paths(install_dir)
            next_managed = {candidate.relative_to(source) for candidate in candidates}
            retired = sorted(
                previous_managed - next_managed,
                key=lambda item: item.as_posix(),
            )
            candidates = _stage_updated_install_manifest(
                source, candidates, install_dir, ref, retired_paths=retired,
            )
            for candidate in candidates:
                relative = candidate.relative_to(source)
                target = install_dir / relative
                if not target.resolve(strict=False).is_relative_to(install_dir):
                    raise ValueError("An update target escapes the installation directory.")
                if _is_link_or_junction(target) or (target.exists() and not target.is_file()):
                    raise ValueError("An update target is not a regular installation file.")
                if (target.is_file() and relative not in previous_managed
                        and relative.as_posix() != INSTALL_MANIFEST_FILE):
                    raise ValueError(
                        f"The update would overwrite an unowned local file: {relative}"
                    )
            retired_existing = []
            for relative in retired:
                target = install_dir / relative
                if not target.resolve(strict=False).is_relative_to(install_dir):
                    raise ValueError("A retired update target escapes the installation directory.")
                if _is_link_or_junction(target) or (target.exists() and not target.is_file()):
                    raise ValueError("A retired update target is not a regular installation file.")
                if target.is_file():
                    retired_existing.append(relative)
            operations = [
                {"operation": "write", "relative": candidate.relative_to(source),
                 "candidate": candidate}
                for candidate in candidates
            ] + [
                {"operation": "delete", "relative": relative, "candidate": None}
                for relative in retired_existing
            ]

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            transaction_id = uuid.uuid4().hex
            backup_root = data_dir / "update_backups"
            if (_is_link_or_junction(backup_root) or
                    not backup_root.resolve(strict=False).is_relative_to(data_dir)):
                raise ValueError("The source-update backup root is an unsafe symbolic link.")
            backup = backup_root / f"{stamp}-{transaction_id[:12]}"
            backup.mkdir(parents=True, exist_ok=False)
            if not backup.resolve().is_relative_to(data_dir):
                raise ValueError("The source-update backup path escapes the data directory.")
            existing = {}
            created_dirs = set()
            entries = []
            # Finish every backup before the first installation mutation.
            for operation in operations:
                relative = operation["relative"]
                candidate = operation["candidate"]
                target = install_dir / relative
                existed = target.is_file()
                existing[relative] = existed
                if operation["operation"] == "write":
                    created_dirs.update(
                        parent for parent in target.parents
                        if (parent != install_dir and parent.is_relative_to(install_dir)
                            and not parent.exists())
                    )
                backup_digest = ""
                if existed:
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    _copy_durable(target, saved)
                    backup_digest = _sha256_file(saved)
                entries.append({
                    "path": relative.as_posix(),
                    "operation": operation["operation"],
                    "existed": existed,
                    "installed_sha256": (
                        _sha256_file(candidate) if operation["operation"] == "write" else ""
                    ),
                    "backup_sha256": backup_digest,
                })

            journal_path = backup / "transaction.json"
            active_path = _active_transaction_path(data_dir)
            entries_by_path = {entry["path"]: entry for entry in entries}
            journal = {
                "version": SOURCE_TRANSACTION_VERSION,
                "transaction_id": transaction_id,
                "state": "prepared",
                "install_dir": str(install_dir),
                "ref": ref,
                "sha256": checksum,
                "entries": entries,
                "applied": [],
                "created_dirs": sorted(
                    directory.relative_to(install_dir).as_posix() for directory in created_dirs
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_write_json(journal_path, journal)
            _atomic_write_json(active_path, {
                "version": SOURCE_TRANSACTION_VERSION,
                "transaction_id": transaction_id,
                "install_dir": str(install_dir),
                "journal": journal_path.relative_to(data_dir).as_posix(),
            })

            try:
                journal["state"] = "applying"
                for index, operation in enumerate(operations, 1):
                    relative = operation["relative"]
                    candidate = operation["candidate"]
                    target = install_dir / relative
                    entry = entries_by_path[relative.as_posix()]
                    if not target.parent.resolve(strict=False).is_relative_to(install_dir):
                        raise ValueError("An update target parent escaped the installation directory.")
                    if existing[relative]:
                        if (_is_link_or_junction(target) or not target.is_file()
                                or _sha256_file(target) != entry["backup_sha256"]):
                            raise ValueError(
                                f"An installation file changed while the update was staged: {relative}"
                            )
                    elif target.exists() or _is_link_or_junction(target):
                        raise ValueError(
                            f"A new update target appeared while the update was staged: {relative}"
                        )
                    # Persist intent before mutation. Recovery is therefore safe
                    # whether a crash happens just before or just after replace.
                    journal["applied"].append(relative.as_posix())
                    _atomic_write_json(journal_path, journal)
                    applied.append((relative, existing[relative]))
                    if operation["operation"] == "delete":
                        target.unlink()
                        _fsync_directory(target.parent)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(f".{target.name}.update-{uuid.uuid4().hex}")
                        try:
                            _copy_durable(candidate, temporary)
                            os.replace(temporary, target)
                            _fsync_directory(target.parent)
                        finally:
                            temporary.unlink(missing_ok=True)
                    _report_progress(progress, "Installing files", index, len(operations))
                written = {
                    operation["relative"] for operation in operations
                    if operation["operation"] == "write"
                }
                copied = [
                    relative.as_posix() for relative, _existed in applied
                    if relative in written
                ]
                _atomic_write_json(backup / "update.json", {
                    "update": update, "sha256": checksum, "files": copied,
                    "replaced_files": [
                        relative.as_posix() for relative, existed in applied
                        if existed and relative in written
                    ],
                    "new_files": [
                        relative.as_posix() for relative, existed in applied
                        if not existed and relative in written
                    ],
                    "retired_files": [relative.as_posix() for relative in retired_existing],
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                    "transaction_id": transaction_id,
                })
                journal["state"] = "committed"
                journal["committed_at"] = datetime.now(timezone.utc).isoformat()
                _atomic_write_json(journal_path, journal)
                _durable_unlink(active_path)
            except BaseException as install_error:
                rollback_errors = _rollback_overlay(applied, backup, install_dir, created_dirs)
                if rollback_errors:
                    journal["state"] = "rollback_failed"
                    journal["rollback_errors"] = rollback_errors
                    try:
                        _atomic_write_json(journal_path, journal)
                    except OSError:
                        pass
                    details = "; ".join(rollback_errors)
                    raise ValueError(
                        f"Update failed and rollback was incomplete: {details}"
                    ) from install_error
                journal["state"] = "rolled_back"
                journal["rolled_back_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    _atomic_write_json(journal_path, journal)
                    _durable_unlink(active_path)
                except OSError as journal_error:
                    raise ValueError(
                        "The update was rolled back but its recovery journal could not be finalized."
                    ) from journal_error
                if isinstance(install_error, (OSError, ValueError, KeyboardInterrupt, SystemExit)):
                    raise
                raise ValueError("The source update failed and was rolled back.") from install_error

            return {
                "backup": str(backup), "files": len(copied), "sha256": checksum,
                "transaction_id": transaction_id, "recovery": recovery,
            }
        finally:
            shutil.rmtree(work, ignore_errors=True)


def executable_asset(update):
    """Return a trusted-metadata candidate Windows executable asset."""
    for asset in update.get("assets", []):
        if asset.get("name", "").casefold() in {"pyos.exe", "pyos-windows.exe"}:
            return asset
    return None


def _trusted_authenticode_thumbprints():
    trusted = {
        str(value).replace(" ", "").upper()
        for value in TRUSTED_AUTHENTICODE_THUMBPRINTS
        if CERTIFICATE_THUMBPRINT.fullmatch(str(value).replace(" ", ""))
    }
    if not trusted:
        raise ValueError(
            "No official pyOS Authenticode signer is pinned; refusing the executable update."
        )
    return trusted


def _validate_authenticode(path):
    """Require a valid, chain-trusted Authenticode signature on Windows."""
    trusted_thumbprints = _trusted_authenticode_thumbprints()
    if os.name != "nt":
        raise ValueError("Authenticode verification is unavailable on this platform.")
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        raise ValueError("Authenticode verification is unavailable; refusing the executable update.")
    environment = os.environ.copy()
    environment["PYOS_AUTHENTICODE_TARGET"] = str(Path(path).resolve())
    script = (
        "$signature=Get-AuthenticodeSignature -LiteralPath $env:PYOS_AUTHENTICODE_TARGET;"
        "$certificate=$signature.SignerCertificate;"
        "[pscustomobject]@{Status=[string]$signature.Status;"
        "Subject=if($certificate){[string]$certificate.Subject}else{''};"
        "Thumbprint=if($certificate){[string]$certificate.Thumbprint}else{''}}|"
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30, env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        details = json.loads(result.stdout.strip()) if result.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        raise ValueError("Authenticode verification failed; refusing the executable update.") from error
    thumbprint = (
        str(details.get("Thumbprint") or "").replace(" ", "").upper()
        if isinstance(details, dict) else ""
    )
    if (not isinstance(details, dict) or details.get("Status") != "Valid" or
            not details.get("Subject") or thumbprint not in trusted_thumbprints):
        raise ValueError("The executable does not have a valid trusted Authenticode signature.")
    details["Thumbprint"] = thumbprint
    return details


def stage_executable_update(asset, data_dir, progress=None):
    """Download and validate an immutable, signed official pyOS executable."""
    if not asset or not asset.get("url"):
        raise ValueError("The release does not contain a downloadable executable.")
    expected_digest = _required_sha256(asset)
    ref = _required_immutable_ref(asset)
    _validate_executable_reference(asset, ref)
    _trusted_authenticode_thumbprints()
    try:
        data_dir = Path(data_dir).expanduser().resolve()
    except RuntimeError as error:
        raise ValueError("The update data directory contains an unsafe symbolic-link loop.") from error
    with _update_lock(data_dir):
        updates = data_dir / "pending_updates"
        updates.mkdir(parents=True, exist_ok=True)
        update_id = uuid.uuid4().hex
        staged = updates / f"pyOS-update-{ref[:16]}-{update_id}.exe"
        temporary = updates / f".{staged.name}.tmp"
        try:
            checksum = _download(asset["url"], temporary, progress)
            if not hmac.compare_digest(checksum.casefold(), expected_digest):
                raise ValueError("The executable checksum does not match trusted release metadata.")
            _report_progress(progress, "Validating executable", None, None)
            with temporary.open("rb") as executable:
                header = executable.read(2)
            if temporary.stat().st_size < 1024 * 1024 or header != b"MZ":
                raise ValueError("The downloaded release asset is not a valid Windows executable.")
            signature = _validate_authenticode(temporary)
            os.replace(temporary, staged)
            return {
                "path": str(staged), "sha256": checksum, "signature": signature,
                "ref": ref, "update_id": update_id,
            }
        finally:
            temporary.unlink(missing_ok=True)
