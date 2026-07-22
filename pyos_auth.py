"""Persistent local authentication shared by the pyOS GUI and CLI."""

import hashlib
import hmac
import json
import secrets
import base64
import sys
import re
import unicodedata
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from pathlib import Path

import pyos_config as storage
from pyos_config import (
    get_data_dir,
    get_standalone_root,
    load_config,
    register_owned_path,
    set_active_user,
)


PBKDF2_ITERATIONS = 600_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000
PASSKEY_RP_ID = "pyos.local"
PASSKEY_ORIGIN = "https://pyos.local"
_active_username = None


class CredentialStoreError(RuntimeError):
    """Raised when an existing credential store cannot be trusted."""


def _username_display(username):
    """Return the canonical display form stored in the database."""
    return unicodedata.normalize("NFKC", str(username).strip())


def _username_key(username):
    """Return the Unicode-normalized, case-insensitive identity key."""
    return _username_display(username).casefold()


def credentials_path():
    """Return the permanent credential-store location."""
    load_config()  # A malformed configuration must fail before choosing a store.
    root = get_data_dir()
    path = root / "credentials.json"
    for owned in (path, path.with_name(path.name + ".bak"),
                  path.with_name("remembered_session.json")):
        register_owned_path(owned, root)
    return path


def remembered_session_path():
    return credentials_path().with_name("remembered_session.json")


def clear_remembered_session():
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        changed = False
        for account in database["accounts"]:
            changed = bool(account.pop("remember_token_hash", None)) or changed
        if changed:
            _save_database_unlocked(path, database, current=database)
    session_path = remembered_session_path()
    with storage._path_lock(session_path):
        try:
            session_path.unlink()
        except OSError:
            pass


def create_remembered_session():
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        data = _find_account(database, _active_username)
        if not data:
            return
        token = secrets.token_urlsafe(48)
        data["remember_token_hash"] = hashlib.sha256(token.encode("utf-8")).hexdigest()
        _save_database_unlocked(path, database, current=database)
        username = data["username"]
    storage.atomic_write_json(
        remembered_session_path(), {"username": username, "token": token}, mode=0o600
    )


