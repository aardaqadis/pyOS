"""Consent-first GitHub update support for pyOS."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPOSITORY = "aardaqadis/pyOS"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
USER_AGENT = "pyOS-Updater/1.0"
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
EXCLUDED_PARTS = {".git", ".github", ".idea", ".venv", "venv", "__pycache__", "build", "dist"}


def _json_request(url):
    request = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


def latest_update(channel):
    """Return normalized metadata for the stable release or newest main commit."""
    if channel == "stable":
        release = _json_request(f"{API_ROOT}/releases/latest")
        assets = [
            {"name": item.get("name", ""), "url": item.get("browser_download_url", ""),
             "size": int(item.get("size", 0)), "digest": str(item.get("digest") or "")}
            for item in release.get("assets", []) if isinstance(item, dict)
        ]
        return {
            "channel": "stable", "ref": str(release["tag_name"]),
            "name": str(release.get("name") or release["tag_name"]),
            "notes": str(release.get("body") or "No release notes supplied."),
            "date": str(release.get("published_at") or ""),
            "archive_url": str(release["zipball_url"]), "assets": assets,
            "page_url": str(release.get("html_url") or ""),
        }
    if channel == "unstable":
        repository = _json_request(API_ROOT)
        branch = str(repository.get("default_branch") or "main")
        commit = _json_request(f"{API_ROOT}/commits/{branch}")
        sha = str(commit["sha"])
        details = commit.get("commit", {})
        return {
            "channel": "unstable", "ref": sha,
            "name": f"Commit {sha[:8]}",
            "notes": str(details.get("message") or "No commit message supplied."),
            "date": str(details.get("committer", {}).get("date") or ""),
            "archive_url": f"https://github.com/{REPOSITORY}/archive/{sha}.zip",
            "assets": [], "page_url": f"https://github.com/{REPOSITORY}/commit/{sha}",
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
        return value if result.returncode == 0 and len(value) == 40 else ""
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


def _download(url, destination, progress=None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
        try:
            expected = int(response.headers.get("Content-Length", 0)) or None
        except (TypeError, ValueError):
            expected = None
        _report_progress(progress, "Downloading update", 0, expected)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError("The update archive exceeds the 1 GB safety limit.")
            digest.update(chunk)
            output.write(chunk)
            _report_progress(progress, "Downloading update", total, expected)
    return digest.hexdigest()


def install_source_update(update, install_dir, data_dir, progress=None):
    """Download, validate, back up, and overlay a GitHub source archive."""
    install_dir, data_dir = Path(install_dir).resolve(), Path(data_dir).resolve()
    if not (install_dir / "pyOSgui.py").is_file():
        raise ValueError("The configured installation folder does not contain pyOSgui.py.")
    work = Path(tempfile.mkdtemp(prefix="pyos-update-"))
    try:
        archive = work / "update.zip"
        checksum = _download(update["archive_url"], archive, progress)
        extracted = work / "source"
        extracted.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if not members or len(members) > 10000:
                raise ValueError("The update archive has an invalid file count.")
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("The update archive contains an unsafe path.")
                if member.file_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("The update archive contains an oversized file.")
            for index, member in enumerate(members, 1):
                bundle.extract(member, extracted)
                _report_progress(progress, "Extracting update", index, len(members))
        roots = [item for item in extracted.iterdir() if item.is_dir()]
        source = roots[0] if len(roots) == 1 else extracted
        if not (source / "pyOSgui.py").is_file() or not (source / "setup.py").is_file():
            raise ValueError("The archive is not a recognizable pyOS update.")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = data_dir / "update_backups" / stamp
        copied = []
        candidates = [
            candidate for candidate in source.rglob("*")
            if candidate.is_file()
            and not any(part in EXCLUDED_PARTS for part in candidate.relative_to(source).parts)
        ]
        for index, candidate in enumerate(candidates, 1):
            relative = candidate.relative_to(source)
            target = install_dir / relative
            if target.exists():
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".update-tmp")
            shutil.copy2(candidate, temporary)
            os.replace(temporary, target)
            copied.append(str(relative))
            _report_progress(progress, "Installing files", index, len(candidates))
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "update.json").write_text(json.dumps({
            "update": update, "sha256": checksum, "files": copied,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }, indent=2), encoding="utf-8")
        return {"backup": str(backup), "files": len(copied), "sha256": checksum}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def executable_asset(update):
    """Return a Windows executable release asset, if the release publishes one."""
    for asset in update.get("assets", []):
        if asset.get("name", "").casefold() in {"pyos.exe", "pyos-windows.exe"}:
            return asset
    return None


def stage_executable_update(asset, data_dir, progress=None):
    """Download and validate a future official pyOS.exe release asset."""
    if not asset or not asset.get("url"):
        raise ValueError("The release does not contain a downloadable executable.")
    updates = Path(data_dir).resolve() / "pending_updates"
    updates.mkdir(parents=True, exist_ok=True)
    staged = updates / "pyOS-update.exe"
    checksum = _download(asset["url"], staged, progress)
    _report_progress(progress, "Validating executable", None, None)
    if staged.stat().st_size < 1024 * 1024 or staged.read_bytes()[:2] != b"MZ":
        staged.unlink(missing_ok=True)
        raise ValueError("The downloaded release asset is not a valid Windows executable.")
    expected = asset.get("digest", "")
    if expected.startswith("sha256:") and checksum.casefold() != expected[7:].casefold():
        staged.unlink(missing_ok=True)
        raise ValueError("The executable checksum does not match GitHub's release digest.")
    return {"path": str(staged), "sha256": checksum}
