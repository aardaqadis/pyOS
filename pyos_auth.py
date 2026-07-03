"""Shared username and password authentication for pyOS."""

import hashlib
import hmac
import json
import secrets
from pathlib import Path

from pyos_config import get_data_dir, load_config


PBKDF2_ITERATIONS = 310_000


def credentials_path():
    config = load_config()
    if config.get("configured"):
        return get_data_dir() / "credentials.json"
    return Path.home() / ".pyos_credentials.json"


def load_credentials():
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        required = {"username", "salt", "password_hash", "iterations"}
        return data if isinstance(data, dict) and required.issubset(data) else None
    except (OSError, ValueError, TypeError):
        return None


def has_account():
    return load_credentials() is not None


def get_username():
    credentials = load_credentials()
    return credentials["username"] if credentials else None


def validate_account(username, password):
    username = username.strip()
    if len(username) < 3:
        return "Username must contain at least 3 characters."
    if len(username) > 32:
        return "Username must contain no more than 32 characters."
    if any(character in username for character in "\r\n\t"):
        return "Username contains invalid characters."
    if len(password) < 8:
        return "Password must contain at least 8 characters."
    return None


def create_account(username, password):
    error = validate_account(username, password)
    if error:
        raise ValueError(error)
    username = username.strip()
    salt = secrets.token_bytes(32)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    data = {
        "username": username,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
        "iterations": PBKDF2_ITERATIONS,
        "algorithm": "pbkdf2-sha256",
    }
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)
    return username


def verify_password(password, username=None):
    credentials = load_credentials()
    if not credentials:
        return False
    if username is not None and not hmac.compare_digest(
        username.strip().casefold(), credentials["username"].casefold()
    ):
        return False
    try:
        salt = bytes.fromhex(credentials["salt"])
        expected = bytes.fromhex(credentials["password_hash"])
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            int(credentials["iterations"]),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False