def remembered_username():
    try:
        session = json.loads(remembered_session_path().read_text(encoding="utf-8"))
        data = load_credentials(str(session["username"]))
        expected = data.get("remember_token_hash") if data else None
        if not expected:
            return None
        actual = hashlib.sha256(str(session["token"]).encode()).hexdigest()
        if (hmac.compare_digest(actual, expected) and
                _username_key(session["username"]) == _username_key(data["username"])):
            return data["username"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def validate_account(username, password):
    username = _username_display(username)
    if not 3 <= len(username) <= 32:
        return "Username must contain between 3 and 32 characters."
    if any(character in username for character in "\r\n\t"):
        return "Username contains invalid characters."
    if len(password) < 8:
        return "Password must contain at least 8 characters."
    if len(password) > 256:
        return "Password must contain no more than 256 characters."
    return None


def _valid_account(data):
    try:
        if not isinstance(data, dict):
            return None
        required = {"username", "salt", "password_hash", "iterations", "algorithm"}
        if not required.issubset(data) or data["algorithm"] != "pbkdf2-sha256":
            return None
        if not isinstance(data["username"], str):
            return None
        username = _username_display(data["username"])
        if not 3 <= len(username) <= 32 or any(character in username for character in "\r\n\t"):
            return None
        iterations = int(data["iterations"])
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            return None
        salt = bytes.fromhex(data["salt"])
        password_hash = bytes.fromhex(data["password_hash"])
        if len(salt) != 32 or len(password_hash) != 32:
            return None
        account = dict(data)
        account["username"] = username
        if "role" in account and account["role"] not in {"admin", "standard"}:
            return None
        account["role"] = account.get("role") or "standard"
        profile_id = str(account.get("profile_id") or hashlib.sha256(
            _username_key(username).encode("utf-8")
        ).hexdigest()[:24])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile_id):
            return None
        account["profile_id"] = profile_id
        remember_hash = account.get("remember_token_hash")
        if remember_hash is not None and (
                not isinstance(remember_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", remember_hash)):
            return None
        if "passkey_user_id" in account:
            value = account["passkey_user_id"]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                return None
        if "passkeys" in account:
            if not isinstance(account["passkeys"], list):
                return None
            for passkey in account["passkeys"]:
                if not isinstance(passkey, dict) or not isinstance(passkey.get("credential_data"), str):
                    return None
                try:
                    base64.b64decode(passkey["credential_data"], validate=True)
                except (ValueError, TypeError):
                    return None
        return account
    except (ValueError, TypeError):
        return None


def _validate_database(raw):
    """Validate a database as a whole; never silently discard bad accounts."""
    migrated = False
    if isinstance(raw, dict) and isinstance(raw.get("accounts"), list):
        if raw.get("version", 2) != 2:
            raise CredentialStoreError("Unsupported credential database version.")
        accounts = []
        for item in raw["accounts"]:
            account = _valid_account(item)
            if account is None or item.get("role") not in {"admin", "standard"}:
                raise CredentialStoreError("Credential database contains an invalid account.")
            accounts.append(account)
        database = {"version": 2, "accounts": accounts}
        migrated = database != raw
    else:
        legacy = _valid_account(raw)
        if legacy is None:
            raise CredentialStoreError("Credential database has an invalid format.")
        legacy["role"] = "admin"
        database = {"version": 2, "accounts": [legacy]}
        migrated = True
    keys = [_username_key(account["username"]) for account in database["accounts"]]
    if len(keys) != len(set(keys)):
        raise CredentialStoreError("Credential database contains duplicate usernames.")
    # An empty database is valid only as an in-memory value returned when no
    # credential file or backup has ever existed.  If this representation is
    # found on disk, treating it as first-run would let corruption/account
    # deletion reopen administrator enrollment.
    if not database["accounts"]:
        raise CredentialStoreError("Credential database contains no accounts.")
    if not any(account["role"] == "admin" for account in database["accounts"]):
        raise CredentialStoreError("Credential database has no administrator account.")
    return database, migrated


def _read_database(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as error:
        raise CredentialStoreError(f"Credential database is unreadable: {path}") from error
    return _validate_database(raw)


def _legacy_database_sources(target):
    if not storage.MIGRATE_LEGACY_STATE:
        return []
    candidates = [
        get_standalone_root(create=False) / "credentials.json",
        Path(storage.LEGACY_HOME) / ".pyos_credentials.json",
    ]
    resolved_target = Path(target).resolve(strict=False)
    unique = []
    for candidate in candidates:
        if candidate.resolve(strict=False) != resolved_target and candidate not in unique:
            unique.append(candidate)
    return unique


def _migrate_legacy_database_unlocked(target):
    for source in _legacy_database_sources(target):
        if not source.exists():
            continue
        try:
            database, _migrated = _read_database(source)
        except CredentialStoreError as primary_error:
            source_backup = source.with_name(source.name + ".bak")
            if not source_backup.exists():
                raise CredentialStoreError(
                    f"Legacy credential database is malformed: {source}"
                ) from primary_error
            try:
                database, _migrated = _read_database(source_backup)
            except CredentialStoreError as backup_error:
                raise CredentialStoreError(
                    f"Legacy credential database and backup are malformed: {source}"
                ) from backup_error
        _save_database_unlocked(target, database)
        for session_source in (
            source.with_name("remembered_session.json"),
            Path(storage.LEGACY_HOME) / "remembered_session.json",
        ):
            session_target = Path(target).with_name("remembered_session.json")
            if session_source.is_file() and not session_target.exists():
                try:
                    with storage._path_lock(session_target):
                        storage._atomic_write_bytes_unlocked(
                            session_target, session_source.read_bytes(), mode=0o600
                        )
                except OSError:
                    pass
                break
        return database
    return None


def _load_database_unlocked(path):
    path = Path(path)
    backup = path.with_name(path.name + ".bak")
    if not path.exists():
        if backup.exists():
            try:
                database, _migrated = _read_database(backup)
            except CredentialStoreError as error:
                raise CredentialStoreError(
                    f"Credential backup is malformed: {backup}"
                ) from error
            storage._atomic_write_json_unlocked(path, database, mode=0o600)
            return database
        migrated = _migrate_legacy_database_unlocked(path)
        return migrated if migrated is not None else {"version": 2, "accounts": []}
    try:
        database, migrated = _read_database(path)
    except CredentialStoreError as primary_error:
        if not backup.exists():
            raise CredentialStoreError(
                f"Credential database is malformed and no valid backup is available: {path}"
            ) from primary_error
        try:
            database, _migrated = _read_database(backup)
        except CredentialStoreError as backup_error:
            raise CredentialStoreError(
                f"Credential database and backup are malformed: {path}"
            ) from backup_error
        storage._atomic_write_json_unlocked(path, database, mode=0o600)
        return database
    if migrated:
        _save_database_unlocked(path, database, current=database)
    return database


def _load_database():
    """Load or safely migrate the account database under an inter-process lock."""
    path = credentials_path()
    with storage._path_lock(path):
        return _load_database_unlocked(path)


def load_credentials(username=None):
    accounts = _load_database()["accounts"]
    selected = username or _active_username
    if selected:
        key = _username_key(selected)
        return next((item for item in accounts if _username_key(item["username"]) == key), None)
    return accounts[0] if len(accounts) == 1 else None


def list_accounts():
    return [{"username": item["username"], "role": item["role"]}
            for item in _load_database()["accounts"]]


def has_account():
    return bool(_load_database()["accounts"])


def get_username():
    return _active_username


def get_role(username=None):
    credentials = load_credentials(username)
    return credentials.get("role") if credentials else None


def is_admin(username=None):
    return get_role(username) == "admin"


def _save_database_unlocked(path, data, current=None):
    database, _migrated = _validate_database(data)
    path = Path(path)
    backup = Path(path).with_name(Path(path).name + ".bak")
    previous = None
    if path.exists():
        try:
            previous, _ = _read_database(path)
        except CredentialStoreError:
            previous = None
    if previous is None and current is not None:
        previous, _ = _validate_database(current)
    if previous is not None:
        storage._atomic_write_json_unlocked(backup, previous, mode=0o600)
    elif not backup.exists():
        storage._atomic_write_json_unlocked(backup, database, mode=0o600)
    storage._atomic_write_json_unlocked(path, database, mode=0o600)
    return database


def _save_database(data):
    path = credentials_path()
    with storage._path_lock(path):
        current = _load_database_unlocked(path)
        _save_database_unlocked(path, data, current=current)


def _find_account(database, username):
    if username is None:
        return None
    key = _username_key(username)
    return next((account for account in database["accounts"]
                 if _username_key(account["username"]) == key), None)


def _save_account(data):
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        key = _username_key(data["username"])
        for index, account in enumerate(database["accounts"]):
            if _username_key(account["username"]) == key:
                database["accounts"][index] = data
                break
        else:
            database["accounts"].append(data)
        _save_database_unlocked(path, database, current=database)


def _update_account(username, updater):
    """Apply an account mutation while holding the database lock."""
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        account = _find_account(database, username)
        if account is None:
            raise CredentialStoreError("The account no longer exists.")
        updated = updater(dict(account))
        if not isinstance(updated, dict) or _username_key(updated.get("username", "")) != _username_key(
                account["username"]):
            raise CredentialStoreError("Account update attempted to change its identity.")
        database["accounts"][database["accounts"].index(account)] = updated
        _save_database_unlocked(path, database, current=database)
        return updated


def _write_account(username, password, preserve_passkeys=False, role="standard", profile_id=None):
    salt = secrets.token_bytes(32)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    previous = load_credentials(_active_username) if preserve_passkeys else None
    data = {
        "version": 1,
        "username": _username_display(username),
        "algorithm": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
        "role": "admin" if role == "admin" else "standard",
        "profile_id": profile_id or (previous.get("profile_id") if previous else secrets.token_hex(12)),
    }
    if previous:
        data["passkeys"] = previous.get("passkeys", [])
        data["passkey_user_id"] = previous.get("passkey_user_id", secrets.token_hex(32))
    _save_account(data)
    return data["username"]


def create_account(username, password, role=None):
    error = validate_account(username, password)
    if error:
        raise ValueError(error)
    username = _username_display(username)
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        if _find_account(database, username):
            raise ValueError("That username already exists.")
        if database["accounts"]:
            active = _find_account(database, _active_username)
            if not active or active["role"] != "admin":
                raise PermissionError("Only an administrator can create another account.")
        assigned_role = "admin" if not database["accounts"] else (role or "standard")
        salt = secrets.token_bytes(32)
        password_hash = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
        )
        data = {
            "version": 1, "username": username, "algorithm": "pbkdf2-sha256",
            "iterations": PBKDF2_ITERATIONS, "salt": salt.hex(),
            "password_hash": password_hash.hex(),
            "role": "admin" if assigned_role == "admin" else "standard",
            "profile_id": secrets.token_hex(12),
        }
        database["accounts"].append(data)
        _save_database_unlocked(path, database, current=database)
    return username


def verify_credentials(username, password):
    credentials = load_credentials(username)
    if not credentials or _username_key(username) != _username_key(credentials["username"]):
        return False
    return _password_matches(credentials, password)


def _password_matches(credentials, password):
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(credentials["salt"]),
            int(credentials["iterations"]),
        )
        return hmac.compare_digest(actual, bytes.fromhex(credentials["password_hash"]))
    except (TypeError, ValueError):
        return False


def change_account(current_password, new_username, new_password):
    username = get_username()
    if username is None or not verify_credentials(username, current_password):
        raise ValueError("Current password is incorrect.")
    error = validate_account(new_username, new_password)
    if error:
        raise ValueError(error)
    old_username = username
    new_username = _username_display(new_username)
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        previous = _find_account(database, old_username)
        if previous is None or not _password_matches(previous, current_password):
            raise ValueError("Current password is incorrect.")
        collision = _find_account(database, new_username)
        if collision is not None and collision is not previous:
            raise ValueError("That username already exists.")
        salt = secrets.token_bytes(32)
        updated = dict(previous)
        updated.update({
            "username": new_username,
            "salt": salt.hex(),
            "password_hash": hashlib.pbkdf2_hmac(
                "sha256", new_password.encode("utf-8"), salt, PBKDF2_ITERATIONS
            ).hex(),
            "iterations": PBKDF2_ITERATIONS,
            "algorithm": "pbkdf2-sha256",
        })
        database["accounts"][database["accounts"].index(previous)] = updated
        _save_database_unlocked(path, database, current=database)
    result = updated["username"]
    _set_authenticated_user(result)
    return result


def _set_authenticated_user(username):
    global _active_username
    account = load_credentials(username)
    if account is None:
        raise CredentialStoreError("The authenticated account no longer exists.")
    _active_username = account["username"]
    set_active_user(account["username"], account.get("profile_id"))


def delete_account(username):
    if not is_admin():
        raise PermissionError("Only an administrator can delete accounts.")
    if _username_key(username) == _username_key(_active_username or ""):
        raise ValueError("You cannot delete the account currently signed in.")
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        active = _find_account(database, _active_username)
        if not active or active["role"] != "admin":
            raise PermissionError("Only an administrator can delete accounts.")
        target = _find_account(database, username)
        if not target:
            raise ValueError("Account not found.")
        if target["role"] == "admin" and sum(a["role"] == "admin" for a in database["accounts"]) <= 1:
            raise ValueError("The last administrator cannot be deleted.")
        database["accounts"].remove(target)
        _save_database_unlocked(path, database, current=database)


def set_account_role(username, role):
    if not is_admin():
        raise PermissionError("Only an administrator can change roles.")
    if role not in {"admin", "standard"}:
        raise ValueError("Invalid account role.")
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        active = _find_account(database, _active_username)
        if not active or active["role"] != "admin":
            raise PermissionError("Only an administrator can change roles.")
        target = _find_account(database, username)
        if not target:
            raise ValueError("Account not found.")
        if target["role"] == "admin" and role == "standard" and sum(
                a["role"] == "admin" for a in database["accounts"]) <= 1:
            raise ValueError("At least one administrator is required.")
        target["role"] = role
        _save_database_unlocked(path, database, current=database)


def has_passkey(username=None):
    credentials = load_credentials(username)
    return bool(credentials and credentials.get("passkeys"))


def passkey_support_status():
    """Return platform-passkey availability without prompting the user."""
    if sys.platform != "win32":
        return False, "Platform passkeys currently require Windows Hello."
    try:
        from fido2.client.windows import WindowsClient
        if not WindowsClient.is_available():
            return False, "The Windows WebAuthn API is unavailable on this device."
        return True, "Windows Hello / WebAuthn is available."
    except ImportError:
        return False, "The fido2 package is not installed. Run setup again to install it."
    except Exception as error:
        return False, f"Could not access Windows WebAuthn: {error}"


def _passkey_components(parent):
    available, reason = passkey_support_status()
    if not available:
        raise RuntimeError(reason)
    from fido2.client import DefaultClientDataCollector
    from fido2.client.windows import WindowsClient
    from fido2.server import Fido2Server

    collector = DefaultClientDataCollector(PASSKEY_ORIGIN)
    client = WindowsClient(collector, handle=parent.winfo_id())
    server = Fido2Server({"id": PASSKEY_RP_ID, "name": "pyOS"})
    return client, server


def _stored_passkey_credentials(data):
    from fido2.webauthn import AttestedCredentialData

    credentials = []
    for item in data.get("passkeys", []):
        try:
            credentials.append(AttestedCredentialData(base64.b64decode(item["credential_data"])))
        except (KeyError, TypeError, ValueError):
            continue
    return credentials


def register_passkey(parent):
    """Register a Windows Hello platform credential for the current account."""
    data = load_credentials()
    if not data:
        raise RuntimeError("Create a pyOS account before adding a passkey.")
    client, server = _passkey_components(parent)
    existing = _stored_passkey_credentials(data)
    user_id_hex = data.get("passkey_user_id") or secrets.token_hex(32)
    options, state = server.register_begin(
        {"id": bytes.fromhex(user_id_hex), "name": data["username"],
         "displayName": data["username"]},
        credentials=existing,
        resident_key_requirement="required",
        user_verification="required",
        authenticator_attachment="platform",
    )
    response = client.make_credential(options["publicKey"])
    auth_data = server.register_complete(state, response)
    credential = auth_data.credential_data
    if credential is None:
        raise RuntimeError("Windows Hello did not return a usable credential.")
    encoded = base64.b64encode(bytes(credential)).decode("ascii")
    def add_to_current(current):
        current_user_id = current.get("passkey_user_id")
        if current_user_id not in {None, user_id_hex}:
            raise CredentialStoreError("Passkey state changed; please register again.")
        passkeys = list(current.get("passkeys", []))
        passkeys.append({"credential_data": encoded, "provider": "Windows Hello"})
        current["passkeys"] = passkeys
        current["passkey_user_id"] = user_id_hex
        return current

    updated = _update_account(data["username"], add_to_current)
    return len(updated["passkeys"])


def register_passkey_dialog(parent):
    """Confirm the account password before invoking Windows Hello enrollment."""
    data = load_credentials()
    if not data:
        raise RuntimeError("Create a pyOS account before adding a passkey.")
    password = simpledialog.askstring(
        "Register Passkey", "Enter the current password before registering Windows Hello:",
        show="*", parent=parent,
    )
    if password is None:
        return None
    if not verify_credentials(data["username"], password):
        raise ValueError("Current password is incorrect.")
    return register_passkey(parent)


def authenticate_passkey(parent, username=None):
    """Verify a fresh Windows Hello assertion against the stored public key."""
    data = load_credentials(username)
    if not data or not data.get("passkeys"):
        raise RuntimeError("No passkey is registered for this account.")
    client, server = _passkey_components(parent)
    credentials = _stored_passkey_credentials(data)
    if not credentials:
        raise RuntimeError("The stored passkey data is invalid.")
    # Registration requires a discoverable credential, so let Windows Hello locate it
    # by RP ID. Supplying an allow-list causes some Windows providers to report that a
    # valid platform passkey does not exist before returning an assertion to verify.
    options, state = server.authenticate_begin(None, user_verification="required")
    public_key_options = options["publicKey"]
    public_key_options["hints"] = ["client-device"]
    selection = client.get_assertion(public_key_options)
    response = selection.get_response(0)
    server.authenticate_complete(state, credentials, response)
    return data["username"]


def remove_passkeys(password):
    """Remove passkey public credentials from pyOS after password verification."""
    path = credentials_path()
    with storage._path_lock(path):
        database = _load_database_unlocked(path)
        data = _find_account(database, _active_username)
        if not data or not _password_matches(data, password):
            raise ValueError("Current password is incorrect.")
        data.pop("passkeys", None)
        data.pop("passkey_user_id", None)
        _save_database_unlocked(path, database, current=database)


def remove_passkeys_dialog(parent):
    password = simpledialog.askstring(
        "Remove Passkeys", "Enter the current password to remove all pyOS passkeys:",
        show="*", parent=parent,
    )
    if password is None:
        return False
    remove_passkeys(password)
    return True


def _show_storage_recovery_error(parent, error):
    """Explain fail-closed authentication storage errors to GUI users."""
    messagebox.showerror(
        "pyOS Storage Recovery Required",
        "pyOS could not safely read its account or installation data and has "
        "kept the desktop locked. Restore a known-good .bak file or repair the "
        "reported file before trying again.\n\n"
        f"Details: {error}",
        parent=parent,
    )


class _AccountDialog:
    def __init__(self, parent, mode, cancellable=True):
        self.parent = parent
        self.mode = mode
        self.cancellable = cancellable
        self.result = None
        # Read the store before creating a Toplevel.  If validation fails,
        # authenticate() can report recovery guidance without leaking a blank,
        # grabbed modal window.
        accounts = list_accounts()
        passkey_available = mode == "login" and any(
            has_passkey(item["username"]) for item in accounts
        )
        titles = {"create": "Create pyOS Account", "login": "pyOS Locked", "change": "Change pyOS Account"}
        self.window = tk.Toplevel(parent)
        self.window.title(titles[mode])
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", self.cancel)
        self.window.bind("<Return>", self.submit)

        frame = ttk.Frame(self.window, padding=22)
        frame.grid(sticky="nsew")
        heading = {
            "create": "Create a permanent pyOS account",
            "login": "pyOS is locked",
            "change": "Change username and password",
        }[mode]
        ttk.Label(frame, text=heading, font=("Courier New", 14, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(0, 14)
        )

        self.username = tk.StringVar(value=get_username() or (
            accounts[0]["username"] if accounts else ""
        ))
        self.password = tk.StringVar()
        self.new_password = tk.StringVar()
        self.confirmation = tk.StringVar()
        self.remember_me = tk.BooleanVar(value=False)
        row = 1
        if mode == "change":
            ttk.Label(frame, text="Current password:").grid(row=row, column=0, sticky="w", pady=4)
            first_entry = ttk.Entry(frame, textvariable=self.password, show="*", width=32)
            first_entry.grid(row=row, column=1, pady=4)
            row += 1

        ttk.Label(frame, text="Username:").grid(row=row, column=0, sticky="w", pady=4)
        if mode == "login":
            username_entry = ttk.Combobox(
                frame, textvariable=self.username,
                values=[item["username"] for item in accounts], state="readonly", width=29,
            )
        else:
            username_entry = ttk.Entry(frame, textvariable=self.username, width=32)
        username_entry.grid(row=row, column=1, pady=4)
        if mode != "login" and mode != "change":
            first_entry = username_entry
        row += 1

        password_variable = self.password if mode != "change" else self.new_password
        ttk.Label(frame, text="Password:" if mode != "change" else "New password:").grid(
            row=row, column=0, sticky="w", pady=4
        )
        password_entry = ttk.Entry(frame, textvariable=password_variable, show="*", width=32)
        password_entry.grid(row=row, column=1, pady=4)
        if mode == "login":
            first_entry = password_entry
        row += 1

        if mode in {"create", "change"}:
            ttk.Label(frame, text="Confirm password:").grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(frame, textvariable=self.confirmation, show="*", width=32).grid(
                row=row, column=1, pady=4
            )
            row += 1

        if mode == "login":
            ttk.Checkbutton(
                frame, text="Remember me on this computer", variable=self.remember_me,
            ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(9, 3))
            row += 1

        self.error = tk.StringVar()
        ttk.Label(frame, textvariable=self.error, foreground="#a00000", wraplength=390).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 2)
        )
        row += 1
        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        if cancellable:
            ttk.Button(buttons, text="Cancel", command=self.cancel).pack(side=tk.RIGHT, padx=(6, 0))
        action = {"create": "Create Account", "login": "Unlock", "change": "Save Account"}[mode]
        ttk.Button(buttons, text=action, command=self.submit).pack(side=tk.RIGHT)
        if passkey_available:
            ttk.Button(buttons, text="Use Passkey", command=self.submit_passkey).pack(
                side=tk.LEFT, padx=(0, 12)
            )

        self.window.update_idletasks()
        x = parent.winfo_screenwidth() // 2 - self.window.winfo_reqwidth() // 2
        y = parent.winfo_screenheight() // 2 - self.window.winfo_reqheight() // 2
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")
        if parent.state() != "withdrawn":
            self.window.transient(parent)
        self.window.grab_set()
        self.window.lift()
        first_entry.focus_set()

    def submit(self, event=None):
        try:
            if self.mode == "login":
                if not verify_credentials(self.username.get(), self.password.get()):
                    raise ValueError("Incorrect username or password.")
                self.result = load_credentials(self.username.get())["username"]
                _set_authenticated_user(self.result)
                create_remembered_session() if self.remember_me.get() else clear_remembered_session()
            elif self.mode == "create":
                if self.password.get() != self.confirmation.get():
                    raise ValueError("Passwords do not match.")
                self.result = create_account(self.username.get(), self.password.get())
                _set_authenticated_user(self.result)
            else:
                if self.new_password.get() != self.confirmation.get():
                    raise ValueError("Passwords do not match.")
                self.result = change_account(
                    self.password.get(), self.username.get(), self.new_password.get()
                )
        except (CredentialStoreError, storage.ConfigurationError) as error:
            self.result = None
            _show_storage_recovery_error(self.window, error)
            self.window.destroy()
            return "break"
        except (OSError, ValueError) as error:
            self.error.set(str(error))
            self.password.set("")
            if self.mode == "change":
                self.new_password.set("")
            self.confirmation.set("")
            return "break"
        self.window.destroy()
        return "break"

    def submit_passkey(self):
        self.error.set("Waiting for Windows Hello...")
        self.window.update_idletasks()
        try:
            self.result = authenticate_passkey(self.window, self.username.get())
            _set_authenticated_user(self.result)
            create_remembered_session() if self.remember_me.get() else clear_remembered_session()
        except (CredentialStoreError, storage.ConfigurationError) as error:
            self.result = None
            _show_storage_recovery_error(self.window, error)
            self.window.destroy()
            return
        except Exception as error:
            self.result = None
            self.error.set(f"Passkey failed: {error}")
            return
        self.window.destroy()

    def cancel(self, event=None):
        if not self.cancellable:
            self.window.bell()
            return "break"
        self.window.destroy()
        return "break"

    def run(self):
        self.parent.wait_window(self.window)
        return self.result


