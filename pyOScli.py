import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
import os
import subprocess
import threading
import queue
from pathlib import Path
from datetime import datetime
import json
import socket
import shutil
import tempfile
import sys
import hashlib
import errno
import shlex
import signal
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
import time
from dataclasses import dataclass, field
from collections import deque

from tkinter import colorchooser

from pyos_config import (
    ConfigurationError,
    JSONPersistenceError,
    StorageOwnershipError,
    atomic_write_json,
    get_cli_settings_path,
    get_drive_b_dir,
    get_gui_settings_path,
    load_config,
    relaunch_in_configured_environment,
)
from pyos_auth import (
    CredentialStoreError,
    authenticate,
    change_credentials_dialog,
    clear_remembered_session,
    has_account,
)
from pyos_updater import recover_source_update


class CommandCancelled(RuntimeError):
    """Raised internally when a lock or shutdown invalidates queued work."""


@dataclass
class CommandTask:
    """Enqueue-time path/auth context plus a cooperative cancel flag.

    A ``cd`` command may update its own path before committing that value to the
    UI; commands already waiting behind it retain their independent snapshots.
    """

    command: str
    working_directory: str
    drive: str
    auth_generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class AuxiliaryTask:
    """Bounded non-command work such as a Browser Inspector fetch."""

    work: object
    on_success: object
    on_error: object
    auth_generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)

def check_psutil():
    """Dynamically check if psutil is available"""
    try:
        import psutil
        return True
    except ImportError:
        return False

class ThemeSettings:
    """Handle theme and display settings"""
    def __init__(self):
        self.settings_file = Path(get_cli_settings_path())
        self.defaults = {
            "console_bg": "#000000",
            "console_fg": "#ffffff",
            "console_font": "Courier New",
            "console_fontsize": 10,
            "gui_bg": "#ffffff",
            "gui_fg": "#000000",
            "gui_font": "Courier New",
            "gui_fontsize": 10,
            "list_bg": "#ffffff",
            "list_fg": "#000000",
            "style_version": 2,
        }
        self.settings = self.load_settings()
    
    def load_settings(self):
        """Load validated settings, recovering only from a validated backup."""
        if self.settings_file.exists():
            try:
                loaded = json.loads(self.settings_file.read_text(encoding="utf-8"))
                return self._validate_settings(loaded)
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                backup = self.settings_file.with_name(self.settings_file.name + ".bak")
                try:
                    loaded = json.loads(backup.read_text(encoding="utf-8"))
                    recovered = self._validate_settings(loaded)
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as backup_error:
                    raise JSONPersistenceError(
                        f"CLI settings and their backup are invalid: {self.settings_file}"
                    ) from backup_error
                atomic_write_json(self.settings_file, recovered, mode=0o600)
                return recovered
        return self.defaults.copy()

    def _validate_settings(self, loaded):
        if not isinstance(loaded, dict):
            raise ValueError("CLI settings must be a JSON object")
        if loaded.get("style_version") != self.defaults["style_version"]:
            # A previous schema is not corrupt, but its values are not trusted in
            # the current UI.  Keep the documented monochrome defaults.
            return self.defaults.copy()
        result = self.defaults.copy()
        for key in ("console_bg", "console_fg", "gui_bg", "gui_fg", "list_bg", "list_fg"):
            value = loaded.get(key, result[key])
            if not isinstance(value, str) or not value or len(value) > 64:
                raise ValueError(f"Invalid CLI colour setting: {key}")
            result[key] = value
        for key in ("console_font", "gui_font"):
            value = loaded.get(key, result[key])
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ValueError(f"Invalid CLI font setting: {key}")
            result[key] = value
        for key in ("console_fontsize", "gui_fontsize"):
            value = loaded.get(key, result[key])
            if isinstance(value, bool) or not isinstance(value, int) or not 8 <= value <= 72:
                raise ValueError(f"Invalid CLI font size setting: {key}")
            result[key] = value
        result["style_version"] = self.defaults["style_version"]
        return result
    
    def save_settings(self):
        """Validate and atomically persist settings under the storage lock."""
        self.settings = self._validate_settings(self.settings)
        atomic_write_json(self.settings_file, self.settings, mode=0o600, backup=True)
    
    def reset_to_defaults(self):
        """Reset all settings to defaults"""
        self.settings = self.defaults.copy()
        self.save_settings()

class VirtualDrive:
    def __init__(self, name, is_temporary=False, custom_path=None):
        self.name = name
        self.is_temporary = is_temporary
        self._owns_temporary_path = False
        self._temporary_directory = None
        
        if custom_path is not None:
            self.path = str(Path(custom_path))
        elif is_temporary:
            self._temporary_directory = tempfile.TemporaryDirectory(
                prefix=f"pyOS_Drive_{name}_",
                ignore_cleanup_errors=False,
            )
            self.path = self._temporary_directory.name
            self._owns_temporary_path = True
        else:
            self.path = os.path.join(os.path.expanduser("~"), f".pyOS_Drive_{name}")
        
        os.makedirs(self.path, exist_ok=True)
    
    def get_path(self):
        return self.path
    
    def get_usage(self, cancel_event=None):
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.path):
            if cancel_event is not None and cancel_event.is_set():
                raise CommandCancelled()
            for filename in filenames:
                if cancel_event is not None and cancel_event.is_set():
                    raise CommandCancelled()
                filepath = os.path.join(dirpath, filename)
                total += os.path.getsize(filepath)
        return total

    def cleanup(self):
        """Remove a session-owned temporary drive without following replacement links."""
        if not self.is_temporary or not self._owns_temporary_path:
            return True
        try:
            self._temporary_directory.cleanup()
            self._owns_temporary_path = False
            self._temporary_directory = None
            return True
        except OSError:
            # A later shutdown/finalizer attempt can retry cleanup.
            return False

