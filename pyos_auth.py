"""Persistent local authentication shared by the pyOS GUI and CLI."""

import hashlib
import hmac
import json
import os
import secrets
import base64
import sys
import tkinter as tk
from tkinter import simpledialog, ttk
from pathlib import Path

from pyos_config import get_data_dir, load_config


PBKDF2_ITERATIONS = 600_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000
PASSKEY_RP_ID = "pyos.local"
PASSKEY_ORIGIN = "https://pyos.local"


def credentials_path():
    """Return the permanent credential-store location."""
    if load_config().get("configured"):
        return get_data_dir() / "credentials.json"
    return Path.home() / ".pyos_credentials.json"


def remembered_session_path():
    return credentials_path().with_name("remembered_session.json")


def clear_remembered_session():
    try:
        remembered_session_path().unlink()
    except OSError:
        pass
    data = load_credentials()
    if data and data.pop("remember_token_hash", None):
        _save_credentials(data)


def create_remembered_session():
    data = load_credentials()
    if not data:
        return
    token = secrets.token_urlsafe(48)
    data["remember_token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    _save_credentials(data)
    path = remembered_session_path()
    path.write_text(json.dumps({"username": data["username"], "token": token}), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def remembered_username():
    data = load_credentials()
    expected = data.get("remember_token_hash") if data else None
    if not expected:
        return None
    try:
        session = json.loads(remembered_session_path().read_text(encoding="utf-8"))
        actual = hashlib.sha256(str(session["token"]).encode()).hexdigest()
        if (hmac.compare_digest(actual, expected) and
                hmac.compare_digest(str(session["username"]).casefold(), data["username"].casefold())):
            return data["username"]
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def validate_account(username, password):
    username = username.strip()
    if not 3 <= len(username) <= 32:
        return "Username must contain between 3 and 32 characters."
    if any(character in username for character in "\r\n\t"):
        return "Username contains invalid characters."
    if len(password) < 8:
        return "Password must contain at least 8 characters."
    if len(password) > 256:
        return "Password must contain no more than 256 characters."
    return None


def load_credentials():
    """Load and strictly validate the credential record."""
    try:
        data = json.loads(credentials_path().read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        required = {"username", "salt", "password_hash", "iterations", "algorithm"}
        if not required.issubset(data) or data["algorithm"] != "pbkdf2-sha256":
            return None
        if not isinstance(data["username"], str) or not 3 <= len(data["username"]) <= 32:
            return None
        iterations = int(data["iterations"])
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            return None
        salt = bytes.fromhex(data["salt"])
        password_hash = bytes.fromhex(data["password_hash"])
        if len(salt) != 32 or len(password_hash) != 32:
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def has_account():
    return load_credentials() is not None


def get_username():
    credentials = load_credentials()
    return credentials["username"] if credentials else None


def _save_credentials(data):
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)


def _write_account(username, password, preserve_passkeys=False):
    salt = secrets.token_bytes(32)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    data = {
        "version": 1,
        "username": username.strip(),
        "algorithm": "pbkdf2-sha256",
        "iterations": PBKDF2_ITERATIONS,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
    }
    previous = load_credentials() if preserve_passkeys else None
    if previous:
        data["passkeys"] = previous.get("passkeys", [])
        data["passkey_user_id"] = previous.get("passkey_user_id", secrets.token_hex(32))
    _save_credentials(data)
    return data["username"]


def create_account(username, password):
    error = validate_account(username, password)
    if error:
        raise ValueError(error)
    if has_account():
        raise ValueError("An account already exists.")
    return _write_account(username, password)


def verify_credentials(username, password):
    credentials = load_credentials()
    if not credentials or not hmac.compare_digest(
        username.strip().casefold(), credentials["username"].casefold()
    ):
        return False
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
    return _write_account(new_username, new_password, preserve_passkeys=True)


def has_passkey():
    credentials = load_credentials()
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
    passkeys = list(data.get("passkeys", []))
    passkeys.append({"credential_data": encoded, "provider": "Windows Hello"})
    data["passkeys"] = passkeys
    data["passkey_user_id"] = user_id_hex
    _save_credentials(data)
    return len(passkeys)


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


def authenticate_passkey(parent):
    """Verify a fresh Windows Hello assertion against the stored public key."""
    data = load_credentials()
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
    data = load_credentials()
    if not data or not verify_credentials(data["username"], password):
        raise ValueError("Current password is incorrect.")
    data.pop("passkeys", None)
    data.pop("passkey_user_id", None)
    _save_credentials(data)


def remove_passkeys_dialog(parent):
    password = simpledialog.askstring(
        "Remove Passkeys", "Enter the current password to remove all pyOS passkeys:",
        show="*", parent=parent,
    )
    if password is None:
        return False
    remove_passkeys(password)
    return True


class _AccountDialog:
    def __init__(self, parent, mode, cancellable=True):
        self.parent = parent
        self.mode = mode
        self.cancellable = cancellable
        self.result = None
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

        self.username = tk.StringVar(value=get_username() or "")
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
        username_entry = ttk.Entry(frame, textvariable=self.username, width=32)
        username_entry.grid(row=row, column=1, pady=4)
        if mode == "login":
            username_entry.configure(state="readonly")
        elif mode != "change":
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
        if mode == "login" and has_passkey():
            ttk.Button(buttons, text="Use Passkey", command=self.submit_passkey).pack(
                side=tk.LEFT, padx=(0, 12)
            )

        self.window.update_idletasks()
        x = parent.winfo_screenwidth() // 2 - self.window.winfo_reqwidth() // 2
        y = parent.winfo_screenheight() // 2 - self.window.winfo_reqheight() // 2
        self.window.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.window.transient(parent)
        self.window.grab_set()
        self.window.lift()
        first_entry.focus_set()

    def submit(self, event=None):
        try:
            if self.mode == "login":
                if not verify_credentials(self.username.get(), self.password.get()):
                    raise ValueError("Incorrect username or password.")
                self.result = get_username()
                create_remembered_session() if self.remember_me.get() else clear_remembered_session()
            elif self.mode == "create":
                if self.password.get() != self.confirmation.get():
                    raise ValueError("Passwords do not match.")
                self.result = create_account(self.username.get(), self.password.get())
            else:
                if self.new_password.get() != self.confirmation.get():
                    raise ValueError("Passwords do not match.")
                self.result = change_account(
                    self.password.get(), self.username.get(), self.new_password.get()
                )
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
            self.result = authenticate_passkey(self.window)
            create_remembered_session() if self.remember_me.get() else clear_remembered_session()
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
    if allow_remembered:
        remembered = remembered_username()
        if remembered:
            return remembered
    mode = "login" if has_account() else "create"
    return _AccountDialog(parent, mode, cancellable).run()


def change_credentials_dialog(parent):
    """Require the current password, then replace username and password."""
    if not has_account():
        return _AccountDialog(parent, "create", True).run()
    return _AccountDialog(parent, "change", True).run()