def authenticate(parent, cancellable=True, allow_remembered=True):
    """Create an account when needed, otherwise request credentials."""
    try:
        if allow_remembered:
            remembered = remembered_username()
            if remembered:
                _set_authenticated_user(remembered)
                return remembered
        mode = "login" if has_account() else "create"
        return _AccountDialog(parent, mode, cancellable).run()
    except (CredentialStoreError, storage.ConfigurationError) as error:
        _show_storage_recovery_error(parent, error)
        return None


def change_credentials_dialog(parent):
    """Require the current password, then replace username and password."""
    try:
        if not has_account():
            return _AccountDialog(parent, "create", True).run()
        return _AccountDialog(parent, "change", True).run()
    except (CredentialStoreError, storage.ConfigurationError) as error:
        _show_storage_recovery_error(parent, error)
        return None


def manage_accounts_dialog(parent):
    """Let an administrator create, remove and assign roles to local accounts."""
    if not is_admin():
        raise PermissionError("Only an administrator can manage accounts.")
    window = tk.Toplevel(parent)
    window.title("Manage pyOS Accounts")
    window.geometry("470x340")
    window.transient(parent)
    window.grab_set()
    frame = ttk.Frame(window, padding=18)
    frame.pack(fill=tk.BOTH, expand=True)
    ttk.Label(frame, text="Local pyOS accounts", font=("Courier New", 13, "bold")).pack(anchor=tk.W)
    accounts_box = tk.Listbox(frame, height=10)
    accounts_box.pack(fill=tk.BOTH, expand=True, pady=12)
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, foreground="#a00000").pack(anchor=tk.W)

    def refresh(select_name=None):
        accounts_box.delete(0, tk.END)
        for index, account in enumerate(list_accounts()):
            accounts_box.insert(tk.END, f"{account['username']}  —  {account['role'].title()}")
            if select_name and _username_key(account["username"]) == _username_key(select_name):
                accounts_box.selection_set(index)

    def selected():
        selection = accounts_box.curselection()
        return list_accounts()[selection[0]] if selection else None

    def add():
        username = simpledialog.askstring("New Account", "Username:", parent=window)
        if username is None:
            return
        password = simpledialog.askstring("New Account", "Password (at least 8 characters):",
                                          show="*", parent=window)
        if password is None:
            return
        confirmation = simpledialog.askstring("New Account", "Confirm password:",
                                              show="*", parent=window)
        if password != confirmation:
            status.set("Passwords do not match.")
            return
        try:
            create_account(username, password, role="standard")
            status.set("")
            refresh(username)
        except (ValueError, PermissionError, OSError) as error:
            status.set(str(error))

    def toggle_role():
        account = selected()
        if not account:
            status.set("Select an account first.")
            return
        try:
            set_account_role(account["username"],
                             "standard" if account["role"] == "admin" else "admin")
            status.set("")
            refresh(account["username"])
        except (ValueError, PermissionError, OSError) as error:
            status.set(str(error))

    def remove():
        account = selected()
        if not account:
            status.set("Select an account first.")
            return
        if not messagebox.askyesno("Delete Account", f"Delete {account['username']}?",
                                   parent=window):
            return
        try:
            delete_account(account["username"])
            status.set("")
            refresh()
        except (ValueError, PermissionError, OSError) as error:
            status.set(str(error))

    buttons = ttk.Frame(frame)
    buttons.pack(fill=tk.X, pady=(8, 0))
    ttk.Button(buttons, text="Add User", command=add).pack(side=tk.LEFT)
    ttk.Button(buttons, text="Toggle Admin", command=toggle_role).pack(side=tk.LEFT, padx=6)
    ttk.Button(buttons, text="Delete", command=remove).pack(side=tk.LEFT)
    ttk.Button(buttons, text="Close", command=window.destroy).pack(side=tk.RIGHT)
    refresh()
    parent.wait_window(window)
