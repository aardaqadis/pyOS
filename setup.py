"""pyOS setup wizard and unattended installer."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import venv
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from pyos_config import CONFIG_FILE, load_config, save_config


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
    "paramiko>=4.0,<5.0",
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
        self.enabled_apps = list(enabled_apps or (app_id for app_id, _label in OPTIONAL_APPS))
        self.dry_run = dry_run
        self.log = logger
        self.warnings = []

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
        for path in (self.install_dir, self.data_dir, self.downloads_dir):
            if path.exists() and not path.is_dir():
                raise ValueError(f"A file already exists at directory location: {path}")

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
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            (self.data_dir / "Drive_B").mkdir(parents=True, exist_ok=True)

    def copy_application(self):
        self.log("Copying pyOS application files")
        for name in APPLICATION_FILES:
            source = SOURCE_DIR / name
            if not source.exists():
                continue
            destination = self.install_dir / name
            self.log(f"  {name}")
            if not self.dry_run and source.resolve() != destination.resolve():
                shutil.copy2(source, destination)

    def create_environment(self):
        self.log(f"Creating isolated Python environment: {self.venv_dir}")
        if not self.dry_run:
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
        gui_command = f'@echo off\r\n"{self.python_executable}" "{self.install_dir / "pyOSgui.py"}"\r\n'
        cli_command = f'@echo off\r\n"{self.python_executable}" "{self.install_dir / "pyOScli.py"}"\r\n'
        if not self.dry_run:
            gui_launcher.write_text(gui_command, encoding="utf-8")
            cli_launcher.write_text(cli_command, encoding="utf-8")
        if self.create_shortcuts and os.name == "nt":
            self.create_desktop_shortcuts(gui_launcher, cli_launcher)

    def create_desktop_shortcuts(self, gui_launcher, cli_launcher):
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
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
        config = {
            "configured": True,
            "install_dir": str(self.install_dir),
            "data_dir": str(self.data_dir),
            "downloads_dir": str(self.downloads_dir),
            "drive_b_dir": str(self.data_dir / "Drive_B"),
            "python_executable": str(self.python_executable),
            "installed_at": datetime.now().isoformat(timespec="seconds"),
            "installer_version": 1,
            "enabled_apps": self.enabled_apps,
        }
        self.log(f"Writing shared configuration: {CONFIG_FILE}")
        if not self.dry_run:
            save_config(config)
            (self.install_dir / "install_manifest.json").write_text(
                json.dumps({
                    **config,
                    "packages": PYTHON_PACKAGES,
                    "optional_packages": OPTIONAL_PYTHON_PACKAGES,
                }, indent=2), encoding="utf-8"
            )

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
        home = Path.home().resolve()
        if (not install_dir.is_dir() or install_dir in {Path(install_dir.anchor), home} or
                home.is_relative_to(install_dir)):
            messagebox.showerror("Uninstall pyOS", "The configured installation directory is unsafe to remove.")
            return
        if not messagebox.askyesno(
            "Uninstall pyOS",
            f"Remove pyOS and its installed libraries from:\n{install_dir}\n\n"
            "Your account, settings, custom apps, Drive B, and downloads will be preserved?",
            icon=messagebox.WARNING,
        ):
            return
        desktop = Path(os.environ.get("USERPROFILE", home)) / "Desktop"
        for shortcut in (desktop / "pyOS GUI.lnk", desktop / "pyOS CLI.lnk"):
            try:
                shortcut.unlink()
            except OSError:
                pass
        try:
            CONFIG_FILE.unlink(missing_ok=True)
            if SOURCE_DIR.resolve().is_relative_to(install_dir):
                command = (
                    f"Start-Sleep -Seconds 2; Remove-Item -LiteralPath '{str(install_dir).replace(chr(39), chr(39)*2)}' "
                    "-Recurse -Force"
                )
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.root.destroy()
            else:
                shutil.rmtree(install_dir)
                self.existing_config = None
                messagebox.showinfo("Uninstall pyOS", "pyOS was uninstalled. User data was preserved.")
                self.root.destroy()
        except OSError as error:
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
