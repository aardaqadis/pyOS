import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog, simpledialog
import os
import subprocess
import threading
from pathlib import Path
from datetime import datetime
import json
import socket
import shutil
import tempfile
import sys
import hashlib
import shlex
import webbrowser
import urllib.error
import urllib.parse
import urllib.request

from tkinter import colorchooser

from pyos_config import (
    get_cli_settings_path,
    get_downloads_dir,
    get_drive_b_dir,
    get_gui_settings_path,
    get_profile_dir,
    relaunch_in_configured_environment,
)
from pyos_auth import authenticate, change_credentials_dialog, has_account

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
        self.settings_file = str(get_cli_settings_path())
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
        """Load settings from file"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    loaded = json.load(f)
                    if loaded.get("style_version") != self.defaults["style_version"]:
                        return self.defaults.copy()
                    return {**self.defaults, **loaded}
            except:
                return self.defaults.copy()
        return self.defaults.copy()
    
    def save_settings(self):
        """Save settings to file"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self.settings, f, indent=2)
        except Exception as e:
            print(f"Could not save settings: {e}")
    
    def reset_to_defaults(self):
        """Reset all settings to defaults"""
        self.settings = self.defaults.copy()
        self.save_settings()

class VirtualDrive:
    def __init__(self, name, is_temporary=False, custom_path=None):
        self.name = name
        self.is_temporary = is_temporary
        
        if custom_path is not None:
            self.path = str(Path(custom_path))
        elif is_temporary:
            self.path = os.path.join(tempfile.gettempdir(), f"pyOS_Drive_{name}")
        else:
            self.path = os.path.join(os.path.expanduser("~"), f".pyOS_Drive_{name}")
        
        os.makedirs(self.path, exist_ok=True)
    
    def get_path(self):
        return self.path
    
    def get_usage(self):
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.path):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                total += os.path.getsize(filepath)
        return total