class PythonOS:
    VERSION = "3.0"
    COMMAND_QUEUE_LIMIT = 8
    AUXILIARY_QUEUE_LIMIT = 8
    AUXILIARY_WORKERS = 2
    UI_QUEUE_LIMIT = 1024
    SHUTDOWN_WAIT_SECONDS = 3.0
    SHELL_TIMEOUT_SECONDS = 60
    SHELL_OUTPUT_LIMIT = 1_000_000
    # Central policy keeps optional-app and mutation decisions out of the
    # legacy conditional dispatcher.  Unknown commands are external shell
    # commands and therefore conservatively treated as mutations.
    COMMAND_POLICIES = {
        "play": {"optional_app": "media"},
        "media": {"optional_app": "media"},
        "browser": {"optional_app": "browser"},
        "browse": {"optional_app": "browser"},
        "inspect": {"optional_app": "browser"},
        "savepage": {"optional_app": "browser", "mutates": True},
        "download_page": {"optional_app": "browser", "mutates": True},
        "desktop_browser": {"optional_app": "browser"},
        "desktop_media": {"optional_app": "media"},
        "games": {"optional_app": "games"}, "snake": {"optional_app": "games"},
        "sudoku": {"optional_app": "games"}, "chess": {"optional_app": "games"},
        "messenger": {"optional_app": "messenger"}, "ide": {"optional_app": "ide"},
        "weather": {"optional_app": "weather"}, "news": {"optional_app": "news"},
        "pyai": {"optional_app": "pyai"}, "modding": {"optional_app": "modding"},
        "mkdir": {"mutates": True}, "del": {"mutates": True}, "rm": {"mutates": True},
        "copy": {"mutates": True}, "cp": {"mutates": True},
        "move": {"mutates": True}, "mv": {"mutates": True},
        "write": {"mutates": True}, "nano": {"mutates": True},
        "append": {"mutates": True}, "rename": {"mutates": True},
        "touch": {"mutates": True}, "archive": {"mutates": True},
        "extract": {"mutates": True}, "download": {"mutates": True},
        "monochrome": {"mutates": True}, "color": {"mutates": True},
        "font": {"mutates": True}, "fontsize": {"mutates": True},
        "powershell": {"mutates": True}, "ps": {"mutates": True},
        "wsl": {"mutates": True},
    }
    BUILTIN_COMMANDS = {
        "cd", "drives", "driveinfo", "drive_info", "open", "start", "explorer", "files",
        "play", "media", "apps", "games", "snake", "sudoku", "chess", "messenger",
        "calculator", "calc", "images", "imageviewer", "notepad", "editor", "ide",
        "filemanager", "desktop_browser", "desktop_media", "pyos_settings", "dispenser",
        "browser", "browse", "inspect", "savepage", "download_page", "history", "hash",
        "date", "time", "whoami", "gui_settings", "desktop_settings", "monochrome", "exit",
        "quit", "cls", "clear", "dir", "ls", "tree", "mkdir", "del", "rm", "copy", "cp",
        "move", "mv", "pwd", "echo", "type", "cat", "write", "nano", "append", "rename",
        "info", "lines", "wc", "grep", "search_text", "touch", "head", "tail", "archive",
        "extract", "files_only", "dirs_only", "hexdump", "xxd", "ipconfig", "ifconfig",
        "netstat", "ping", "network", "download", "sysinfo", "diskspace", "tasklist", "ps",
        "search", "find", "theme", "settings", "color", "font", "fontsize", "theme_info",
        "deskgui", "help", "commands", "weather", "news", "pyai", "modding",
        "powershell", "ps", "wsl", "stop",
    }

    def __init__(self, root):
        self.root = root
        self._ui_thread_id = threading.get_ident()
        self._ui_events = queue.Queue(maxsize=self.UI_QUEUE_LIMIT)
        self._command_tasks = queue.Queue(maxsize=self.COMMAND_QUEUE_LIMIT)
        self._auxiliary_tasks = queue.Queue(maxsize=self.AUXILIARY_QUEUE_LIMIT)
        self._command_stop = threading.Event()
        self._auxiliary_stop = threading.Event()
        self._command_active = threading.Event()
        self._active_command_lock = threading.Lock()
        self._active_command_task = None
        self._active_auxiliary_lock = threading.Lock()
        self._active_auxiliary_tasks = {}
        self._worker_local = threading.local()
        self._state_lock = threading.RLock()
        self._mutation_lock = threading.RLock()
        self._children_lock = threading.Lock()
        self._child_processes = set()
        self._cleanup_thread = None
        self._closing = False
        self._locked = False
        self._auth_generation = 0
        self._lock_overlay = None
        self._ui_after = None
        self.root.title(f"pyOS {self.VERSION} - Command Center")
        self.root.geometry("1200x800")
        
        # Load theme settings
        self.theme = ThemeSettings()
        self.root.configure(bg=self.theme.settings["gui_bg"])
        self.gui_settings_file = get_gui_settings_path()
        self.root.option_add("*Font", ("Courier New", 10))
        self.root.option_add("*Background", "white")
        self.root.option_add("*Foreground", "black")
        self.root.option_add("*Button.activeBackground", "black")
        self.root.option_add("*Button.activeForeground", "white")
        self.root.option_add("*Listbox.selectBackground", "black")
        self.root.option_add("*Listbox.selectForeground", "white")
        
        self.current_directory = str(Path.home())
        self.command_history = []
        self.history_index = -1
        self.authenticated = False
        self.authenticated_username = None
        configured_apps = load_config().get("enabled_apps")
        self.enabled_apps = (
            {str(app_id).casefold() for app_id in configured_apps}
            if isinstance(configured_apps, list) else None
        )
        
        # Virtual Drives
        self.drive_a = VirtualDrive("A", is_temporary=True)
        self.drive_b = VirtualDrive("B", is_temporary=False, custom_path=get_drive_b_dir())  # Permanent storage
        self.drives = {
            "C": str(Path.home()),
            "A": self.drive_a.get_path(),
            "B": self.drive_b.get_path()
        }
        
        self.current_drive = "C"
        
        self.setup_ui()
        self._command_worker = threading.Thread(
            target=self._command_worker_loop,
            name="pyOS-command-worker",
            daemon=True,
        )
        self._command_worker.start()
        self._auxiliary_workers = []
        for index in range(self.AUXILIARY_WORKERS):
            worker = threading.Thread(
                target=self._auxiliary_worker_loop,
                name=f"pyOS-auxiliary-worker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._auxiliary_workers.append(worker)
        self._ui_after = self.root.after(25, self._drain_ui_events)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
    
    def setup_ui(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background="white", foreground="black", font=("Courier New", 10))
        style.configure("TButton", background="white", foreground="black", bordercolor="black")
        style.map("TButton", background=[("active", "black")], foreground=[("active", "white")])
        style.configure("TEntry", fieldbackground="white", foreground="black", bordercolor="black")
        style.configure("TLabelframe", background="white", foreground="black", bordercolor="black")
        style.configure("TLabelframe.Label", background="white", foreground="black")

        # Top menu bar
        menubar = tk.Menu(self.root)
        self.menubar = menubar
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open Directory", command=self.open_directory)
        file_menu.add_command(label="Clear Console", command=self.clear_console)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.shutdown)
        
        drive_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Drives", menu=drive_menu)
        drive_menu.add_command(label="Switch to Drive A", command=lambda: self.switch_drive("A"))
        drive_menu.add_command(label="Switch to Drive B", command=lambda: self.switch_drive("B"))
        drive_menu.add_command(label="Switch to Drive C", command=lambda: self.switch_drive("C"))
        drive_menu.add_separator()
        drive_menu.add_command(label="Drive Info", command=self.show_drive_info)

        apps_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Apps", menu=apps_menu)
        apps_menu.add_command(label="Desktop GUI", command=self.open_desktop_gui)
        apps_menu.add_separator()
        for label, app_name in (
            ("File Manager", "files"),
            ("Games Suite", "games"),
            ("Snake", "snake"),
            ("Sudoku", "sudoku"),
            ("Automated Chess", "chess"),
            ("Messenger", "messenger"),
            ("Calculator", "calculator"),
            ("Image Viewer", "images"),
            ("Notepad", "notepad"),
            ("Text Editor", "editor"),
            ("Media Player", "media"),
            ("Python IDE", "ide"),
            ("Internet Browser", "browser"),
            ("Weather", "weather"),
            ("News", "news"),
            ("pyAI", "pyai"),
            ("Modding Tools", "modding"),
            ("Dispenser", "dispenser"),
        ):
            if not self.is_app_enabled(app_name):
                continue
            apps_menu.add_command(
                label=label,
                command=lambda name=app_name: self.open_desktop_app(name),
            )
        if self.is_app_enabled("browser"):
            apps_menu.add_command(label="Browser Inspector", command=self.open_browser_inspector)
        apps_menu.add_command(label="Open Current Folder", command=lambda: self._open_explorer(self.current_directory))
        
        network_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Network", menu=network_menu)
        network_menu.add_command(label="Network Status", command=self.show_network_status)
        network_menu.add_command(label="IP Configuration", command=self.show_ipconfig)

        shell_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Shell", menu=shell_menu)
        shell_menu.add_command(
            label="Windows PowerShell Command...",
            command=lambda: self.prompt_host_shell("powershell"),
        )
        shell_menu.add_command(
            label="WSL Command...",
            command=lambda: self.prompt_host_shell("wsl"),
        )
        shell_menu.add_separator()
        shell_menu.add_command(label="Stop Active Command", command=self.stop_active_command)
        
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Theme Settings", command=self.open_theme_settings)
        settings_menu.add_command(label="Change Account", command=self.change_cli_account)
        settings_menu.add_command(label="Lock CLI", command=self.lock_cli)
        settings_menu.add_separator()
        settings_menu.add_command(label="Reset Theme", command=self.reset_theme)
        
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="Commands", command=self.show_commands)
        help_menu.add_command(label="About", command=self.show_about)
        
        # Main container
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Status bar
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(0, 5))
        
        self.drive_var = tk.StringVar(value="Drive: C:")
        ttk.Label(status_frame, textvariable=self.drive_var, font=("Courier", 9, "bold")).pack(side=tk.LEFT, padx=5)
        
        self.user_var = tk.StringVar(value="User: Locked" if has_account() else "User: Not configured")
        ttk.Label(status_frame, textvariable=self.user_var).pack(side=tk.LEFT, padx=20)
        
        self.time_var = tk.StringVar()
        ttk.Label(status_frame, textvariable=self.time_var).pack(side=tk.RIGHT, padx=5)
        self.update_time()
        
        # Top section: File browser
        browser_frame = ttk.LabelFrame(main_frame, text="File Browser", padding=5)
        browser_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Path display
        path_frame = ttk.Frame(browser_frame)
        path_frame.pack(fill=tk.X, pady=5)
        ttk.Label(path_frame, text="Current Path:").pack(side=tk.LEFT, padx=5)
        self.path_var = tk.StringVar(value=self.current_directory)
        path_entry = ttk.Entry(path_frame, textvariable=self.path_var, state='readonly')
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        ttk.Button(path_frame, text="Browse", command=self.open_directory).pack(side=tk.LEFT, padx=5)
        ttk.Button(path_frame, text="Drive A", command=lambda: self.switch_drive("A")).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_frame, text="Drive B", command=lambda: self.switch_drive("B")).pack(side=tk.LEFT, padx=3)
        ttk.Button(path_frame, text="Refresh", command=self.refresh_files).pack(side=tk.LEFT, padx=5)
        
        # File list
        list_frame = ttk.Frame(browser_frame)
        list_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        self.file_listbox = tk.Listbox(list_frame, bg=self.theme.settings["list_bg"], fg=self.theme.settings["list_fg"], 
                                       yscrollcommand=scrollbar.set, font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]))
        self.file_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.file_listbox.bind('<Double-Button-1>', self.open_file_or_folder)
        self.file_listbox.bind('<Delete>', lambda e: self.delete_file())
        scrollbar.config(command=self.file_listbox.yview)
        
        # Middle section: Console output
        console_frame = ttk.LabelFrame(main_frame, text="Console Output", padding=5)
        console_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))
        
        self.console = scrolledtext.ScrolledText(
            console_frame, 
            bg=self.theme.settings["console_bg"], 
            fg=self.theme.settings["console_fg"],
            font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]),
            height=15,
            state='disabled'
        )
        self.console.pack(fill=tk.BOTH, expand=True)
        
        # Bottom section: Command input
        input_frame = ttk.LabelFrame(main_frame, text="Command Input", padding=5)
        input_frame.pack(fill=tk.X)
        
        ttk.Label(input_frame, text="Command:").pack(side=tk.LEFT, padx=5)
        
        self.input_var = tk.StringVar()
        self.command_input = ttk.Entry(input_frame, textvariable=self.input_var, font=("Courier", 10))
        self.command_input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.command_input.bind('<Return>', lambda e: self.execute_command())
        self.command_input.bind('<Up>', self.history_up)
        self.command_input.bind('<Down>', self.history_down)
        
        ttk.Button(input_frame, text="Execute", command=self.execute_command).pack(side=tk.LEFT, padx=5)
        ttk.Button(input_frame, text="Stop", command=self.stop_active_command).pack(side=tk.LEFT, padx=5)
        
        self.refresh_files()
        self.log_message("Type 'help' for commands\n")
        self.log_message(f"pyOS Command Center {self.VERSION}\n\n")
        self.command_input.focus()

    def _on_ui_thread(self):
        return threading.get_ident() == self._ui_thread_id

    def _post_ui(self, callback, *args, **kwargs):
        """Run a callback on Tk's owning thread without calling Tk from a worker."""
        if self._closing:
            return False
        if self._on_ui_thread():
            callback(*args, **kwargs)
            return True
        while not self._closing:
            try:
                self._ui_events.put((callback, args, kwargs), timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _post_command_ui(self, callback, *args, **kwargs):
        """Post a command result that expires on lock/account generation change."""
        task = self._current_command_task()
        if task is None:
            return self._post_ui(callback, *args, **kwargs)
        if not self._task_is_active(task):
            return False
        return self._post_ui(
            self._run_command_ui_if_current,
            task.auth_generation,
            callback,
            args,
            kwargs,
        )

    def _run_command_ui_if_current(self, auth_generation, callback, args, kwargs):
        if (self._closing or self._locked or not self.authenticated
                or auth_generation != self._auth_generation):
            return
        callback(*args, **kwargs)

    def _drain_ui_events(self):
        """Apply a bounded batch of worker results on Tk's event loop."""
        self._ui_after = None
        for _ in range(100):
            try:
                callback, args, kwargs = self._ui_events.get_nowait()
            except queue.Empty:
                break
            try:
                callback(*args, **kwargs)
            except Exception as error:
                if not self._closing:
                    print(f"Could not apply CLI worker result: {error}", file=sys.stderr)
            finally:
                self._ui_events.task_done()
        if not self._closing:
            self._ui_after = self.root.after(25, self._drain_ui_events)

    def _command_worker_loop(self):
        """Serialize commands from the bounded queue to protect shared CLI state."""
        while not self._command_stop.is_set():
            try:
                task = self._command_tasks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if task is None:
                    return
                if not self._task_is_active(task):
                    continue
                with self._active_command_lock:
                    self._active_command_task = task
                self._command_active.set()
                self._worker_local.command_task = task
                self._run_command(task.command)
            except CommandCancelled:
                pass
            finally:
                self._worker_local.command_task = None
                with self._active_command_lock:
                    if self._active_command_task is task:
                        self._active_command_task = None
                self._command_active.clear()
                self._command_tasks.task_done()

    def _auxiliary_worker_loop(self):
        """Run a fixed number of cancellable auxiliary jobs."""
        while not self._auxiliary_stop.is_set():
            try:
                task = self._auxiliary_tasks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                if task is None:
                    return
                if not self._auxiliary_task_is_active(task):
                    continue
                with self._active_auxiliary_lock:
                    self._active_auxiliary_tasks[id(task)] = task
                try:
                    result = task.work(task.cancel_event)
                except CommandCancelled:
                    continue
                except Exception as error:
                    if self._auxiliary_task_is_active(task):
                        self._post_ui(self._finish_auxiliary_task, task, False, error)
                else:
                    if self._auxiliary_task_is_active(task):
                        self._post_ui(self._finish_auxiliary_task, task, True, result)
            finally:
                if task is not None:
                    with self._active_auxiliary_lock:
                        self._active_auxiliary_tasks.pop(id(task), None)
                self._auxiliary_tasks.task_done()

    def _finish_auxiliary_task(self, task, succeeded, value):
        if not self._auxiliary_task_is_active(task):
            return
        callback = task.on_success if succeeded else task.on_error
        callback(value)

    def _submit_auxiliary(self, work, on_success, on_error):
        """Queue bounded auxiliary work without creating per-click threads."""
        if self._closing or self._locked or not self.authenticated:
            return None
        task = AuxiliaryTask(work, on_success, on_error, self._auth_generation)
        try:
            self._auxiliary_tasks.put_nowait(task)
        except queue.Full:
            return None
        return task

    def _current_command_task(self):
        return getattr(self._worker_local, "command_task", None)

    def _task_is_active(self, task):
        return bool(
            task is not None
            and not task.cancel_event.is_set()
            and not self._closing
            and not self._locked
            and self.authenticated
            and task.auth_generation == self._auth_generation
        )

    def _auxiliary_task_is_active(self, task):
        return bool(
            task is not None
            and not task.cancel_event.is_set()
            and not self._closing
            and not self._locked
            and self.authenticated
            and task.auth_generation == self._auth_generation
        )

    def _require_active_task(self, task=None):
        task = task or self._current_command_task()
        if task is not None and not self._task_is_active(task):
            raise CommandCancelled()
        if self._closing or self._locked or not self.authenticated:
            raise CommandCancelled()
        return task

    def _working_directory(self):
        task = self._current_command_task()
        return task.working_directory if task is not None else self.current_directory

    def _working_drive(self):
        task = self._current_command_task()
        return task.drive if task is not None else self.current_drive

    def _perform_mutation(self, action, task=None):
        """Make the lock boundary atomic with respect to a filesystem mutation."""
        with self._mutation_lock:
            self._require_active_task(task)
            return action()

    def _cancel_active_work(self):
        with self._active_command_lock:
            if self._active_command_task is not None:
                self._active_command_task.cancel_event.set()
        self._cancel_auxiliary_work()
        self._terminate_children()

    def stop_active_command(self):
        """Cancel active command and auxiliary work without closing the CLI."""
        if self._closing:
            return
        with self._active_command_lock:
            active = self._active_command_task is not None
        self._cancel_active_work()
        self.log_message(
            "Cancellation requested.\n" if active else "No command is currently running.\n"
        )

    def prompt_host_shell(self, shell_name):
        """Prompt for a one-shot host-shell command whose output stays in the CLI."""
        if self._locked or self._closing or not self.ensure_authenticated():
            return
        label = "Windows PowerShell" if shell_name == "powershell" else "WSL"
        command = simpledialog.askstring(
            f"Run {label}",
            f"Enter a {label} command:",
            parent=self.root,
        )
        if command and command.strip():
            self.input_var.set(f"{shell_name} {command.strip()}")
            self.execute_command()

    @staticmethod
    def _host_shell_arguments(shell_name, command, working_directory):
        """Return a shell executable and arguments without invoking an implicit host shell."""
        name = str(shell_name).casefold()
        if os.name != "nt":
            raise OSError("Windows PowerShell and WSL integration is available only on Windows.")
        if not command.strip():
            raise ValueError(f"Usage: {name} <command>")
        if name in {"powershell", "ps"}:
            executable = shutil.which("powershell.exe") or shutil.which("powershell")
            if not executable:
                raise OSError("Windows PowerShell was not found.")
            return [
                executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", command,
            ]
        if name == "wsl":
            executable = shutil.which("wsl.exe") or shutil.which("wsl")
            if not executable:
                raise OSError(
                    "WSL was not found. Install it from an elevated PowerShell prompt with: wsl --install"
                )
            return [executable, "--cd", os.path.abspath(working_directory), "--exec", "sh", "-lc", command]
        raise ValueError(f"Unsupported host shell: {shell_name}")

    @classmethod
    def _bounded_shell_output(cls, output):
        output = output or ""
        if len(output) <= cls.SHELL_OUTPUT_LIMIT:
            return output
        omitted = len(output) - cls.SHELL_OUTPUT_LIMIT
        return output[:cls.SHELL_OUTPUT_LIMIT] + f"\n[Output truncated; {omitted:,} characters omitted.]\n"

    def _run_host_shell(self, shell_name, command):
        try:
            args = self._host_shell_arguments(shell_name, command, self._working_directory())
            returncode, stdout, stderr = self._run_tracked_capture(
                args,
                cwd=self._working_directory(),
                timeout=self.SHELL_TIMEOUT_SECONDS,
            )
            output = self._bounded_shell_output(stdout)
            errors = self._bounded_shell_output(stderr)
            if output:
                self.log_message(output + ("" if output.endswith("\n") else "\n"))
            if errors:
                self.log_message(errors + ("" if errors.endswith("\n") else "\n"))
            if returncode and not errors:
                self.log_message(f"{shell_name} exited with status {returncode}.\n")
            elif not output and not errors:
                self.log_message(f"{shell_name} completed successfully.\n")
        except subprocess.TimeoutExpired:
            self.log_message(
                f"{shell_name} timed out after {self.SHELL_TIMEOUT_SECONDS} seconds and was stopped.\n"
            )
        except (OSError, ValueError) as error:
            self.log_message(f"Could not run {shell_name}: {error}\n")
    def _cancel_auxiliary_work(self):
        with self._active_auxiliary_lock:
            for active in self._active_auxiliary_tasks.values():
                active.cancel_event.set()
        while True:
            try:
                queued = self._auxiliary_tasks.get_nowait()
            except queue.Empty:
                break
            else:
                if queued is not None:
                    queued.cancel_event.set()
                self._auxiliary_tasks.task_done()

    def _register_child(self, process):
        with self._children_lock:
            self._child_processes.add(process)
        return process

    def _spawn_process(self, args, **kwargs):
        with self._mutation_lock:
            self._require_active_task()
            if os.name == "nt":
                kwargs.setdefault(
                    "creationflags", getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                kwargs.setdefault("start_new_session", True)
            return self._register_child(subprocess.Popen(args, **kwargs))

    @staticmethod
    def _stop_process_tree(process, *, force=False):
        if process.poll() is not None:
            return
        if os.name != "nt":
            try:
                group = os.getpgid(process.pid)
                os.killpg(group, signal.SIGKILL if force else signal.SIGTERM)
                return
            except (OSError, ProcessLookupError):
                pass
        if force and os.name == "nt":
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                killer = subprocess.Popen(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                killer.wait(timeout=1)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass

    def _run_tracked_capture(self, args, *, cwd=None, timeout=15, shell=False):
        """Run a cancellable child process registered with CLI lifecycle."""
        task = self._current_command_task()
        with self._mutation_lock:
            self._require_active_task(task)
            process = self._spawn_process(
                args,
                cwd=cwd,
                shell=shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._require_active_task(task)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop_process_tree(process)
                    try:
                        process.wait(timeout=0.5)
                    except (OSError, subprocess.TimeoutExpired):
                        self._stop_process_tree(process, force=True)
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                    return process.returncode, stdout, stderr
                except subprocess.TimeoutExpired:
                    continue
        except CommandCancelled:
            if process.poll() is None:
                self._stop_process_tree(process)
                try:
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    self._stop_process_tree(process, force=True)
            raise
        finally:
            self._reap_children()

    def _reap_children(self):
        with self._children_lock:
            finished = {process for process in self._child_processes if process.poll() is not None}
            self._child_processes.difference_update(finished)

    def _terminate_children(self):
        with self._children_lock:
            children = tuple(self._child_processes)
        for process in children:
            if process.poll() is None:
                self._stop_process_tree(process)
        deadline = time.monotonic() + 0.75
        for process in children:
            if process.poll() is not None:
                continue
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                self._stop_process_tree(process, force=True)
        self._reap_children()

    def _discard_queued_commands(self):
        while True:
            try:
                task = self._command_tasks.get_nowait()
            except queue.Empty:
                return
            else:
                if task is not None:
                    task.cancel_event.set()
                self._command_tasks.task_done()

    @staticmethod
    def _optional_app_id(app_name):
        return {
            "browser": "browser",
            "desktop_browser": "browser",
            "media": "media",
            "desktop_media": "media",
            "messenger": "messenger",
            "games": "games",
            "snake": "games",
            "sudoku": "games",
            "chess": "games",
            "ide": "ide",
            "pyai": "pyai",
            "weather": "weather",
            "news": "news",
            "modding": "modding",
        }.get(str(app_name).casefold())

    def is_app_enabled(self, app_name):
        optional_id = self._optional_app_id(app_name)
        return optional_id is None or self.enabled_apps is None or optional_id in self.enabled_apps

    def _optional_command_enabled(self, command_name):
        required = self.COMMAND_POLICIES.get(
            str(command_name).casefold(), {}
        ).get("optional_app")
        return required is None or self.is_app_enabled(required)

    def _command_policy(self, command_name):
        name = str(command_name).casefold()
        policy = dict(self.COMMAND_POLICIES.get(name, {}))
        if name not in self.BUILTIN_COMMANDS:
            policy["mutates"] = True
            policy["external"] = True
        return policy

    def _commit_task_directory(self, task, directory, drive=None):
        self._require_active_task(task)
        task.working_directory = os.path.abspath(directory)
        if drive is not None:
            task.drive = drive
        self._post_ui(
            self._apply_task_directory,
            task.working_directory,
            task.drive,
            task.auth_generation,
        )

    def _apply_task_directory(self, directory, drive, auth_generation):
        if self._closing or self._locked or auth_generation != self._auth_generation:
            return
        self.current_directory = directory
        self.current_drive = drive
        self._update_directory_widgets()

    def shutdown(self):
        """Cancel and join active work before session storage can be removed."""
        if self._closing:
            return
        with self._state_lock:
            self._closing = True
            self._auth_generation += 1
            self._command_stop.set()
            self._auxiliary_stop.set()
            self._discard_queued_commands()
            self._cancel_active_work()
        try:
            self._command_tasks.put_nowait(None)
        except queue.Full:
            pass
        for _ in self._auxiliary_workers:
            try:
                self._auxiliary_tasks.put_nowait(None)
            except queue.Full:
                break
        if self._ui_after is not None:
            try:
                self.root.after_cancel(self._ui_after)
            except tk.TclError:
                pass
            self._ui_after = None
        if self._lock_overlay is not None:
            try:
                self._lock_overlay.grab_release()
            except tk.TclError:
                pass
        deadline = time.monotonic() + self.SHUTDOWN_WAIT_SECONDS
        workers = [self._command_worker, *self._auxiliary_workers]
        for worker in workers:
            if worker is threading.current_thread():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        survivors = [worker for worker in workers if worker.is_alive()]
        if not survivors:
            self.drive_a.cleanup()
        else:
            # Never delete Drive A under a worker still using its queued path.
            # The coordinator waits off the Tk thread and performs the cleanup
            # only after every tracked worker has actually stopped.
            def finish_cleanup():
                for worker in survivors:
                    worker.join()
                self.drive_a.cleanup()

            self._cleanup_thread = threading.Thread(
                target=finish_cleanup,
                name="pyOS-drive-a-cleanup",
                # Keep the process alive long enough to honour Drive A's
                # cleanup contract after the bounded Tk-thread wait expires.
                daemon=False,
            )
            self._cleanup_thread.start()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
    
    def update_time(self):
        """Update time display"""
        if self._closing:
            return
        self._reap_children()
        self.time_var.set(datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self.update_time)
    
    def open_theme_settings(self):
        """Open theme settings dialog"""
        if not self._on_ui_thread():
            self._post_command_ui(self.open_theme_settings)
            return
        if self._locked or self._closing:
            return
        settings_window = tk.Toplevel(self.root)
        settings_window.title("Theme Settings")
        settings_window.geometry("500x600")
        settings_window.transient(self.root)
        
        notebook = ttk.Notebook(settings_window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Console Tab
        console_frame = ttk.Frame(notebook)
        notebook.add(console_frame, text="Console")
        
        ttk.Label(console_frame, text="Console Background Color:").pack(pady=5)
        ttk.Button(console_frame, text="Choose Color", 
                   command=lambda: self.choose_color("console_bg")).pack()
        ttk.Label(console_frame, text=f"Current: {self.theme.settings['console_bg']}", 
                  foreground=self.theme.settings['console_bg']).pack()
        
        ttk.Label(console_frame, text="Console Text Color:").pack(pady=5)
        ttk.Button(console_frame, text="Choose Color", 
                   command=lambda: self.choose_color("console_fg")).pack()
        ttk.Label(console_frame, text=f"Current: {self.theme.settings['console_fg']}", 
                  foreground=self.theme.settings['console_fg']).pack()
        
        ttk.Label(console_frame, text="Console Font:").pack(pady=5)
        font_var = tk.StringVar(value=self.theme.settings['console_font'])
        font_combo = ttk.Combobox(console_frame, textvariable=font_var, 
                                   values=["Courier", "Arial", "Consolas", "Courier New", "Monospace"])
        font_combo.pack()
        ttk.Button(console_frame, text="Apply Font", 
                   command=lambda: self.change_setting('console_font', font_var.get())).pack(pady=5)
        
        ttk.Label(console_frame, text="Console Font Size:").pack(pady=5)
        size_var = tk.StringVar(value=str(self.theme.settings['console_fontsize']))
        size_spin = ttk.Spinbox(console_frame, from_=8, to=24, textvariable=size_var)
        size_spin.pack()
        ttk.Button(console_frame, text="Apply Size", 
                   command=lambda: self.change_setting('console_fontsize', int(size_var.get()))).pack(pady=5)
        
        # List Box Tab
        list_frame = ttk.Frame(notebook)
        notebook.add(list_frame, text="File List")
        
        ttk.Label(list_frame, text="List Background Color:").pack(pady=5)
        ttk.Button(list_frame, text="Choose Color", 
                   command=lambda: self.choose_color("list_bg")).pack()
        ttk.Label(list_frame, text=f"Current: {self.theme.settings['list_bg']}", 
                  background=self.theme.settings['list_bg']).pack()
        
        ttk.Label(list_frame, text="List Text Color:").pack(pady=5)
        ttk.Button(list_frame, text="Choose Color", 
                   command=lambda: self.choose_color("list_fg")).pack()
        ttk.Label(list_frame, text=f"Current: {self.theme.settings['list_fg']}", 
                  foreground=self.theme.settings['list_fg']).pack()
        
        # Buttons
        button_frame = ttk.Frame(settings_window)
        button_frame.pack(fill=tk.X, padx=10, pady=10)
        ttk.Button(button_frame, text="Save", command=self.save_theme_settings).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Reset to Defaults", command=self.reset_theme).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Close", command=settings_window.destroy).pack(side=tk.RIGHT, padx=5)
    
    def choose_color(self, setting_key):
        """Open color picker"""
        color = colorchooser.askcolor(title=f"Choose color for {setting_key}")
        if color[1]:  # If color was selected
            self.theme.settings[setting_key] = color[1]
            self.apply_theme_changes()
    
    def change_setting(self, key, value):
        """Change a setting"""
        self.theme.settings[key] = value
        self.apply_theme_changes()
    
    def apply_theme_changes(self):
        """Apply theme changes to UI"""
        if not self._on_ui_thread():
            self._post_command_ui(self.apply_theme_changes)
            return
        if self._closing or self._locked:
            return
        self.console.config(bg=self.theme.settings["console_bg"], 
                           fg=self.theme.settings["console_fg"],
                           font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]))
        self.file_listbox.config(bg=self.theme.settings["list_bg"], 
                                fg=self.theme.settings["list_fg"],
                                font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]))
        self.log_message("Theme updated!\n")
    
    def save_theme_settings(self):
        """Save theme settings"""
        try:
            self.theme.save_settings()
        except (ConfigurationError, JSONPersistenceError, StorageOwnershipError, OSError) as error:
            messagebox.showerror(
                "CLI Settings Recovery Required",
                f"pyOS could not safely save CLI settings:\n\n{error}",
                parent=self.root,
            )
            return
        self.log_message("Settings saved!\n")
    
    def reset_theme(self):
        """Reset theme to defaults"""
        try:
            self.theme.reset_to_defaults()
        except (ConfigurationError, JSONPersistenceError, StorageOwnershipError, OSError) as error:
            messagebox.showerror(
                "CLI Settings Recovery Required",
                f"pyOS could not safely reset CLI settings:\n\n{error}",
                parent=self.root,
            )
            return
        self.apply_theme_changes()
        self.log_message("Theme reset to defaults!\n")

    def ensure_authenticated(self):
        """Authenticate once for the current CLI session."""
        if self._closing:
            return False
        if self._locked:
            return self.unlock_cli()
        if self.authenticated:
            return True
        try:
            username = authenticate(self.root, cancellable=True, allow_remembered=False)
        except (CredentialStoreError, ConfigurationError, JSONPersistenceError, StorageOwnershipError) as error:
            messagebox.showerror(
                "pyOS Account Recovery Required",
                f"pyOS detected invalid account state and failed closed:\n\n{error}",
                parent=self.root,
            )
            return False
        if not username:
            return False
        self.authenticated = True
        self.authenticated_username = username
        self.user_var.set(f"User: {username}")
        self.log_message(f"Authenticated as {username}.\n")
        return True

    def lock_cli(self):
        """Cover the entire CLI and require a fresh authentication to resume."""
        if not self._on_ui_thread():
            self._post_ui(self.lock_cli)
            return
        if self._closing or self._locked:
            return
        with self._state_lock:
            self._locked = True
            self._auth_generation += 1
            self._discard_queued_commands()
            self._cancel_active_work()
        remembered_clear_error = None
        try:
            clear_remembered_session()
        except Exception as error:
            # Locking must fail closed even if persistent state cannot be updated.
            remembered_clear_error = str(error)
        self.authenticated = False
        self.authenticated_username = None
        self.user_var.set("User: Locked")
        self.input_var.set("")
        self.log_message("CLI locked. Fresh authentication is required to continue.\n")
        for child in list(self.root.winfo_children()):
            if isinstance(child, tk.Toplevel):
                child.destroy()
        self.root.config(menu="")

        overlay = tk.Frame(self.root, bg="#000000", bd=0, takefocus=True)
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        card = tk.Frame(overlay, bg="#ffffff", bd=3, relief=tk.RAISED, padx=35, pady=30)
        card.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        tk.Label(
            card, text="PYTHON OS COMMAND CENTER", bg="#ffffff", fg="#000000",
            font=("Courier New", 15, "bold"),
        ).pack(pady=(0, 18))
        tk.Label(
            card, text="CLI LOCKED", bg="#ffffff", fg="#000000",
            font=("Courier New", 22, "bold"),
        ).pack(pady=(0, 8))
        lock_message = "Authenticate again to use files, menus, or commands."
        if remembered_clear_error:
            lock_message += "\nRemembered sign-in could not be cleared; this unlock will still require credentials."
        tk.Label(
            card, text=lock_message,
            bg="#ffffff", fg="#000000", font=("Courier New", 10),
            justify=tk.CENTER, wraplength=520,
        ).pack(pady=(0, 20))
        unlock_button = ttk.Button(card, text="Unlock", command=self.unlock_cli)
        unlock_button.pack(ipadx=18, ipady=5)
        self._lock_overlay = overlay
        overlay.bind("<Return>", lambda event: self.unlock_cli())
        overlay.bind("<Escape>", lambda event: "break")
        overlay.lift()
        overlay.focus_set()
        try:
            overlay.grab_set()
        except tk.TclError:
            pass

    def unlock_cli(self):
        """Unlock only after an interactive, non-remembered authentication."""
        if not self._on_ui_thread():
            self._post_ui(self.unlock_cli)
            return False
        if self._closing:
            return False
        if not self._locked:
            return self.authenticated
        overlay = self._lock_overlay
        if overlay is not None:
            try:
                overlay.grab_release()
            except tk.TclError:
                pass
        try:
            username = authenticate(self.root, cancellable=True, allow_remembered=False)
        except (CredentialStoreError, ConfigurationError, JSONPersistenceError, StorageOwnershipError) as error:
            messagebox.showerror(
                "pyOS Account Recovery Required",
                f"pyOS detected invalid account state and kept the CLI locked:\n\n{error}",
                parent=self.root,
            )
            username = None
        if not username:
            if overlay is not None and overlay.winfo_exists():
                overlay.lift()
                overlay.focus_set()
                try:
                    overlay.grab_set()
                except tk.TclError:
                    pass
            return False
        self.authenticated = True
        self.authenticated_username = username
        self._locked = False
        self.user_var.set(f"User: {username}")
        if overlay is not None:
            try:
                overlay.grab_release()
            except tk.TclError:
                pass
            overlay.destroy()
        self._lock_overlay = None
        self.root.config(menu=self.menubar)
        self.log_message(f"Authenticated as {username}.\n")
        self.command_input.focus_set()
        return True

    def change_cli_account(self):
        """Create or change the persistent pyOS account."""
        if self._locked or self._closing:
            return
        try:
            if has_account() and not self.ensure_authenticated():
                return
            username = change_credentials_dialog(self.root)
        except (CredentialStoreError, ConfigurationError, JSONPersistenceError, StorageOwnershipError) as error:
            messagebox.showerror(
                "pyOS Account Recovery Required",
                f"pyOS detected invalid account state and made no changes:\n\n{error}",
                parent=self.root,
            )
            return
        if username:
            self.authenticated = True
            self.authenticated_username = username
            self.user_var.set(f"User: {username}")
            self.log_message("Account credentials changed.\n")
    
    def format_display_path(self, full_path):
        """Format path for display (replace drive root with 'root\\')"""
        # Replace the full Windows path with a simplified display
        if full_path.startswith(self.drives["C"]):
            relative = full_path[len(self.drives["C"]):].lstrip("\\")
            return f"root\\{relative}" if relative else "root\\"
        elif full_path.startswith(self.drives["A"]):
            relative = full_path[len(self.drives["A"]):].lstrip("\\")
            return f"root\\{relative}" if relative else "root\\"
        elif full_path.startswith(self.drives["B"]):
            relative = full_path[len(self.drives["B"]):].lstrip("\\")
            return f"root\\{relative}" if relative else "root\\"
        return full_path
    
    def switch_drive(self, drive):
        """Switch to different drive"""
        if self._locked or self._closing:
            return
        if drive in self.drives:
            task = self._current_command_task()
            if task is not None:
                self._commit_task_directory(task, self.drives[drive], drive)
                self.log_message(f"Switched to Drive {drive}:\n")
                return
            self.current_drive = drive
            self.current_directory = self.drives[drive]
            self._post_ui(self._update_directory_widgets)
            self.log_message(f"Switched to Drive {drive}:\n")
        else:
            self.log_message(f"Drive {drive}: not found\n")

    def _update_directory_widgets(self):
        if not self._on_ui_thread():
            self._post_ui(self._update_directory_widgets)
            return
        if self._closing:
            return
        self.drive_var.set(f"Drive: {self.current_drive}:")
        self.path_var.set(self.format_display_path(self.current_directory))
        self.refresh_files()
    
    def show_drive_info(self):
        """Show drive information"""
        info = "\n=== DRIVE INFORMATION ===\n\n"
        
        for drive_letter, drive_path in self.drives.items():
            try:
                if os.path.exists(drive_path):
                    if drive_letter == "A":
                        task = self._current_command_task()
                        size = self.drive_a.get_usage(task.cancel_event if task else None)
                        info += f"Drive {drive_letter}: (Session temporary)\n"
                        info += f"  Location: {drive_path}\n"
                        info += f"  Used: {size / (1024*1024):.2f} MB\n"
                    elif drive_letter == "B":
                        task = self._current_command_task()
                        size = self.drive_b.get_usage(task.cancel_event if task else None)
                        drive_type = "Permanent (Disk)"
                        info += f"Drive {drive_letter}: (Permanent)\n"
                        info += f"  Location: {drive_path}\n"
                        info += f"  Used: {size / (1024*1024):.2f} MB\n"
                    else:
                        try:
                            total, used, free = shutil.disk_usage(drive_path)
                            info += f"Drive {drive_letter}:\n"
                            info += f"  Total: {total / (1024**3):.2f} GB\n"
                            info += f"  Used: {used / (1024**3):.2f} GB\n"
                            info += f"  Free: {free / (1024**3):.2f} GB\n"
                        except:
                            info += f"Drive {drive_letter}: Access denied\n"
                    
                    info += f"  Files: {len(os.listdir(drive_path))}\n\n"
            except Exception as e:
                info += f"Drive {drive_letter}: Error - {e}\n\n"
        
        self.log_message(info)
    
    def show_network_status(self):
        """Show network status"""
        if not check_psutil():
            self.log_message("\npsutil not installed. Run: pip install psutil\n")
            return
        
        import psutil
        info = "\n=== NETWORK STATUS ===\n\n"
        
        try:
            # Network interfaces
            net_if_addrs = psutil.net_if_addrs()
            for interface, addrs in net_if_addrs.items():
                info += f"Interface: {interface}\n"
                for addr in addrs:
                    info += f"  {addr.family.name}: {addr.address}\n"
                info += "\n"
            
            # Network IO stats
            net_io = psutil.net_io_counters()
            info += f"Bytes Sent: {net_io.bytes_sent / (1024**2):.2f} MB\n"
            info += f"Bytes Received: {net_io.bytes_recv / (1024**2):.2f} MB\n"
            info += f"Packets Sent: {net_io.packets_sent}\n"
            info += f"Packets Received: {net_io.packets_recv}\n"
        except Exception as e:
            info += f"Error: {e}\n"
        
        self.log_message(info + "\n")
    
    def show_ipconfig(self):
        """Show IP configuration"""
        info = "\n=== IP CONFIGURATION ===\n\n"
        
        try:
            hostname = socket.gethostname()
            ip_address = socket.gethostbyname(hostname)
            
            info += f"Hostname: {hostname}\n"
            info += f"IP Address: {ip_address}\n\n"
            
            # Get detailed network info if psutil available
            if check_psutil():
                import psutil
                net_if_addrs = psutil.net_if_addrs()
                for interface, addrs in net_if_addrs.items():
                    info += f"{interface}:\n"
                    for addr in addrs:
                        if addr.family.name == "AF_INET":
                            info += f"  IPv4: {addr.address}\n"
                            info += f"  Netmask: {addr.netmask}\n"
                        elif addr.family.name == "AF_INET6":
                            info += f"  IPv6: {addr.address}\n"
                    info += "\n"
        except Exception as e:
            info += f"Error: {e}\n"
        
        self.log_message(info)
    
    def refresh_files(self):
        """Refresh file list based on current directory"""
        if not self._on_ui_thread():
            self._post_command_ui(self.refresh_files)
            return
        if self._closing or self._locked:
            return
        self.file_listbox.delete(0, tk.END)
        
        try:
            # Add parent directory option
            self.file_listbox.insert(tk.END, "[..]")
            
            items = sorted(os.listdir(self.current_directory))
            for item in items:
                full_path = os.path.join(self.current_directory, item)
                if os.path.isdir(full_path):
                    self.file_listbox.insert(tk.END, f"[D] {item}")
                else:
                    self.file_listbox.insert(tk.END, f"[F] {item}")
        except PermissionError:
            self.log_message(f"Permission denied: {self.current_directory}\n")
    
    def open_file_or_folder(self, event):
        """Open selected file or folder"""
        if self._locked or self._closing:
            return
        selection = self.file_listbox.curselection()
        if not selection:
            return
        
        item = self.file_listbox.get(selection[0])
        
        if item == "[..]":
            self.current_directory = os.path.dirname(self.current_directory)
        else:
            item_name = item.replace("[D] ", "").replace("[F] ", "")
            full_path = os.path.join(self.current_directory, item_name)
            
            if os.path.isdir(full_path):
                self.current_directory = full_path
            else:
                try:
                    if os.name == 'nt':
                        os.startfile(full_path)
                    elif sys.platform == "darwin":
                        self._spawn_process(["open", full_path])
                    else:
                        self._spawn_process(["xdg-open", full_path])
                    self.log_message(f"Opening: {full_path}\n")
                except Exception as e:
                    self.log_message(f"Error opening file: {e}\n")
        
        self.path_var.set(self.format_display_path(self.current_directory))
        self.refresh_files()
    
    def delete_file(self):
        """Delete selected file or folder"""
        if self._locked or self._closing:
            return
        selection = self.file_listbox.curselection()
        if not selection:
            return
        
        item = self.file_listbox.get(selection[0])
        item_name = item.replace("[D] ", "").replace("[F] ", "")
        full_path = os.path.join(self.current_directory, item_name)
        
        if messagebox.askyesno("Confirm Delete", f"Delete {item_name}?"):
            try:
                if os.path.isdir(full_path):
                    import shutil
                    shutil.rmtree(full_path)
                    self.log_message(f"Deleted directory: {item_name}\n")
                else:
                    os.remove(full_path)
                    self.log_message(f"Deleted file: {item_name}\n")
                self.refresh_files()
            except Exception as e:
                self.log_message(f"Error deleting: {e}\n")
    
    def open_directory(self):
        """Open directory browser dialog"""
        if self._locked or self._closing:
            return
        directory = filedialog.askdirectory()
        if directory:
            self.current_directory = directory
            self.path_var.set(self.format_display_path(self.current_directory))
            self.refresh_files()
    
    def execute_command(self):
        """Execute command from input"""
        if self._locked or self._closing:
            return
        command = self.input_var.get().strip()
        if not command:
            return
        if command.casefold() == "stop":
            self.input_var.set("")
            self.stop_active_command()
            return
        if not self.ensure_authenticated():
            return
        task = CommandTask(
            command=command,
            working_directory=os.path.abspath(self.current_directory),
            drive=self.current_drive,
            auth_generation=self._auth_generation,
        )
        try:
            self._command_tasks.put_nowait(task)
        except queue.Full:
            self.log_message(
                f"Command queue is full ({self.COMMAND_QUEUE_LIMIT} waiting). "
                "Wait for a command to finish and try again.\n"
            )
            return
        
        self.log_message(f"{self.format_display_path(task.working_directory)}> {command}\n")
        self.command_history.append(command)
        if len(self.command_history) > 1000:
            del self.command_history[:-1000]
        self.history_index = len(self.command_history)
        self.input_var.set("")
    
    def _run_command(self, command):
        """Execute command and display output"""
        try:
            task = self._require_active_task()
            try:
                parsed = shlex.split(command, posix=os.name != "nt")
            except ValueError as error:
                self.log_message(f"Command parsing error: {error}\n")
                return
            command_name = parsed[0].lower() if parsed else ""
            command_args = parsed[1:]
            policy = self._command_policy(command_name)
            optional_app = policy.get("optional_app")
            if optional_app and not self.is_app_enabled(optional_app):
                self.log_message(
                    f"pyOS {optional_app} is disabled in Setup and cannot be used.\n"
                )
                return

            # Built-in commands
            if command.lower().startswith("cd "):
                path = command[3:].strip().strip('"')
                if path == "..":
                    self._commit_task_directory(
                        task, os.path.dirname(task.working_directory), task.drive
                    )
                elif path.upper().startswith("A:") or path.upper().startswith("B:") or path.upper().startswith("C:"):
                    drive = path[0].upper()
                    if drive in self.drives:
                        destination = self.drives[drive]
                        if len(path) > 2:
                            subpath = path[2:].lstrip("\\")
                            if subpath:
                                full_path = os.path.join(self.drives[drive], subpath)
                                if os.path.isdir(full_path):
                                    destination = full_path
                                else:
                                    self.log_message(f"The path does not exist: {path}\n")
                                    return
                        self._commit_task_directory(task, destination, drive)
                        self.log_message(f"Switched to Drive {drive}:\n")
                    return
                else:
                    candidate = path if os.path.isabs(path) else os.path.join(task.working_directory, path)
                    if os.path.isdir(candidate):
                        self._commit_task_directory(task, candidate, task.drive)
                    else:
                        self.log_message(f"The path does not exist: {path}\n")
            
            elif command.lower() == "drives":
                output = f"Available Drives:\n"
                output += f"C: - User Home Directory\n"
                output += f"A: - Session-temporary storage (cleared on exit)\n"
                output += f"B: - Permanent Storage\n"
                self.log_message(output + "\n")

            elif command_name in {"driveinfo", "drive_info"}:
                self.log_message("\nVIRTUAL DRIVES\n" + "-" * 32 + "\n")
                for letter, path in self.drives.items():
                    kind = {"A": "temporary", "B": "persistent", "C": "home"}[letter]
                    self.log_message(f"{letter}:  {kind:<10} {path}\n")
                self.log_message("\n")

            elif command_name in {"open", "start"}:
                if not command_args:
                    self.log_message("Usage: open <file-or-folder>\n")
                else:
                    self._open_path(" ".join(command_args))

            elif command_name in {"explorer", "files"}:
                target = " ".join(command_args) if command_args else self._working_directory()
                self._open_explorer(self._resolve_path(target))

            elif command_name in {"play", "media"}:
                if not command_args:
                    self.log_message("Usage: play <audio-or-video-file>\n")
                else:
                    self._play_media(" ".join(command_args))

            elif command_name in {
                "apps", "games", "snake", "sudoku", "chess", "messenger", "calculator",
                "calc", "images", "imageviewer", "notepad", "editor", "ide",
                "filemanager", "desktop_browser", "desktop_media", "pyos_settings",
                "dispenser", "weather", "news", "pyai", "modding",
            }:
                aliases = {
                    "calc": "calculator", "imageviewer": "images", "filemanager": "files",
                    "desktop_browser": "browser", "desktop_media": "media",
                    "pyos_settings": "settings",
                }
                if command_name == "apps":
                    commands = [
                        ("filemanager", "files"), ("games", "games"),
                        ("snake", "snake"), ("sudoku", "sudoku"), ("chess", "chess"),
                        ("messenger", "messenger"), ("calculator", "calculator"),
                        ("images", "images"), ("notepad", "notepad"),
                        ("editor", "editor"), ("desktop_media", "media"),
                        ("ide", "ide"), ("desktop_browser", "browser"),
                        ("dispenser", "dispenser"), ("pyos_settings", "settings"),
                        ("weather", "weather"), ("news", "news"),
                        ("pyai", "pyai"), ("modding", "modding"),
                    ]
                    available = [label for label, app_id in commands if self.is_app_enabled(app_id)]
                    self.log_message("Desktop apps: " + ", ".join(available) + "\n")
                else:
                    app_name = aliases.get(command_name, command_name)
                    self._post_command_ui(self.open_desktop_app, app_name)

            elif command_name == "browser":
                url = " ".join(command_args) if command_args else "https://www.google.com"
                if not self.is_app_enabled("browser"):
                    self.log_message("pyOS browser is disabled in Setup and cannot be launched.\n")
                else:
                    self._post_command_ui(self.open_browser_inspector, url)
                    self.log_message(f"Opened browser inspector: {url}\n")

            elif command_name == "browse":
                if not command_args:
                    self.log_message("Usage: browse <url>\n")
                else:
                    url = " ".join(command_args)
                    if "://" not in url:
                        url = "https://" + url
                    self._perform_mutation(lambda: webbrowser.open(url))
                    self.log_message(f"Opened browser: {url}\n")

            elif command_name == "inspect":
                if not command_args:
                    self.log_message("Usage: inspect <url>\n")
                else:
                    self._inspect_page(" ".join(command_args))

            elif command_name in {"savepage", "download_page"}:
                if not command_args:
                    self.log_message("Usage: savepage <url> [filename]\n")
                else:
                    filename = command_args[1] if len(command_args) > 1 else None
                    self._save_page(command_args[0], filename)

            elif command_name == "history":
                for index, previous in enumerate(self.command_history, 1):
                    self.log_message(f"{index:>4}  {previous}\n")

            elif command_name == "hash":
                if not command_args:
                    self.log_message("Usage: hash <file> [md5|sha1|sha256|sha512]\n")
                else:
                    algorithm = command_args[1].lower() if len(command_args) > 1 else "sha256"
                    self._hash_file(command_args[0], algorithm)

            elif command_name == "date":
                self.log_message(datetime.now().strftime("%Y-%m-%d\n"))

            elif command_name == "time":
                self.log_message(datetime.now().strftime("%H:%M:%S\n"))

            elif command_name in {"powershell", "ps", "wsl"}:
                shell_command = command.partition(" ")[2].strip()
                self._run_host_shell(command_name, shell_command)

            elif command_name == "whoami":
                self.log_message(f"{os.getenv('USERNAME') or os.getenv('USER') or 'unknown'}\n")

            elif command_name in {"gui_settings", "desktop_settings"}:
                self._show_gui_settings()

            elif command_name == "monochrome":
                def restore_monochrome():
                    self.theme.settings = self.theme.defaults.copy()
                    self.theme.save_settings()
                self._perform_mutation(restore_monochrome)
                self._post_command_ui(self.apply_theme_changes)
                self.log_message("Monochrome theme restored.\n")

            elif command_name in {"exit", "quit"}:
                self._post_command_ui(self.shutdown)
            
            elif command.lower() == "cls" or command.lower() == "clear":
                self.clear_console()
            
            elif command.lower() == "dir" or command.lower() == "ls":
                self._list_directory()
            
            elif command.lower() == "tree":
                self._show_tree()
            
            elif command.lower().startswith("mkdir "):
                dirname = command[6:].strip().strip('"')
                path = os.path.join(self._working_directory(), dirname)
                self._perform_mutation(lambda: os.makedirs(path, exist_ok=True))
                self.log_message(f"Directory created: {dirname}\n")
                self.refresh_files()
            
            elif command.lower().startswith("del ") or command.lower().startswith("rm "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                if os.path.exists(path):
                    self._remove_path_cancellable(path)
                    self.log_message(f"Deleted: {filename}\n")
                    self.refresh_files()
                else:
                    self.log_message(f"File not found: {filename}\n")
            
            elif command.lower().startswith("copy ") or command.lower().startswith("cp "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    src = parts[1].strip('"')
                    dst = parts[2].strip('"')
                    src_path = os.path.join(self._working_directory(), src)
                    dst_path = os.path.join(self._working_directory(), dst)
                    self._copy_file_cancellable(src_path, dst_path)
                    self.log_message(f"Copied {src} to {dst}\n")
                    self.refresh_files()
            
            elif command.lower().startswith("move ") or command.lower().startswith("mv "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    src = parts[1].strip('"')
                    dst = parts[2].strip('"')
                    src_path = os.path.join(self._working_directory(), src)
                    dst_path = os.path.join(self._working_directory(), dst)
                    self._move_path_cancellable(src_path, dst_path)
                    self.log_message(f"Moved {src} to {dst}\n")
                    self.refresh_files()
            
            elif command.lower() == "pwd":
                self.log_message(f"{self._working_directory()}\n")
            
            elif command.lower().startswith("echo "):
                text = command[5:].strip()
                self.log_message(f"{text}\n")
            
            elif command.lower().startswith("type ") or command.lower().startswith("cat "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                if os.path.exists(path) and os.path.isfile(path):
                    with open(path, 'r', errors='ignore') as f:
                        contents = f.read(1_000_001)
                    self._require_active_task()
                    if len(contents) > 1_000_000:
                        contents = contents[:1_000_000] + "\n[Output truncated at 1,000,000 characters]"
                    self.log_message(contents + "\n")
                else:
                    self.log_message(f"File not found: {filename}\n")
            
            elif command.lower().startswith("write ") or command.lower().startswith("nano "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._open_write_dialog(filename, path, task.auth_generation)
             
            elif command.lower().startswith("append "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._open_append_dialog(filename, path, task.auth_generation)
            
            elif command.lower().startswith("rename "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    old_name = parts[1].strip('"')
                    new_name = parts[2].strip('"')
                    old_path = os.path.join(self._working_directory(), old_name)
                    new_path = os.path.join(self._working_directory(), new_name)
                    try:
                        self._perform_mutation(lambda: os.rename(old_path, new_path))
                        self.log_message(f"Renamed: {old_name} → {new_name}\n")
                        self.refresh_files()
                    except Exception as e:
                        self.log_message(f"Error renaming file: {e}\n")
                else:
                    self.log_message("Usage: rename <oldname> <newname>\n")
            
            elif command.lower().startswith("info "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._show_file_info(path)
            
            elif command.lower().startswith("lines ") or command.lower().startswith("wc "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._count_lines(path)
            
            elif command.lower().startswith("grep ") or command.lower().startswith("search_text "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    pattern = parts[1].strip('"')
                    filename = parts[2].strip('"')
                    path = os.path.join(self._working_directory(), filename)
                    self._search_text_in_file(pattern, path)
                else:
                    self.log_message("Usage: grep <pattern> <filename>\n")
            
            elif command.lower().startswith("touch "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                try:
                    existed = os.path.exists(path)
                    def touch_path():
                        if existed:
                            os.utime(path, None)
                        else:
                            Path(path).touch()
                    self._perform_mutation(touch_path)
                    self.log_message(
                        f"File {'touched (timestamp updated)' if existed else 'created'}: {filename}\n"
                    )
                    self.refresh_files()
                except Exception as e:
                    self.log_message(f"Error: {e}\n")
            
            elif command.lower().startswith("head "):
                parts = command.split(maxsplit=2)
                filename = parts[1].strip('"')
                lines = 10
                if len(parts) > 2:
                    try:
                        lines = int(parts[2])
                    except ValueError:
                        pass
                path = os.path.join(self._working_directory(), filename)
                self._show_head(path, lines)
            
            elif command.lower().startswith("tail "):
                parts = command.split(maxsplit=2)
                filename = parts[1].strip('"')
                lines = 10
                if len(parts) > 2:
                    try:
                        lines = int(parts[2])
                    except ValueError:
                        pass
                path = os.path.join(self._working_directory(), filename)
                self._show_tail(path, lines)
            
            elif command.lower().startswith("archive "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._perform_mutation(lambda: self._create_zip_archive(path))
            
            elif command.lower().startswith("extract "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._perform_mutation(lambda: self._extract_zip(path))
            
            elif command.lower() == "files_only":
                self._list_files_only()
            
            elif command.lower() == "dirs_only":
                self._list_dirs_only()
            
            elif command.lower().startswith("hexdump ") or command.lower().startswith("xxd "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self._working_directory(), filename)
                self._show_hexdump(path)
            
            elif command.lower() == "ipconfig" or command.lower() == "ifconfig":
                self.show_ipconfig()
            
            elif command.lower() == "netstat":
                self._show_netstat()
            
            elif command.lower().startswith("ping "):
                host = command[5:].strip()
                self._ping(host)
            
            elif command.lower() == "network":
                self.show_network_status()
            
            elif command.lower().startswith("download "):
                url = command[9:].strip().strip('"')
                self._download(url)
            
            elif command.lower() == "sysinfo":
                self._show_sysinfo()
            
            elif command.lower() == "diskspace":
                self._show_diskspace()
            
            elif command.lower() == "tasklist" or command.lower() == "ps":
                self._show_processes()
            
            elif command.lower().startswith("search ") or command.lower().startswith("find "):
                pattern = command.split(maxsplit=1)[1].strip().strip('"')
                self._search_files(pattern)
            
            elif command.lower() == "theme" or command.lower() == "settings":
                self.open_theme_settings()
            
            elif command.lower().startswith("color "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    color_type = parts[1].lower()
                    color_value = parts[2]
                    if color_type == "console_bg":
                        setting_key = "console_bg"
                    elif color_type == "console_fg":
                        setting_key = "console_fg"
                    elif color_type == "list_bg":
                        setting_key = "list_bg"
                    elif color_type == "list_fg":
                        setting_key = "list_fg"
                    else:
                        self.log_message("Usage: color [console_bg|console_fg|list_bg|list_fg] #HEXCOLOR\n")
                        return
                    self._perform_mutation(
                        lambda: self.theme.settings.__setitem__(setting_key, color_value)
                    )
                    self.apply_theme_changes()
                    self.log_message(f"Color {color_type} changed to {color_value}\n")
                else:
                    self.log_message("Usage: color [console_bg|console_fg|list_bg|list_fg] #HEXCOLOR\n")
            
            elif command.lower().startswith("font "):
                font_name = command[5:].strip()
                self._perform_mutation(
                    lambda: self.theme.settings.__setitem__("console_font", font_name)
                )
                self.apply_theme_changes()
                self.log_message(f"Font changed to {font_name}\n")
            
            elif command.lower().startswith("fontsize "):
                try:
                    size = int(command.split(maxsplit=1)[1])
                    if 8 <= size <= 24:
                        self._perform_mutation(
                            lambda: self.theme.settings.__setitem__("console_fontsize", size)
                        )
                        self.apply_theme_changes()
                        self.log_message(f"Font size changed to {size}\n")
                    else:
                        self.log_message("Font size must be between 8 and 24\n")
                except ValueError:
                    self.log_message("Usage: fontsize <number>\n")
            
            elif command.lower() == "theme_info":
                info = "\n=== THEME INFORMATION ===\n\n"
                info += f"Console Background: {self.theme.settings['console_bg']}\n"
                info += f"Console Text: {self.theme.settings['console_fg']}\n"
                info += f"Console Font: {self.theme.settings['console_font']}\n"
                info += f"Console Font Size: {self.theme.settings['console_fontsize']}\n"
                info += f"List Background: {self.theme.settings['list_bg']}\n"
                info += f"List Text: {self.theme.settings['list_fg']}\n"
                self.log_message(info + "\n")
            
            elif command.lower() == "deskgui":
                self.open_desktop_gui()
            
            elif command.lower() == "help" or command.lower() == "commands":
                self.show_commands()
            
            else:
                # System command
                if not policy.get("external"):
                    self.log_message(f"Invalid or incomplete usage for: {command_name}\n")
                    return
                try:
                    returncode, stdout, stderr = self._run_tracked_capture(
                        command,
                        shell=True,
                        cwd=self._working_directory(),
                        timeout=15
                    )
                    if stdout:
                        self.log_message(stdout)
                    if stderr:
                        self.log_message(stderr)
                    if not stdout and not stderr:
                        self.log_message("Command executed successfully.\n")
                except subprocess.TimeoutExpired:
                    self.log_message("Command timed out after 15 seconds.\n")
                except Exception as e:
                    self.log_message(f"Error: {str(e)}\n")
        
        except CommandCancelled:
            raise
        except Exception as e:
            self.log_message(f"Error: {str(e)}\n")
    
    def _resolve_path(self, value):
        """Resolve relative and virtual-drive paths used by launcher commands."""
        value = str(value).strip().strip('"')
        if len(value) >= 2 and value[1] == ":" and value[0].upper() in self.drives:
            drive_root = self.drives[value[0].upper()]
            remainder = value[2:].lstrip("\\/")
            return os.path.normpath(os.path.join(drive_root, remainder))
        if os.path.isabs(value):
            return os.path.normpath(value)
        return os.path.normpath(os.path.join(self._working_directory(), value))

    def _copy_file_cancellable(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        if not source.is_file():
            raise OSError(f"Source is not a file: {source}")
        if destination.is_dir():
            destination /= source.name
        self._perform_mutation(lambda: destination.parent.mkdir(parents=True, exist_ok=True))
        with self._mutation_lock:
            self._require_active_task()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".copy", dir=str(destination.parent)
            )
        temporary = Path(temporary_name)
        try:
            with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
                while True:
                    self._require_active_task()
                    block = input_stream.read(1024 * 1024)
                    if not block:
                        break
                    self._perform_mutation(lambda current=block: output_stream.write(current))
                def sync_copy():
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                self._perform_mutation(sync_copy)
            self._perform_mutation(lambda: shutil.copystat(source, temporary))
            self._perform_mutation(lambda: os.replace(temporary, destination))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _move_path_cancellable(self, source, destination):
        source = Path(source)
        destination = Path(destination)
        target = destination / source.name if destination.is_dir() else destination
        try:
            self._perform_mutation(lambda: os.replace(source, target))
            return
        except OSError as error:
            if error.errno != errno.EXDEV or not source.is_file():
                raise
        self._copy_file_cancellable(source, target)
        self._perform_mutation(source.unlink)

    def _remove_path_cancellable(self, path):
        path = Path(path)
        is_junction = hasattr(path, "is_junction") and path.is_junction()
        if is_junction:
            self._perform_mutation(path.rmdir)
            return
        if not path.is_dir() or path.is_symlink():
            self._perform_mutation(path.unlink)
            return
        for root, directories, files in os.walk(path, topdown=False, followlinks=False):
            self._require_active_task()
            root_path = Path(root)
            for name in files:
                self._perform_mutation((root_path / name).unlink)
            for name in directories:
                child = root_path / name
                if hasattr(child, "is_junction") and child.is_junction():
                    self._perform_mutation(child.rmdir)
                elif child.is_symlink():
                    self._perform_mutation(child.unlink)
                else:
                    self._perform_mutation(child.rmdir)
        self._perform_mutation(path.rmdir)

    def _open_path(self, value):
        path = self._resolve_path(value)
        if not os.path.exists(path):
            self.log_message(f"Path not found: {value}\n")
            return
        try:
            if os.name == "nt":
                self._perform_mutation(lambda: os.startfile(path))
            elif sys.platform == "darwin":
                self._spawn_process(["open", path])
            else:
                self._spawn_process(["xdg-open", path])
            self.log_message(f"Opened: {path}\n")
        except OSError as error:
            self.log_message(f"Could not open path: {error}\n")

    def _open_explorer(self, value):
        path = self._resolve_path(value)
        if not os.path.isdir(path):
            self.log_message(f"Folder not found: {value}\n")
            return
        try:
            if os.name == "nt":
                self._spawn_process(["explorer", path])
            elif sys.platform == "darwin":
                self._spawn_process(["open", path])
            else:
                self._spawn_process(["xdg-open", path])
            self.log_message(f"Opened File Explorer: {path}\n")
        except OSError as error:
            self.log_message(f"Could not open File Explorer: {error}\n")

    def _play_media(self, value):
        if not self.is_app_enabled("media"):
            self.log_message("pyOS media is disabled in Setup and cannot be used.\n")
            return
        path = self._resolve_path(value)
        if not os.path.isfile(path):
            self.log_message(f"Media file not found: {value}\n")
            return
        candidates = [
            shutil.which("vlc"),
            str(Path(os.environ.get("ProgramFiles", "")) / "VideoLAN" / "VLC" / "vlc.exe"),
            str(Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN" / "VLC" / "vlc.exe"),
        ]
        player = next((candidate for candidate in candidates if candidate and os.path.isfile(candidate)), None)
        try:
            if player:
                self._spawn_process([player, path])
            elif os.name == "nt":
                self._perform_mutation(lambda: os.startfile(path))
            else:
                self._open_path(path)
            self.log_message(f"Playing: {path}\n")
        except OSError as error:
            self.log_message(f"Could not play media: {error}\n")

    def _hash_file(self, value, algorithm):
        path = self._resolve_path(value)
        if not os.path.isfile(path):
            self.log_message(f"File not found: {value}\n")
            return
        if algorithm not in {"md5", "sha1", "sha256", "sha512"}:
            self.log_message("Supported algorithms: md5, sha1, sha256, sha512\n")
            return
        digest = hashlib.new(algorithm)
        try:
            with open(path, "rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    self._require_active_task()
                    digest.update(block)
            self.log_message(f"{algorithm.upper()}  {digest.hexdigest()}  {os.path.basename(path)}\n")
        except OSError as error:
            self.log_message(f"Could not hash file: {error}\n")

    def _show_gui_settings(self):
        defaults = {
            "desktop_inverted": False,
            "font_size": 9,
            "clock_24h": True,
            "show_seconds": True,
            "show_hidden_files": False,
            "file_manager_start": "Home",
        }
        try:
            loaded = json.loads(self.gui_settings_file.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Desktop settings must be a JSON object")
            defaults.update({key: loaded[key] for key in defaults if key in loaded})
        except FileNotFoundError:
            pass
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            self.log_message(
                "Desktop settings recovery is required; invalid state was not ignored: "
                f"{error}\nFile: {self.gui_settings_file}\n"
            )
            return
        self.log_message("\nDESKTOP GUI SETTINGS\n")
        self.log_message("-" * 32 + "\n")
        for key, value in defaults.items():
            self.log_message(f"{key:<24} {value}\n")
        self.log_message(f"\nFile: {self.gui_settings_file}\n\n")

    def _ping(self, host):
        """Ping a host"""
        try:
            self.log_message(f"Pinging {host}...\n")
            args = ["ping", "-n" if os.name == "nt" else "-c", "4", host]
            _returncode, stdout, stderr = self._run_tracked_capture(
                args,
                timeout=10
            )
            self.log_message(stdout or stderr)
        except CommandCancelled:
            raise
        except Exception as e:
            self.log_message(f"Ping failed: {e}\n")
    
    def _show_netstat(self):
        """Show network statistics"""
        try:
            self.log_message("\n=== NETWORK STATISTICS ===\n\n")
            
            if not check_psutil():
                self.log_message("psutil not installed. Run: pip install psutil\n")
                return
            
            import psutil
            net_io = psutil.net_io_counters()
            
            output = f"Bytes Sent: {net_io.bytes_sent / (1024**2):.2f} MB\n"
            output += f"Bytes Received: {net_io.bytes_recv / (1024**2):.2f} MB\n"
            output += f"Packets Sent: {net_io.packets_sent}\n"
            output += f"Packets Received: {net_io.packets_recv}\n"
            output += f"Dropped In: {net_io.dropin}\n"
            output += f"Dropped Out: {net_io.dropout}\n"
            
            self.log_message(output + "\n")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _show_sysinfo(self):
        """Show system information"""
        info = "\n=== SYSTEM INFORMATION ===\n\n"
        
        try:
            info += f"Platform: {os.name}\n"
            info += f"User: {os.getenv('USERNAME')}\n"
            info += f"Computer: {socket.gethostname()}\n"
            
            if check_psutil():
                import psutil
                info += f"CPU Count: {psutil.cpu_count()}\n"
                info += f"CPU Usage: {psutil.cpu_percent(interval=1)}%\n"
                
                mem = psutil.virtual_memory()
                info += f"Total Memory: {mem.total / (1024**3):.2f} GB\n"
                info += f"Available Memory: {mem.available / (1024**3):.2f} GB\n"
                info += f"Memory Usage: {mem.percent}%\n"
            
            import sys
            info += f"\nPython Version: {sys.version.split()[0]}\n"
        except Exception as e:
            info += f"Error: {e}\n"
        
        self.log_message(info + "\n")
    
    def _show_processes(self):
        """Show running processes"""
        try:
            if not check_psutil():
                self.log_message("\npsutil not installed. Run: pip install psutil\n")
                return
            
            import psutil
            self.log_message("\n=== RUNNING PROCESSES ===\n\n")
            self.log_message(f"{'PID':<10} {'Name':<30} {'Memory (MB)':<15}\n")
            self.log_message("-" * 55 + "\n")
            
            for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
                self._require_active_task()
                try:
                    memory_mb = proc.info['memory_info'].rss / (1024 * 1024)
                    self.log_message(f"{proc.info['pid']:<10} {proc.info['name']:<30} {memory_mb:<15.2f}\n")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    @staticmethod
    def _normalize_url(url):
        url = url.strip().strip('"')
        return url if "://" in url else "https://" + url

    def _fetch_page(self, url, cancel_event=None):
        """Fetch a web page and return its final URL, headers, bytes, and decoded source."""
        url = self._normalize_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "pyOS Browser Inspector/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            chunks = []
            received = 0
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CommandCancelled()
                task = self._current_command_task()
                if task is not None:
                    self._require_active_task(task)
                chunk = response.read(min(64 * 1024, 10 * 1024 * 1024 + 1 - received))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > 10 * 1024 * 1024:
                    raise ValueError("Page exceeds the 10 MB inspection limit")
            data = b"".join(chunks)
            encoding = response.headers.get_content_charset() or "utf-8"
            source = data.decode(encoding, errors="replace")
            headers = dict(response.headers.items())
            return response.geturl(), response.status, headers, data, source

    def open_browser_inspector(self, initial_url="https://www.google.com"):
        """Open a monochrome browser source inspector and page downloader."""
        if not self._on_ui_thread():
            self._post_command_ui(self.open_browser_inspector, initial_url)
            return
        if self._locked or self._closing or not self.authenticated:
            return
        if not self.is_app_enabled("browser"):
            self.log_message("pyOS browser is disabled in Setup and cannot be launched.\n")
            return
        window = tk.Toplevel(self.root)
        window.title("pyOS Browser Inspector")
        window.geometry("900x650")
        window.configure(bg="white")

        toolbar = ttk.Frame(window, padding=6)
        toolbar.pack(fill=tk.X)
        url_var = tk.StringVar(value=self._normalize_url(initial_url))
        url_entry = ttk.Entry(toolbar, textvariable=url_var)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))

        notebook = ttk.Notebook(window)
        notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
        source_view = scrolledtext.ScrolledText(
            notebook, wrap=tk.NONE, bg="white", fg="black", insertbackground="black", font=("Courier New", 10)
        )
        headers_view = scrolledtext.ScrolledText(
            notebook, wrap=tk.WORD, bg="white", fg="black", insertbackground="black", font=("Courier New", 10)
        )
        notebook.add(source_view, text="Page Source")
        notebook.add(headers_view, text="Response")
        status_var = tk.StringVar(value="Ready")
        ttk.Label(window, textvariable=status_var, anchor=tk.W).pack(fill=tk.X, padx=8, pady=(0, 6))
        cache = {"url": "", "data": None}
        request_state = {"generation": 0, "task": None, "closed": False}

        def replace_text(widget, text):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text)
            widget.configure(state=tk.DISABLED)

        def request_is_current(generation):
            try:
                exists = bool(window.winfo_exists())
            except tk.TclError:
                exists = False
            return bool(
                not request_state["closed"]
                and generation == request_state["generation"]
                and not self._closing
                and not self._locked
                and self.authenticated
                and exists
            )

        def display_result(generation, result):
            if not request_is_current(generation):
                return
            final_url, status, headers, data, source = result
            cache.update(url=final_url, data=data)
            url_var.set(final_url)
            replace_text(source_view, source)
            metadata = [f"URL: {final_url}", f"Status: {status}", f"Size: {len(data):,} bytes", ""]
            metadata.extend(f"{name}: {value}" for name, value in headers.items())
            replace_text(headers_view, "\n".join(metadata))
            status_var.set(f"Loaded {len(data):,} bytes | HTTP {status}")

        def display_error(generation, error):
            if request_is_current(generation):
                status_var.set(f"Load failed: {error}")

        def fetch():
            if self._locked or self._closing or not self.authenticated:
                return
            if not self.is_app_enabled("browser"):
                status_var.set("Browser is disabled in Setup.")
                return
            previous = request_state["task"]
            if previous is not None:
                previous.cancel_event.set()
            request_state["generation"] += 1
            generation = request_state["generation"]
            cache.update(url="", data=None)
            status_var.set("Loading...")
            source_view.configure(state=tk.NORMAL)
            source_view.delete("1.0", tk.END)
            source_view.insert("1.0", "Loading...")
            source_view.configure(state=tk.DISABLED)
            requested_url = url_var.get()

            submitted = self._submit_auxiliary(
                lambda cancel: self._fetch_page(requested_url, cancel),
                lambda result, current=generation: display_result(current, result),
                lambda error, current=generation: display_error(current, error),
            )
            if submitted is None:
                status_var.set("Browser work queue is full; wait and try again.")
                return
            request_state["task"] = submitted

        def save_cached_page():
            if self._locked or self._closing or not self.authenticated:
                return
            if not self.is_app_enabled("browser"):
                status_var.set("Browser is disabled in Setup.")
                return
            if cache["data"] is None:
                status_var.set("Load a page before saving it.")
                return
            parsed = urllib.parse.urlparse(cache["url"])
            suggested = Path(parsed.path).name or "index.html"
            destination = filedialog.asksaveasfilename(
                parent=window,
                initialdir=self.current_directory,
                initialfile=suggested,
                defaultextension=".html",
                filetypes=(("HTML pages", "*.html;*.htm"), ("All files", "*.*")),
            )
            if not destination:
                return
            try:
                self._atomic_replace_bytes(destination, cache["data"])
                status_var.set(f"Saved: {destination}")
                self.refresh_files()
            except OSError as error:
                status_var.set(f"Save failed: {error}")

        def open_external():
            if self._locked or self._closing or not self.authenticated:
                return
            if not self.is_app_enabled("browser"):
                status_var.set("Browser is disabled in Setup.")
                return
            webbrowser.open(self._normalize_url(url_var.get()))

        def close_window():
            request_state["closed"] = True
            request_state["generation"] += 1
            active = request_state["task"]
            if active is not None:
                active.cancel_event.set()
            try:
                window.destroy()
            except tk.TclError:
                pass

        ttk.Button(toolbar, text="Inspect", command=fetch).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Save Page", command=save_cached_page).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Open External", command=open_external).pack(side=tk.LEFT, padx=3)
        url_entry.bind("<Return>", lambda event: fetch())
        window.protocol("WM_DELETE_WINDOW", close_window)
        fetch()

    def _inspect_page(self, url):
        if not self.is_app_enabled("browser"):
            self.log_message("pyOS browser is disabled in Setup and cannot be used.\n")
            return
        try:
            final_url, status, headers, data, source = self._fetch_page(url)
            self.log_message(f"\nPAGE INSPECTION\n{'-' * 48}\n")
            self.log_message(f"URL: {final_url}\nStatus: {status}\nSize: {len(data):,} bytes\n")
            for name, value in headers.items():
                self.log_message(f"{name}: {value}\n")
            preview = source[:12000]
            self.log_message(f"\nSOURCE PREVIEW\n{'-' * 48}\n{preview}\n")
            if len(source) > len(preview):
                self.log_message("\n[Preview truncated at 12,000 characters]\n")
        except (urllib.error.URLError, OSError, ValueError) as error:
            self.log_message(f"Page inspection failed: {error}\n")

    def _save_page(self, url, filename=None):
        if not self.is_app_enabled("browser"):
            self.log_message("pyOS browser is disabled in Setup and cannot be used.\n")
            return
        try:
            final_url, status, headers, data, source = self._fetch_page(url)
            if not filename:
                filename = Path(urllib.parse.urlparse(final_url).path).name or "index.html"
            destination = self._resolve_path(filename)
            self._atomic_replace_bytes(destination, data)
            self.log_message(f"Saved page: {destination} ({len(data):,} bytes, HTTP {status})\n")
            self.refresh_files()
        except CommandCancelled:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            self.log_message(f"Page download failed: {error}\n")

    def _atomic_replace_bytes(self, destination, data):
        """Write bytes to a unique temporary file, then replace under the lock boundary."""
        destination = Path(destination)
        self._perform_mutation(lambda: destination.parent.mkdir(parents=True, exist_ok=True))
        with self._mutation_lock:
            self._require_active_task()
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
            )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                view = memoryview(data)
                for offset in range(0, len(view), 64 * 1024):
                    task = self._current_command_task()
                    if task is not None:
                        self._require_active_task(task)
                    block = view[offset:offset + 64 * 1024]
                    self._perform_mutation(lambda current=block: stream.write(current))
                def sync_stream():
                    stream.flush()
                    os.fsync(stream.fileno())
                self._perform_mutation(sync_stream)
            self._perform_mutation(lambda: os.replace(temporary, destination))
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _download(self, url):
        """Download file from URL"""
        temporary = None
        try:
            filename = Path(urllib.parse.urlparse(url).path).name or "download.bin"
            filepath = Path(self._working_directory()) / filename
            self._perform_mutation(lambda: filepath.parent.mkdir(parents=True, exist_ok=True))
            with self._mutation_lock:
                self._require_active_task()
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{filepath.name}.", suffix=".download", dir=str(filepath.parent)
                )
            temporary = Path(temporary_name)
            self.log_message(f"Downloading {url}...\n")
            request = urllib.request.Request(url, headers={"User-Agent": "pyOS Command Center/1.0"})
            received = 0
            with os.fdopen(descriptor, "wb") as target, urllib.request.urlopen(request, timeout=15) as response:
                while True:
                    self._require_active_task()
                    block = response.read(64 * 1024)
                    if not block:
                        break
                    received += len(block)
                    if received > 256 * 1024 * 1024:
                        raise ValueError("Download exceeds the 256 MB command limit")
                    self._perform_mutation(lambda current=block: target.write(current))
                def sync_target():
                    target.flush()
                    os.fsync(target.fileno())
                self._perform_mutation(sync_target)
            self._perform_mutation(lambda: os.replace(temporary, filepath))
            temporary = None
            self.log_message(f"Downloaded to: {filepath}\n")
            self.refresh_files()
        except CommandCancelled:
            raise
        except Exception as e:
            self.log_message(f"Download failed: {e}\n")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
    
    def _show_diskspace(self):
        """Show disk space usage"""
        info = "\n=== DISK SPACE ===\n\n"
        
        try:
            total, used, free = shutil.disk_usage(self._working_directory())
            info += f"Drive: {self._working_drive()}:\n"
            info += f"Total: {total / (1024**3):.2f} GB\n"
            info += f"Used: {used / (1024**3):.2f} GB\n"
            info += f"Free: {free / (1024**3):.2f} GB\n"
            info += f"Usage: {(used/total)*100:.1f}%\n"
        except Exception as e:
            info += f"Error: {e}\n"
        
        self.log_message(info + "\n")
    
    def _search_files(self, pattern):
        """Search for files"""
        try:
            self.log_message(f"\nSearching for '{pattern}'...\n\n")
            found = 0
            
            for root, dirs, files in os.walk(self._working_directory()):
                self._require_active_task()
                for file in files:
                    if pattern.lower() in file.lower():
                        filepath = os.path.join(root, file)
                        self.log_message(f"  {filepath}\n")
                        found += 1
                        if found >= 20:
                            self.log_message(f"\n... and more (limited to 20 results)\n")
                            return
            
            self.log_message(f"Found {found} file(s)\n\n")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _show_file_info(self, filepath):
        """Show detailed file information"""
        try:
            if not os.path.exists(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            import stat
            stat_info = os.stat(filepath)
            
            info = f"\n=== FILE INFO: {os.path.basename(filepath)} ===\n\n"
            info += f"Path: {filepath}\n"
            info += f"Size: {stat_info.st_size:,} bytes ({stat_info.st_size / 1024:.2f} KB)\n"
            info += f"Type: {'Directory' if os.path.isdir(filepath) else 'File'}\n"
            
            from datetime import datetime
            modified = datetime.fromtimestamp(stat_info.st_mtime)
            created = datetime.fromtimestamp(stat_info.st_ctime)
            accessed = datetime.fromtimestamp(stat_info.st_atime)
            
            info += f"Created: {created}\n"
            info += f"Modified: {modified}\n"
            info += f"Accessed: {accessed}\n"
            
            # File permissions
            mode = stat_info.st_mode
            permissions = f"{'R' if mode & stat.S_IRUSR else '-'}{'W' if mode & stat.S_IWUSR else '-'}{'X' if mode & stat.S_IXUSR else '-'}"
            info += f"Permissions: {permissions}\n"
            
            info += "\n"
            self.log_message(info)
        except Exception as e:
            self.log_message(f"Error getting file info: {e}\n")
     
    def _open_write_dialog(self, filename, path, auth_generation):
        """Open write dialog on main thread (fixes threading issue)"""
        def show_dialog():
            if (self._locked or self._closing or not self.authenticated
                    or auth_generation != self._auth_generation):
                return
            content = tk.simpledialog.askstring("Write File", f"Enter content for {filename}:")
            if content is not None:
                try:
                    if auth_generation != self._auth_generation:
                        return
                    self._atomic_replace_bytes(path, content.encode("utf-8"))
                    self.log_message(f"File created: {filename}\n")
                    self.refresh_files()
                except Exception as e:
                    self.log_message(f"Error writing file: {e}\n")
        self._post_ui(show_dialog)
     
    def _open_append_dialog(self, filename, path, auth_generation):
        """Open append dialog on main thread (fixes threading issue)"""
        def show_dialog():
            if (self._locked or self._closing or not self.authenticated
                    or auth_generation != self._auth_generation):
                return
            content = tk.simpledialog.askstring("Append to File", f"Enter content to append to {filename}:")
            if content is not None:
                try:
                    if auth_generation != self._auth_generation:
                        return
                    def append_content():
                        with Path(path).open("a", encoding="utf-8") as stream:
                            stream.write(content + "\n")
                    self._perform_mutation(append_content)
                    self.log_message(f"Content appended to: {filename}\n")
                    self.refresh_files()
                except Exception as e:
                    self.log_message(f"Error appending to file: {e}\n")
        self._post_ui(show_dialog)
     
    def _count_lines(self, filepath):
        """Count lines in a file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            with open(filepath, 'r', errors='ignore') as f:
                lines = words = chars = 0
                for line in f:
                    self._require_active_task()
                    lines += 1
                    words += len(line.split())
                    chars += len(line)
            
            self.log_message(f"\n=== FILE STATISTICS: {os.path.basename(filepath)} ===\n\n")
            self.log_message(f"Lines: {lines:,}\n")
            self.log_message(f"Words: {words:,}\n")
            self.log_message(f"Characters: {chars:,}\n\n")
        except Exception as e:
            self.log_message(f"Error counting lines: {e}\n")
    
    def _search_text_in_file(self, pattern, filepath):
        """Search for text pattern in file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            self.log_message(f"\n=== SEARCH RESULTS in {os.path.basename(filepath)} ===\n\n")
            found = 0
            
            with open(filepath, 'r', errors='ignore') as f:
                for line_num, line in enumerate(f, 1):
                    self._require_active_task()
                    if pattern.lower() in line.lower():
                        self.log_message(f"Line {line_num}: {line.rstrip()}\n")
                        found += 1
                        if found >= 50:
                            self.log_message(f"\n... and more (limited to 50 results)\n")
                            break
            
            self.log_message(f"\nFound {found} match(es)\n\n")
        except Exception as e:
            self.log_message(f"Error searching text: {e}\n")
    
    def _show_head(self, filepath, num_lines):
        """Show first N lines of file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            self.log_message(f"\n=== FIRST {num_lines} LINES: {os.path.basename(filepath)} ===\n\n")
            
            with open(filepath, 'r', errors='ignore') as f:
                for i, line in enumerate(f):
                    self._require_active_task()
                    if i >= num_lines:
                        break
                    self.log_message(line)
            
            self.log_message("\n")
        except Exception as e:
            self.log_message(f"Error reading file: {e}\n")
    
    def _show_tail(self, filepath, num_lines):
        """Show last N lines of file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            self.log_message(f"\n=== LAST {num_lines} LINES: {os.path.basename(filepath)} ===\n\n")
            
            with open(filepath, 'r', errors='ignore') as f:
                lines = deque(maxlen=max(0, num_lines))
                for line in f:
                    self._require_active_task()
                    lines.append(line)

            for line in lines:
                self.log_message(line)
            
            self.log_message("\n")
        except Exception as e:
            self.log_message(f"Error reading file: {e}\n")
    
    def _create_zip_archive(self, filepath):
        """Create zip archive of file or directory"""
        try:
            import zipfile
            
            if not os.path.exists(filepath):
                self.log_message(f"File/Directory not found: {filepath}\n")
                return
            
            archive_name = os.path.basename(filepath) + ".zip"
            archive_path = os.path.join(self._working_directory(), archive_name)
            
            self.log_message(f"Creating archive: {archive_name}...\n")
            
            with zipfile.ZipFile(archive_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if os.path.isdir(filepath):
                    for root, dirs, files in os.walk(filepath):
                        for file in files:
                            file_path = os.path.join(root, file)
                            arcname = os.path.relpath(file_path, os.path.dirname(filepath))
                            zipf.write(file_path, arcname)
                else:
                    zipf.write(filepath, os.path.basename(filepath))
            
            size = os.path.getsize(archive_path) / 1024
            self.log_message(f"Archive created: {archive_name} ({size:.2f} KB)\n")
            self.refresh_files()
        except Exception as e:
            self.log_message(f"Error creating archive: {e}\n")
    
    def _extract_zip(self, filepath):
        """Extract zip archive"""
        try:
            import zipfile
            
            if not os.path.exists(filepath) or not filepath.lower().endswith('.zip'):
                self.log_message(f"Invalid zip file: {filepath}\n")
                return
            
            extract_dir = os.path.join(self._working_directory(), os.path.basename(filepath)[:-4])
            os.makedirs(extract_dir, exist_ok=True)
            
            self.log_message(f"Extracting: {os.path.basename(filepath)}...\n")
            
            with zipfile.ZipFile(filepath, 'r') as zipf:
                zipf.extractall(extract_dir)
            
            self.log_message(f"Extracted to: {os.path.basename(extract_dir)}\n")
            self.refresh_files()
        except Exception as e:
            self.log_message(f"Error extracting archive: {e}\n")
    
    def _list_files_only(self):
        """List only files in current directory"""
        try:
            self.log_message("\n=== FILES ONLY ===\n\n")
            working_directory = self._working_directory()
            files = [f for f in os.listdir(working_directory) if os.path.isfile(os.path.join(working_directory, f))]
            
            if not files:
                self.log_message("No files found\n")
                return
            
            for f in sorted(files):
                path = os.path.join(working_directory, f)
                size = os.path.getsize(path)
                self.log_message(f"  {f} ({size:,} bytes)\n")
            
            self.log_message(f"\nTotal: {len(files)} file(s)\n\n")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _list_dirs_only(self):
        """List only directories in current directory"""
        try:
            self.log_message("\n=== DIRECTORIES ONLY ===\n\n")
            working_directory = self._working_directory()
            dirs = [d for d in os.listdir(working_directory) if os.path.isdir(os.path.join(working_directory, d))]
            
            if not dirs:
                self.log_message("No directories found\n")
                return
            
            for d in sorted(dirs):
                path = os.path.join(working_directory, d)
                file_count = len(os.listdir(path))
                self.log_message(f"  {d}/ ({file_count} items)\n")
            
            self.log_message(f"\nTotal: {len(dirs)} director(ies)\n\n")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _show_hexdump(self, filepath):
        """Show hexadecimal dump of file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            self.log_message(f"\n=== HEXDUMP: {os.path.basename(filepath)} ===\n\n")
            
            with open(filepath, 'rb') as f:
                data = f.read(512)  # First 512 bytes
            
            for i in range(0, len(data), 16):
                chunk = data[i:i+16]
                hex_str = ' '.join(f'{b:02x}' for b in chunk)
                ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
                self.log_message(f"{i:08x}  {hex_str:<48}  {ascii_str}\n")
            
            if len(data) == 512:
                self.log_message("\n... (showing first 512 bytes)\n\n")
            else:
                self.log_message("\n")
        except Exception as e:
            self.log_message(f"Error showing hexdump: {e}\n")
    
    def _show_tree(self):
        """Show directory tree"""
        try:
            self.log_message("\n")
            self._tree_walk(self._working_directory(), "")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _tree_walk(self, path, prefix, max_depth=3, current_depth=0):
        """Recursively walk and display directory tree"""
        if current_depth >= max_depth:
            return
        
        try:
            items = sorted(os.listdir(path))
            dirs = [item for item in items if os.path.isdir(os.path.join(path, item))]
            
            for i, d in enumerate(dirs[:10]):
                self._require_active_task()
                is_last = (i == len(dirs) - 1)
                self.log_message(f"{prefix}{'└── ' if is_last else '├── '}{d}/\n")
                new_prefix = prefix + ("    " if is_last else "│   ")
                self._tree_walk(os.path.join(path, d), new_prefix, max_depth, current_depth + 1)
        except PermissionError:
            pass
    
    def _list_directory(self):
        """List files and directories"""
        try:
            working_directory = self._working_directory()
            items = sorted(os.listdir(working_directory))
            output = "\n Directory listing:\n\n"
            
            for item in items:
                self._require_active_task()
                full_path = os.path.join(working_directory, item)
                if os.path.isdir(full_path):
                    output += f"  [DIR]  {item}\n"
                else:
                    size = os.path.getsize(full_path)
                    output += f"  [FILE] {item} ({size} bytes)\n"
            
            self.log_message(output + "\n")
        except Exception as e:
            self.log_message(f"Error listing directory: {e}\n")
    
    def log_message(self, message):
        """Add message to console"""
        if not self._on_ui_thread():
            self._post_command_ui(self.log_message, message)
            return
        if self._closing:
            return
        self.console.config(state='normal')
        self.console.insert(tk.END, message)
        self.console.see(tk.END)
        self.console.config(state='disabled')
    
    def clear_console(self):
        """Clear console output"""
        if not self._on_ui_thread():
            self._post_command_ui(self.clear_console)
            return
        if self._closing or self._locked:
            return
        self.console.config(state='normal')
        self.console.delete(1.0, tk.END)
        self.console.config(state='disabled')
    
    def history_up(self, event):
        """Navigate command history up"""
        if self.history_index > 0:
            self.history_index -= 1
            self.input_var.set(self.command_history[self.history_index])
            self.command_input.icursor(tk.END)
        return 'break'
    
    def history_down(self, event):
        """Navigate command history down"""
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.input_var.set(self.command_history[self.history_index])
            self.command_input.icursor(tk.END)
        else:
            self.history_index = len(self.command_history)
            self.input_var.set("")
        return 'break'
    
    def open_desktop_gui(self):
        """Open the desktop GUI version"""
        if not self._on_ui_thread():
            self._post_command_ui(self.open_desktop_gui)
            return
        if self._locked or self._closing:
            return
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gui_path = os.path.join(script_dir, "pyOSgui.py")
        
        try:
            self._spawn_process([sys.executable, gui_path])
            self.log_message("Desktop GUI opening in new window...\n")
        except Exception as e:
            self.log_message(f"Error opening desktop GUI: {e}\n")

    def open_desktop_app(self, app_name):
        """Open one pyOS desktop application directly from the command center."""
        if not self._on_ui_thread():
            self._post_command_ui(self.open_desktop_app, app_name)
            return
        if self._locked or self._closing:
            return
        if not self.is_app_enabled(app_name):
            optional_id = self._optional_app_id(app_name)
            self.log_message(
                f"pyOS {optional_id or app_name} is disabled in Setup and cannot be launched.\n"
            )
            return
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gui_path = os.path.join(script_dir, "pyOSgui.py")
        try:
            self._spawn_process([sys.executable, gui_path, "--app", app_name])
            self.log_message(f"Opening pyOS {app_name.replace('-', ' ')}...\n")
        except Exception as error:
            self.log_message(f"Could not open pyOS {app_name}: {error}\n")
    
    def show_commands_legacy(self):
        """Show available commands"""
        commands = """
╔════════════════════════════════════════════════════════════════════╗
║                   Python OS - Available Commands                   ║
╚════════════════════════════════════════════════════════════════════╝

FILE NAVIGATION:
  cd <path>              - Change directory (use A:, B:, C: for drives)
  pwd                    - Print working directory
  dir / ls               - List files and directories
  tree                   - Show directory tree
  drives                 - Show available drives

FILE OPERATIONS:
  mkdir <dirname>        - Create directory
  del / rm <file>        - Delete file
  copy / cp <src> <dst>  - Copy file
  move / mv <src> <dst>  - Move file
  type / cat <file>      - Display file contents
  write / nano <file>    - Write to file
  append <file>          - Append text to file
  rename <old> <new>     - Rename file
  touch <file>           - Create empty file or update timestamp
  info <file>            - Show file information
  lines / wc <file>      - Count lines/words/chars in file
  grep <pattern> <file>  - Search text in file
  head <file> [N]        - Show first N lines (default: 10)
  tail <file> [N]        - Show last N lines (default: 10)
  hexdump / xxd <file>   - Show hex dump of file (first 512 bytes)
  archive <file>         - Create zip archive
  extract <file.zip>     - Extract zip file
  files_only             - List only files
  dirs_only              - List only directories
  search / find <pattern> - Search for files

DRIVES:
  A: - Session-temporary storage (cleared on exit)
  B: - Permanent Storage (persistent files)
  C: - User Home Directory

NETWORK COMMANDS:
  ipconfig / ifconfig    - Show IP configuration
  ping <host>            - Ping a host
  netstat                - Show network statistics
  network                - Show network status
  download <url>         - Download file from URL

SYSTEM COMMANDS:
  sysinfo                - Show system information
  diskspace              - Show disk usage
  tasklist / ps          - Show running processes
  echo <text>            - Print text

THEME & APPEARANCE:
  theme / settings       - Open theme settings GUI
  color <type> <hex>     - Change color (console_bg|console_fg|list_bg|list_fg)
  font <name>            - Change font (Courier, Arial, Consolas, etc.)
  fontsize <size>        - Change font size (8-24)
  theme_info             - Show current theme settings

CONSOLE:
  cls / clear            - Clear console
  help / commands        - Show this help
  exit                   - Exit application

TIPS:
  • Use arrow keys (Up/Down) to navigate command history
  • Double-click files to open them
  • Press Delete to remove selected file
  • Press Enter to execute commands
  • Try: cd A: to switch to temporary storage
  • Try: cd B: to switch to permanent storage
  • Try: color console_fg #FF5733 to change text color
  • Try: fontsize 12 to change font size
  • Try: font Consolas to change font family
"""
        self.log_message(commands + "\n")
    
    def show_commands(self):
        """Show the current command reference using ASCII-only formatting."""
        commands = """
PYTHON OS COMMAND REFERENCE
===========================

NAVIGATION
  cd <path>                 Change directory; A:, B:, and C: are supported
  pwd                       Print the current directory
  dir | ls                  List directory contents
  tree                      Display a directory tree
  drives                    List virtual drives
  driveinfo                 Show drive types and physical locations
  explorer [path|A:|B:]     Open a folder in the system File Explorer

FILES
  open <path>               Open a file or folder with its default application
  mkdir <name>              Create a directory
  del | rm <path>           Delete a file or directory
  copy | cp <src> <dst>     Copy a file
  move | mv <src> <dst>     Move a file
  type | cat <file>         Display a text file
  write | nano <file>       Write a file
  append <file>             Append to a file
  rename <old> <new>        Rename a file
  touch <file>              Create a file or update its timestamp
  info <file>               Show file metadata
  hash <file> [algorithm]   Calculate MD5, SHA1, SHA256, or SHA512
  grep <text> <file>        Search within a file
  search <name>             Search for files recursively
  head | tail <file> [N]    Show the first or last N lines
  archive <path>            Create a ZIP archive
  extract <file.zip>        Extract a ZIP archive

APPLICATIONS
  apps                      List pyOS desktop applications
  filemanager               Open the pyOS file manager
  games                     Open the games suite
  snake | sudoku | chess    Open an individual game
  messenger                 Open peer-to-peer Messenger
  calculator | calc         Open the graphing calculator
  images | imageviewer      Open the image viewer
  notepad | editor          Open a note or text editor
  desktop_media             Open the embedded media player
  ide                       Open the Python IDE
  desktop_browser           Open the embedded internet browser
  dispenser                 Open the dot matrix sausage dispenser
  pyos_settings             Open desktop settings
  play | media <file>       Play audio or video using VLC/default player
  browser [url]             Open the page source inspector
  browse <url>              Open a URL in the system browser
  inspect <url>             Print headers and an HTML source preview
  savepage <url> [file]     Download a complete page into the current folder
  deskgui                   Launch the pyOS desktop GUI
  gui_settings              Display saved pyOS GUI preferences

HOST SHELLS (WINDOWS)
  powershell | ps <command>  Run Windows PowerShell inside the pyOS console
  wsl <command>              Run a WSL command inside the pyOS console
  stop                       Cancel the active command

NETWORK AND SYSTEM
  ipconfig | ifconfig       Show network configuration
  ping <host>               Ping a host
  netstat                   Show network statistics
  download <url>            Download into the current directory
  sysinfo                   Show system information
  diskspace                 Show disk usage
  tasklist | ps             Show running processes
  whoami                    Show the current user
  date | time               Show the current date or time

CONSOLE
  history                   Show command history
  monochrome                Restore the black-and-white CLI theme
  theme | settings          Open CLI appearance settings
  fontsize <8-24>           Change console font size
  clear | cls               Clear console output
  help | commands           Show this reference
  exit | quit               Close the command center
"""
        self.log_message(commands + "\n")

    def show_about(self):
        """Show current Command Center capabilities."""
        about = f"""pyOS Command Center {self.VERSION}

+ Authenticated command and file workspace
+ Temporary and persistent virtual drives
+ Bounded background command queues
+ Tracked Windows PowerShell and WSL commands
+ Safe cancellation, lock, and coordinated shutdown
+ Network, system, archive, hashing, and search tools
+ Configurable console and file-list appearance
+ Enabled pyOS desktop application launchers

PowerShell and WSL integration is available on Windows. Host-shell commands run
with the permissions of the signed-in operating-system user.
"""
        messagebox.showinfo("About pyOS Command Center", about)

def main():
    try:
        relaunch_in_configured_environment(__file__)
    except ConfigurationError as error:
        failure_root = tk.Tk()
        failure_root.withdraw()
        messagebox.showerror(
            "pyOS Configuration Recovery Required",
            f"pyOS refused to use invalid configuration state:\n\n{error}",
            parent=failure_root,
        )
        failure_root.destroy()
        return
    root = tk.Tk()
    root.withdraw()
    try:
        config = load_config()
        recovered_update = recover_source_update(config["install_dir"], config["data_dir"])
    except (ConfigurationError, OSError, ValueError) as error:
        messagebox.showerror(
            "pyOS Update Recovery Required",
            f"pyOS could not safely recover an interrupted source update:\n\n{error}",
            parent=root,
        )
        root.destroy()
        return
    if recovered_update:
        messagebox.showinfo(
            "pyOS Update Recovered",
            "An interrupted source update was rolled back before Command Center continued.",
            parent=root,
        )
    try:
        username = authenticate(root, cancellable=False, allow_remembered=True)
    except (CredentialStoreError, ConfigurationError) as error:
        messagebox.showerror(
            "pyOS Account Recovery Required",
            f"pyOS detected invalid account state and failed closed:\n\n{error}",
            parent=root,
        )
        root.destroy()
        return
    if not username:
        # Authentication may deliberately fail closed after reporting corrupt
        # state.  Never turn that sentinel into an authenticated None session.
        root.destroy()
        return
    try:
        app = PythonOS(root)
    except (
        CredentialStoreError,
        ConfigurationError,
        JSONPersistenceError,
        StorageOwnershipError,
        OSError,
        ValueError,
        tk.TclError,
    ) as error:
        messagebox.showerror(
            "pyOS State Recovery Required",
            f"pyOS refused to start with invalid or inaccessible state:\n\n{error}",
            parent=root,
        )
        root.destroy()
        return
    app.authenticated = True
    app.authenticated_username = username
    app.user_var.set(f"User: {username}")
    root.deiconify()
    try:
        root.mainloop()
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