class PythonOS:
    def __init__(self, root):
        self.root = root
        self.root.title("Python OS - Monochrome Command Center")
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
        
        # Virtual Drives
        self.drive_a = VirtualDrive("A", custom_path=get_profile_dir() / "Drive_A")
        self.drive_b = VirtualDrive("B", is_temporary=False, custom_path=get_drive_b_dir())  # Permanent storage
        self.drives = {
            "C": str(Path.home()),
            "A": self.drive_a.get_path(),
            "B": self.drive_b.get_path()
        }
        
        self.current_drive = "C"
        
        self.setup_ui()
    
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
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Open Directory", command=self.open_directory)
        file_menu.add_command(label="Clear Console", command=self.clear_console)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.quit)
        
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
            ("Dispenser", "dispenser"),
        ):
            apps_menu.add_command(
                label=label,
                command=lambda name=app_name: self.open_desktop_app(name),
            )
        apps_menu.add_command(label="Browser Inspector", command=self.open_browser_inspector)
        apps_menu.add_command(label="Open Current Folder", command=lambda: self._open_explorer(self.current_directory))
        
        network_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Network", menu=network_menu)
        network_menu.add_command(label="Network Status", command=self.show_network_status)
        network_menu.add_command(label="IP Configuration", command=self.show_ipconfig)
        
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
        
        self.refresh_files()
        self.log_message("Type 'help' for commands\n")
        self.log_message("pyOS - v1.0.1a\n\n")
        self.command_input.focus()
    
    def update_time(self):
        """Update time display"""
        self.time_var.set(datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self.update_time)
    
    def open_theme_settings(self):
        """Open theme settings dialog"""
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
        self.console.config(bg=self.theme.settings["console_bg"], 
                           fg=self.theme.settings["console_fg"],
                           font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]))
        self.file_listbox.config(bg=self.theme.settings["list_bg"], 
                                fg=self.theme.settings["list_fg"],
                                font=(self.theme.settings["console_font"], self.theme.settings["console_fontsize"]))
        self.log_message("Theme updated!\n")
    
    def save_theme_settings(self):
        """Save theme settings"""
        self.theme.save_settings()
        self.log_message("Settings saved!\n")
    
    def reset_theme(self):
        """Reset theme to defaults"""
        self.theme.reset_to_defaults()
        self.apply_theme_changes()
        self.log_message("Theme reset to defaults!\n")

    def ensure_authenticated(self):
        """Authenticate once for the current CLI session."""
        if self.authenticated:
            return True
        username = authenticate(self.root, cancellable=True)
        if not username:
            return False
        self.authenticated = True
        self.authenticated_username = username
        self.user_var.set(f"User: {username}")
        self.log_message(f"Authenticated as {username}.\n")
        return True

    def lock_cli(self):
        """Require authentication again before the next command."""
        self.authenticated = False
        self.authenticated_username = None
        self.user_var.set("User: Locked")
        self.log_message("CLI locked. The next command requires authentication.\n")

    def change_cli_account(self):
        """Create or change the persistent pyOS account."""
        if has_account() and not self.ensure_authenticated():
            return
        username = change_credentials_dialog(self.root)
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
        if drive in self.drives:
            self.current_drive = drive
            self.current_directory = self.drives[drive]
            self.drive_var.set(f"Drive: {drive}:")
            self.path_var.set(self.format_display_path(self.current_directory))
            self.refresh_files()
            self.log_message(f"Switched to Drive {drive}:\n")
        else:
            self.log_message(f"Drive {drive}: not found\n")
    
    def show_drive_info(self):
        """Show drive information"""
        info = "\n=== DRIVE INFORMATION ===\n\n"
        
        for drive_letter, drive_path in self.drives.items():
            try:
                if os.path.exists(drive_path):
                    if drive_letter == "A":
                        size = self.drive_a.get_usage()
                        drive_type = "Temporary (RAM)"
                        info += f"Drive {drive_letter}: (Temp)\n"
                        info += f"  Location: {drive_path}\n"
                        info += f"  Used: {size / (1024*1024):.2f} MB\n"
                    elif drive_letter == "B":
                        size = self.drive_b.get_usage()
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
                        subprocess.Popen(["open", full_path])
                    else:
                        subprocess.Popen(["xdg-open", full_path])
                    self.log_message(f"Opening: {full_path}\n")
                except Exception as e:
                    self.log_message(f"Error opening file: {e}\n")
        
        self.path_var.set(self.format_display_path(self.current_directory))
        self.refresh_files()
    
    def delete_file(self):
        """Delete selected file or folder"""
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
        directory = filedialog.askdirectory()
        if directory:
            self.current_directory = directory
            self.path_var.set(self.format_display_path(self.current_directory))
            self.refresh_files()
    
    def execute_command(self):
        """Execute command from input"""
        command = self.input_var.get().strip()
        if not command:
            return
        if not self.ensure_authenticated():
            return
        
        self.log_message(f"{self.format_display_path(self.current_directory)}> {command}\n")
        self.command_history.append(command)
        self.history_index = len(self.command_history)
        self.input_var.set("")
        
        # Run command in thread to prevent GUI freeze
        thread = threading.Thread(target=self._run_command, args=(command,))
        thread.daemon = True
        thread.start()
    
    def _run_command(self, command):
        """Execute command and display output"""
        try:
            try:
                parsed = shlex.split(command, posix=os.name != "nt")
            except ValueError as error:
                self.log_message(f"Command parsing error: {error}\n")
                return
            command_name = parsed[0].lower() if parsed else ""
            command_args = parsed[1:]

            # Built-in commands
            if command.lower().startswith("cd "):
                path = command[3:].strip().strip('"')
                if path == "..":
                    self.current_directory = os.path.dirname(self.current_directory)
                elif path.upper().startswith("A:") or path.upper().startswith("B:") or path.upper().startswith("C:"):
                    drive = path[0].upper()
                    if drive in self.drives:
                        self.switch_drive(drive)
                        if len(path) > 2:
                            subpath = path[2:].lstrip("\\")
                            if subpath:
                                full_path = os.path.join(self.drives[drive], subpath)
                                if os.path.isdir(full_path):
                                    self.current_directory = full_path
                                    self.path_var.set(self.format_display_path(self.current_directory))
                    return
                elif os.path.isdir(path):
                    self.current_directory = os.path.abspath(path)
                elif os.path.isdir(os.path.join(self.current_directory, path)):
                    self.current_directory = os.path.abspath(os.path.join(self.current_directory, path))
                else:
                    self.log_message(f"The path does not exist: {path}\n")
                
                self.path_var.set(self.format_display_path(self.current_directory))
                self.root.after(100, self.refresh_files)
            
            elif command.lower() == "drives":
                output = f"Available Drives:\n"
                output += f"C: - User Home Directory\n"
                output += f"A: - Temporary Storage (RAM)\n"
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
                target = " ".join(command_args) if command_args else self.current_directory
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
                "dispenser",
            }:
                aliases = {
                    "calc": "calculator", "imageviewer": "images", "filemanager": "files",
                    "desktop_browser": "browser", "desktop_media": "media",
                    "pyos_settings": "settings",
                }
                if command_name == "apps":
                    self.log_message(
                        "Desktop apps: filemanager, games, snake, sudoku, chess, messenger, "
                        "calculator, images, notepad, editor, desktop_media, ide, "
                        "desktop_browser, dispenser, pyos_settings\n"
                    )
                else:
                    app_name = aliases.get(command_name, command_name)
                    self.root.after(0, lambda name=app_name: self.open_desktop_app(name))

            elif command_name == "browser":
                url = " ".join(command_args) if command_args else "https://www.google.com"
                self.root.after(0, lambda value=url: self.open_browser_inspector(value))
                self.log_message(f"Opened browser inspector: {url}\n")

            elif command_name == "browse":
                if not command_args:
                    self.log_message("Usage: browse <url>\n")
                else:
                    url = " ".join(command_args)
                    if "://" not in url:
                        url = "https://" + url
                    webbrowser.open(url)
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

            elif command_name == "whoami":
                self.log_message(f"{os.getenv('USERNAME') or os.getenv('USER') or 'unknown'}\n")

            elif command_name in {"gui_settings", "desktop_settings"}:
                self._show_gui_settings()

            elif command_name == "monochrome":
                self.theme.settings = self.theme.defaults.copy()
                self.theme.save_settings()
                self.root.after(0, self.apply_theme_changes)
                self.log_message("Monochrome theme restored.\n")

            elif command_name in {"exit", "quit"}:
                self.root.after(0, self.root.quit)
            
            elif command.lower() == "cls" or command.lower() == "clear":
                self.clear_console()
            
            elif command.lower() == "dir" or command.lower() == "ls":
                self._list_directory()
            
            elif command.lower() == "tree":
                self._show_tree()
            
            elif command.lower().startswith("mkdir "):
                dirname = command[6:].strip().strip('"')
                path = os.path.join(self.current_directory, dirname)
                os.makedirs(path, exist_ok=True)
                self.log_message(f"Directory created: {dirname}\n")
                self.root.after(100, self.refresh_files)
            
            elif command.lower().startswith("del ") or command.lower().startswith("rm "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                if os.path.exists(path):
                    if os.path.isdir(path):
                        shutil.rmtree(path)
                    else:
                        os.remove(path)
                    self.log_message(f"Deleted: {filename}\n")
                    self.root.after(100, self.refresh_files)
                else:
                    self.log_message(f"File not found: {filename}\n")
            
            elif command.lower().startswith("copy ") or command.lower().startswith("cp "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    src = parts[1].strip('"')
                    dst = parts[2].strip('"')
                    src_path = os.path.join(self.current_directory, src)
                    dst_path = os.path.join(self.current_directory, dst)
                    shutil.copy2(src_path, dst_path)
                    self.log_message(f"Copied {src} to {dst}\n")
                    self.root.after(100, self.refresh_files)
            
            elif command.lower().startswith("move ") or command.lower().startswith("mv "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    src = parts[1].strip('"')
                    dst = parts[2].strip('"')
                    src_path = os.path.join(self.current_directory, src)
                    dst_path = os.path.join(self.current_directory, dst)
                    shutil.move(src_path, dst_path)
                    self.log_message(f"Moved {src} to {dst}\n")
                    self.root.after(100, self.refresh_files)
            
            elif command.lower() == "pwd":
                self.log_message(f"{self.current_directory}\n")
            
            elif command.lower().startswith("echo "):
                text = command[5:].strip()
                self.log_message(f"{text}\n")
            
            elif command.lower().startswith("type ") or command.lower().startswith("cat "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                if os.path.exists(path) and os.path.isfile(path):
                    with open(path, 'r', errors='ignore') as f:
                        self.log_message(f.read() + "\n")
                else:
                    self.log_message(f"File not found: {filename}\n")
            
            elif command.lower().startswith("write ") or command.lower().startswith("nano "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._open_write_dialog(filename, path)
             
            elif command.lower().startswith("append "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._open_append_dialog(filename, path)
            
            elif command.lower().startswith("rename "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    old_name = parts[1].strip('"')
                    new_name = parts[2].strip('"')
                    old_path = os.path.join(self.current_directory, old_name)
                    new_path = os.path.join(self.current_directory, new_name)
                    try:
                        os.rename(old_path, new_path)
                        self.log_message(f"Renamed: {old_name} → {new_name}\n")
                        self.root.after(100, self.refresh_files)
                    except Exception as e:
                        self.log_message(f"Error renaming file: {e}\n")
                else:
                    self.log_message("Usage: rename <oldname> <newname>\n")
            
            elif command.lower().startswith("info "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._show_file_info(path)
            
            elif command.lower().startswith("lines ") or command.lower().startswith("wc "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._count_lines(path)
            
            elif command.lower().startswith("grep ") or command.lower().startswith("search_text "):
                parts = command.split(maxsplit=2)
                if len(parts) >= 3:
                    pattern = parts[1].strip('"')
                    filename = parts[2].strip('"')
                    path = os.path.join(self.current_directory, filename)
                    self._search_text_in_file(pattern, path)
                else:
                    self.log_message("Usage: grep <pattern> <filename>\n")
            
            elif command.lower().startswith("touch "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                try:
                    if not os.path.exists(path):
                        open(path, 'a').close()
                        self.log_message(f"File created: {filename}\n")
                    else:
                        os.utime(path, None)
                        self.log_message(f"File touched (timestamp updated): {filename}\n")
                    self.root.after(100, self.refresh_files)
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
                path = os.path.join(self.current_directory, filename)
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
                path = os.path.join(self.current_directory, filename)
                self._show_tail(path, lines)
            
            elif command.lower().startswith("archive "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._create_zip_archive(path)
            
            elif command.lower().startswith("extract "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
                self._extract_zip(path)
            
            elif command.lower() == "files_only":
                self._list_files_only()
            
            elif command.lower() == "dirs_only":
                self._list_dirs_only()
            
            elif command.lower().startswith("hexdump ") or command.lower().startswith("xxd "):
                filename = command.split(maxsplit=1)[1].strip().strip('"')
                path = os.path.join(self.current_directory, filename)
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
                        self.theme.settings["console_bg"] = color_value
                    elif color_type == "console_fg":
                        self.theme.settings["console_fg"] = color_value
                    elif color_type == "list_bg":
                        self.theme.settings["list_bg"] = color_value
                    elif color_type == "list_fg":
                        self.theme.settings["list_fg"] = color_value
                    else:
                        self.log_message("Usage: color [console_bg|console_fg|list_bg|list_fg] #HEXCOLOR\n")
                        return
                    self.apply_theme_changes()
                    self.log_message(f"Color {color_type} changed to {color_value}\n")
                else:
                    self.log_message("Usage: color [console_bg|console_fg|list_bg|list_fg] #HEXCOLOR\n")
            
            elif command.lower().startswith("font "):
                font_name = command[5:].strip()
                self.theme.settings["console_font"] = font_name
                self.apply_theme_changes()
                self.log_message(f"Font changed to {font_name}\n")
            
            elif command.lower().startswith("fontsize "):
                try:
                    size = int(command.split(maxsplit=1)[1])
                    if 8 <= size <= 24:
                        self.theme.settings["console_fontsize"] = size
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
                try:
                    result = subprocess.run(
                        command,
                        shell=True,
                        cwd=self.current_directory,
                        capture_output=True,
                        text=True,
                        timeout=15
                    )
                    if result.stdout:
                        self.log_message(result.stdout)
                    if result.stderr:
                        self.log_message(result.stderr)
                    if not result.stdout and not result.stderr:
                        self.log_message("Command executed successfully.\n")
                except subprocess.TimeoutExpired:
                    self.log_message("Command timed out after 15 seconds.\n")
                except Exception as e:
                    self.log_message(f"Error: {str(e)}\n")
        
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
        return os.path.normpath(os.path.join(self.current_directory, value))

    def _open_path(self, value):
        path = self._resolve_path(value)
        if not os.path.exists(path):
            self.log_message(f"Path not found: {value}\n")
            return
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
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
                subprocess.Popen(["explorer", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            self.log_message(f"Opened File Explorer: {path}\n")
        except OSError as error:
            self.log_message(f"Could not open File Explorer: {error}\n")

    def _play_media(self, value):
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
                subprocess.Popen([player, path])
            elif os.name == "nt":
                os.startfile(path)
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
            if isinstance(loaded, dict):
                defaults.update({key: loaded[key] for key in defaults if key in loaded})
        except (OSError, ValueError, TypeError):
            pass
        self.log_message("\nDESKTOP GUI SETTINGS\n")
        self.log_message("-" * 32 + "\n")
        for key, value in defaults.items():
            self.log_message(f"{key:<24} {value}\n")
        self.log_message(f"\nFile: {self.gui_settings_file}\n\n")

    def _ping(self, host):
        """Ping a host"""
        try:
            self.log_message(f"Pinging {host}...\n")
            result = subprocess.run(
                f"ping -n 4 {host}" if os.name == 'nt' else f"ping -c 4 {host}",
                shell=True,
                capture_output=True,
                text=True,
                timeout=10
            )
            self.log_message(result.stdout)
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

    def _fetch_page(self, url):
        """Fetch a web page and return its final URL, headers, bytes, and decoded source."""
        url = self._normalize_url(url)
        request = urllib.request.Request(url, headers={"User-Agent": "pyOS Browser Inspector/1.0"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(10 * 1024 * 1024 + 1)
            if len(data) > 10 * 1024 * 1024:
                raise ValueError("Page exceeds the 10 MB inspection limit")
            encoding = response.headers.get_content_charset() or "utf-8"
            source = data.decode(encoding, errors="replace")
            headers = dict(response.headers.items())
            return response.geturl(), response.status, headers, data, source

    def open_browser_inspector(self, initial_url="https://www.google.com"):
        """Open a monochrome browser source inspector and page downloader."""
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

        def replace_text(widget, text):
            widget.configure(state=tk.NORMAL)
            widget.delete("1.0", tk.END)
            widget.insert("1.0", text)
            widget.configure(state=tk.DISABLED)

        def display_result(result):
            final_url, status, headers, data, source = result
            cache.update(url=final_url, data=data)
            url_var.set(final_url)
            replace_text(source_view, source)
            metadata = [f"URL: {final_url}", f"Status: {status}", f"Size: {len(data):,} bytes", ""]
            metadata.extend(f"{name}: {value}" for name, value in headers.items())
            replace_text(headers_view, "\n".join(metadata))
            status_var.set(f"Loaded {len(data):,} bytes | HTTP {status}")

        def fetch():
            status_var.set("Loading...")
            source_view.configure(state=tk.NORMAL)
            source_view.delete("1.0", tk.END)
            source_view.insert("1.0", "Loading...")
            source_view.configure(state=tk.DISABLED)
            requested_url = url_var.get()

            def worker():
                try:
                    result = self._fetch_page(requested_url)
                    self.root.after(0, lambda: display_result(result))
                except (urllib.error.URLError, OSError, ValueError) as error:
                    message = str(error)
                    self.root.after(0, lambda: status_var.set(f"Load failed: {message}"))

            threading.Thread(target=worker, daemon=True).start()

        def save_cached_page():
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
                Path(destination).write_bytes(cache["data"])
                status_var.set(f"Saved: {destination}")
                self.refresh_files()
            except OSError as error:
                status_var.set(f"Save failed: {error}")

        ttk.Button(toolbar, text="Inspect", command=fetch).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Save Page", command=save_cached_page).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="Open External", command=lambda: webbrowser.open(url_var.get())).pack(side=tk.LEFT, padx=3)
        url_entry.bind("<Return>", lambda event: fetch())
        fetch()

    def _inspect_page(self, url):
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
        try:
            final_url, status, headers, data, source = self._fetch_page(url)
            if not filename:
                filename = Path(urllib.parse.urlparse(final_url).path).name or "index.html"
            destination = self._resolve_path(filename)
            Path(destination).write_bytes(data)
            self.log_message(f"Saved page: {destination} ({len(data):,} bytes, HTTP {status})\n")
            self.root.after(100, self.refresh_files)
        except (urllib.error.URLError, OSError, ValueError) as error:
            self.log_message(f"Page download failed: {error}\n")

    def _download(self, url):
        """Download file from URL"""
        try:
            import urllib.request
            filename = url.split('/')[-1]
            filepath = str(get_downloads_dir() / filename)
            
            self.log_message(f"Downloading {url}...\n")
            urllib.request.urlretrieve(url, filepath)
            self.log_message(f"Downloaded to: {filepath}\n")
            self.root.after(100, self.refresh_files)
        except Exception as e:
            self.log_message(f"Download failed: {e}\n")
    
    def _show_diskspace(self):
        """Show disk space usage"""
        info = "\n=== DISK SPACE ===\n\n"
        
        try:
            total, used, free = shutil.disk_usage(self.current_directory)
            info += f"Drive: {self.current_drive}:\n"
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
            
            for root, dirs, files in os.walk(self.current_directory):
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
     
    def _open_write_dialog(self, filename, path):
        """Open write dialog on main thread (fixes threading issue)"""
        def show_dialog():
            content = tk.simpledialog.askstring("Write File", f"Enter content for {filename}:")
            if content is not None:
                try:
                    with open(path, 'w') as f:
                        f.write(content)
                    self.log_message(f"File created: {filename}\n")
                    self.root.after(100, self.refresh_files)
                except Exception as e:
                    self.log_message(f"Error writing file: {e}\n")
        self.root.after(0, show_dialog)
     
    def _open_append_dialog(self, filename, path):
        """Open append dialog on main thread (fixes threading issue)"""
        def show_dialog():
            content = tk.simpledialog.askstring("Append to File", f"Enter content to append to {filename}:")
            if content is not None:
                try:
                    with open(path, 'a') as f:
                        f.write(content + "\n")
                    self.log_message(f"Content appended to: {filename}\n")
                    self.root.after(100, self.refresh_files)
                except Exception as e:
                    self.log_message(f"Error appending to file: {e}\n")
        self.root.after(0, show_dialog)
     
    def _count_lines(self, filepath):
        """Count lines in a file"""
        try:
            if not os.path.exists(filepath) or not os.path.isfile(filepath):
                self.log_message(f"File not found: {filepath}\n")
                return
            
            with open(filepath, 'r', errors='ignore') as f:
                lines = len(f.readlines())
                words = sum(len(line.split()) for line in open(filepath, 'r', errors='ignore'))
                chars = sum(len(line) for line in open(filepath, 'r', errors='ignore'))
            
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
                lines = f.readlines()
            
            start = max(0, len(lines) - num_lines)
            for line in lines[start:]:
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
            archive_path = os.path.join(self.current_directory, archive_name)
            
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
            self.root.after(100, self.refresh_files)
        except Exception as e:
            self.log_message(f"Error creating archive: {e}\n")
    
    def _extract_zip(self, filepath):
        """Extract zip archive"""
        try:
            import zipfile
            
            if not os.path.exists(filepath) or not filepath.lower().endswith('.zip'):
                self.log_message(f"Invalid zip file: {filepath}\n")
                return
            
            extract_dir = os.path.join(self.current_directory, os.path.basename(filepath)[:-4])
            os.makedirs(extract_dir, exist_ok=True)
            
            self.log_message(f"Extracting: {os.path.basename(filepath)}...\n")
            
            with zipfile.ZipFile(filepath, 'r') as zipf:
                zipf.extractall(extract_dir)
            
            self.log_message(f"Extracted to: {os.path.basename(extract_dir)}\n")
            self.root.after(100, self.refresh_files)
        except Exception as e:
            self.log_message(f"Error extracting archive: {e}\n")
    
    def _list_files_only(self):
        """List only files in current directory"""
        try:
            self.log_message("\n=== FILES ONLY ===\n\n")
            files = [f for f in os.listdir(self.current_directory) if os.path.isfile(os.path.join(self.current_directory, f))]
            
            if not files:
                self.log_message("No files found\n")
                return
            
            for f in sorted(files):
                path = os.path.join(self.current_directory, f)
                size = os.path.getsize(path)
                self.log_message(f"  {f} ({size:,} bytes)\n")
            
            self.log_message(f"\nTotal: {len(files)} file(s)\n\n")
        except Exception as e:
            self.log_message(f"Error: {e}\n")
    
    def _list_dirs_only(self):
        """List only directories in current directory"""
        try:
            self.log_message("\n=== DIRECTORIES ONLY ===\n\n")
            dirs = [d for d in os.listdir(self.current_directory) if os.path.isdir(os.path.join(self.current_directory, d))]
            
            if not dirs:
                self.log_message("No directories found\n")
                return
            
            for d in sorted(dirs):
                path = os.path.join(self.current_directory, d)
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
            self._tree_walk(self.current_directory, "")
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
                is_last = (i == len(dirs) - 1)
                self.log_message(f"{prefix}{'└── ' if is_last else '├── '}{d}/\n")
                new_prefix = prefix + ("    " if is_last else "│   ")
                self._tree_walk(os.path.join(path, d), new_prefix, max_depth, current_depth + 1)
        except PermissionError:
            pass
    
    def _list_directory(self):
        """List files and directories"""
        try:
            items = sorted(os.listdir(self.current_directory))
            output = "\n Directory listing:\n\n"
            
            for item in items:
                full_path = os.path.join(self.current_directory, item)
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
        self.console.config(state='normal')
        self.console.insert(tk.END, message)
        self.console.see(tk.END)
        self.console.config(state='disabled')
    
    def clear_console(self):
        """Clear console output"""
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
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gui_path = os.path.join(script_dir, "pyOSgui.py")
        
        try:
            subprocess.Popen([sys.executable, gui_path])
            self.log_message("Desktop GUI opening in new window...\n")
        except Exception as e:
            self.log_message(f"Error opening desktop GUI: {e}\n")

    def open_desktop_app(self, app_name):
        """Open one pyOS desktop application directly from the command center."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        gui_path = os.path.join(script_dir, "pyOSgui.py")
        try:
            subprocess.Popen([sys.executable, gui_path, "--app", app_name])
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
  A: - Temporary Storage (RAM-like, cleared on exit)
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
        """Show about information"""
        about = """Python OS v3.0
Advanced Terminal with Virtual Drives, Customization & File Operations

Features:
• GUI-based terminal with file browser
• Virtual drives (A: temp, B: permanent, C: home)
• Network diagnostics and monitoring
• System information and process management
• 📄 ADVANCED FILE OPERATIONS (20+ commands):
  - Create, edit, append, rename files
  - Search text in files, count lines
  - Show head/tail/hexdump of files
  - Archive/extract zip files
  - Filter files and directories
• 🎨 CUSTOMIZABLE COLORS - Change console/list colors
• 🔤 CUSTOMIZABLE FONTS - Choose your font family
• 📏 CUSTOMIZABLE FONT SIZE - Adjust text size (8-24)
• ⚙️ PERSISTENT SETTINGS - Preferences saved
• Command history and autocomplete
• Cross-platform support

Built with Python & Tkinter
"""
        messagebox.showinfo("About Python OS", about)


if __name__ == "__main__":
    relaunch_in_configured_environment(__file__)
    root = tk.Tk()
    username = authenticate(root, cancellable=False, allow_remembered=True)
    app = PythonOS(root)
    app.authenticated = True
    app.authenticated_username = username
    app.user_var.set(f"User: {username}")
    root.mainloop()
