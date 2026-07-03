
import tkinter as tk
from tkinter import colorchooser, filedialog, messagebox, scrolledtext, ttk
import subprocess
import os
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
import threading
import sys
import calendar
import math
from pathlib import Path

from pyos_config import get_downloads_dir, get_drive_b_dir, get_gui_settings_path

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".flac", ".m4a", ".mid", ".midi", ".mp3", ".ogg", ".opus", ".wav", ".wma",
}
VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".ts", ".webm", ".wmv",
}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
IMAGE_EXTENSIONS = {
    ".apng", ".bmp", ".gif", ".ico", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp",
}

class DesktopIcon:
    """A text-only desktop launcher styled like an early desktop OS."""
    GRID_MARGIN = 10
    GRID_X = 110
    GRID_Y = 74

    def __init__(self, parent, name, icon_type, command, x, y, width=100, height=64):
        self.parent = parent
        self.name = name
        self.icon_type = icon_type
        self.command = command
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self._drag_start = None
        self._drag_origin = None
        self._dragged = False
        self.frame = tk.Frame(parent, bg="white", relief=tk.RAISED, bd=2)
        self.frame.place(x=x, y=y, width=width, height=height)

        self.name_label = tk.Label(
            self.frame,
            text=name.upper(),
            font=("Courier New", 9, "bold"),
            bg="white",
            fg="black",
            wraplength=88,
            justify=tk.CENTER,
            relief=tk.FLAT,
            cursor="hand2",
        )
        self.name_label.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)

        for widget in (self.name_label, self.frame):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.finish_drag)

    def start_drag(self, event):
        """Record pointer and launcher positions before a possible drag."""
        self._drag_start = (event.x_root, event.y_root)
        self._drag_origin = (self.x, self.y)
        self._dragged = False
        self.frame.lift()

    def drag(self, event):
        """Move the launcher while keeping it within the desktop area."""
        if self._drag_start is None or self._drag_origin is None:
            return
        delta_x = event.x_root - self._drag_start[0]
        delta_y = event.y_root - self._drag_start[1]
        if abs(delta_x) > 4 or abs(delta_y) > 4:
            self._dragged = True

        parent_width = max(self.width, self.parent.winfo_width())
        parent_height = max(self.height, self.parent.winfo_height())
        new_x = max(0, min(self._drag_origin[0] + delta_x, parent_width - self.width))
        new_y = max(0, min(self._drag_origin[1] + delta_y, parent_height - self.height))
        self.x, self.y = new_x, new_y
        self.frame.place_configure(x=new_x, y=new_y)

    def finish_drag(self, event):
        """Open the launcher on a click, or leave it at its new drag position."""
        should_open = not self._dragged
        self._drag_start = None
        self._drag_origin = None
        if should_open:
            self.double_click()
        else:
            self.snap_to_grid()

    def snap_to_grid(self):
        """Snap the launcher to the nearest classic desktop grid cell."""
        parent_width = max(self.width, self.parent.winfo_width())
        parent_height = max(self.height, self.parent.winfo_height())
        max_x = max(0, parent_width - self.width)
        max_y = max(0, parent_height - self.height)

        snapped_x = self.GRID_MARGIN + round((self.x - self.GRID_MARGIN) / self.GRID_X) * self.GRID_X
        snapped_y = self.GRID_MARGIN + round((self.y - self.GRID_MARGIN) / self.GRID_Y) * self.GRID_Y
        self.x = max(0, min(snapped_x, max_x))
        self.y = max(0, min(snapped_y, max_y))
        self.frame.place_configure(x=self.x, y=self.y)
    
    def double_click(self):
        """Execute command on double-click"""
        if callable(self.command):
            self.command()
        else:
            subprocess.Popen(self.command, shell=True)


class TaskbarItem:
    """Compact taskbar launcher with a drawn icon and label underneath."""
    def __init__(self, parent, label, icon_type, command, colors, remove_command=None):
        self.command = command
        self.remove_command = remove_command
        self.frame = tk.Frame(parent, bg=colors["bg"], width=68, height=58, cursor="hand2")
        self.frame.pack(side=tk.LEFT, padx=2, pady=1)
        self.frame.pack_propagate(False)
        self.icon = tk.Canvas(
            self.frame, width=28, height=30, bg=colors["bg"], highlightthickness=0, cursor="hand2"
        )
        self.icon.pack()
        self.label = tk.Label(
            self.frame,
            text=label[:9],
            bg=colors["bg"],
            fg=colors["fg"],
            font=("Courier New", 7),
            anchor=tk.CENTER,
        )
        self.label._keep_font = True
        self.label.pack(fill=tk.X)
        self.icon_type = icon_type
        self.set_colors(colors["bg"], colors["fg"])
        for widget in (self.frame, self.icon, self.label):
            widget.bind("<Button-1>", lambda event: self.command())
            if remove_command:
                widget.bind("<Button-3>", self.show_remove_menu)

    def set_colors(self, background, foreground):
        self.frame.configure(bg=background)
        self.icon.configure(bg=background)
        self.label.configure(bg=background, fg=foreground)
        self.icon.delete("all")
        if self.icon_type == "folder":
            self.icon.create_polygon(3, 10, 12, 10, 15, 6, 25, 6, 25, 25, 3, 25,
                                     fill=background, outline=foreground, width=2)
        elif self.icon_type == "add":
            self.icon.create_rectangle(3, 3, 25, 25, outline=foreground, width=2)
            self.icon.create_line(14, 7, 14, 21, fill=foreground, width=2)
            self.icon.create_line(7, 14, 21, 14, fill=foreground, width=2)
        else:
            self.icon.create_rectangle(6, 3, 22, 26, fill=background, outline=foreground, width=2)
            self.icon.create_line(10, 10, 19, 10, fill=foreground)
            self.icon.create_line(10, 15, 19, 15, fill=foreground)

    def show_remove_menu(self, event):
        menu = tk.Menu(self.frame, tearoff=0)
        menu.add_command(label="Remove Shortcut", command=self.remove_command)
        menu.post(event.x_root, event.y_root)

    def destroy(self):
        self.frame.destroy()


class Taskbar:
    """Taskbar at the bottom"""
    def __init__(self, parent, desktop, clock_24h=True, show_seconds=True):
        self.parent = parent
        self.desktop = desktop
        self.clock_24h = clock_24h
        self.show_seconds = show_seconds
        self.items = []
        self.taskbar = tk.Frame(parent, bg="black", height=64, relief=tk.RAISED, bd=2)
        self.taskbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.taskbar.pack_propagate(False)

        self.shortcuts_frame = tk.Frame(self.taskbar, bg="black")
        self.shortcuts_frame.pack(side=tk.LEFT, fill=tk.Y)
        for shortcut in desktop.preferences.get("taskbar_shortcuts", []):
            self.add_shortcut(shortcut)

        # Time display on right
        self.time_label = tk.Label(self.taskbar, text="", font=("Courier New", 11, "bold"),
                                   bg="black", fg="white", cursor="hand2")
        self.time_label._keep_font = True
        self.time_label.pack(side=tk.RIGHT, padx=15, fill=tk.Y)
        self.time_label.bind("<Button-1>", lambda event: self.desktop.open_clock_calendar())
        self.update_time()

    def add_shortcut(self, shortcut):
        path = shortcut.get("path", "")
        if not path:
            return
        icon_type = "folder" if shortcut.get("kind") == "folder" else "file"
        item = TaskbarItem(
            self.shortcuts_frame,
            shortcut.get("label") or Path(path).name,
            icon_type,
            lambda value=path: self.desktop.open_taskbar_path(value),
            {"bg": self.desktop.preferences["chrome_bg"], "fg": self.desktop.preferences["chrome_fg"]},
            lambda value=path: self.desktop.remove_taskbar_shortcut(value),
        )
        self.items.append((path, item))

    def remove_shortcut(self, path):
        for stored_path, item in list(self.items):
            if stored_path == path:
                item.destroy()
                self.items.remove((stored_path, item))

    def apply_colors(self, background, foreground):
        self.taskbar.configure(bg=background)
        self.shortcuts_frame.configure(bg=background)
        self.time_label.configure(bg=background, fg=foreground)
        for path, item in self.items:
            item.set_colors(background, foreground)
    
    def update_time(self):
        """Update time display"""
        if self.clock_24h:
            time_format = "%H:%M:%S" if self.show_seconds else "%H:%M"
        else:
            time_format = "%I:%M:%S %p" if self.show_seconds else "%I:%M %p"
        current_time = datetime.now().strftime(time_format)
        self.time_label.config(text=current_time)
        self.parent.after(1000, self.update_time)


class DesktopWindow:
    """A draggable application window hosted inside the desktop canvas."""
    def __init__(self, desktop, title, x=180, y=120, width=720, height=460):
        self.desktop = desktop
        self.canvas = desktop.desktop_canvas
        self.width = width
        self.height = height
        self.min_width = 320
        self.min_height = 220
        surface_bg = desktop.preferences.get("surface_bg", "#ffffff")
        text_fg = desktop.preferences.get("text_fg", "#000000")
        chrome_bg = desktop.preferences.get("chrome_bg", "#000000")
        chrome_fg = desktop.preferences.get("chrome_fg", "#ffffff")
        self.frame = tk.Frame(self.canvas, bg=surface_bg, relief=tk.RAISED, bd=3)
        self.window_id = self.canvas.create_window(
            x,
            y,
            window=self.frame,
            anchor=tk.NW,
            width=width,
            height=height,
        )

        self.titlebar = tk.Frame(self.frame, bg=chrome_bg, height=30)
        self.titlebar.pack(fill=tk.X)
        self.titlebar.pack_propagate(False)

        self.title_label = tk.Label(
            self.titlebar,
            text=title,
            bg=chrome_bg,
            fg=chrome_fg,
            font=("Courier New", 10, "bold"),
        )
        self.title_label.pack(side=tk.LEFT, padx=8)

        self.close_button = tk.Button(
            self.titlebar,
            text="X",
            command=self.close,
            bg=chrome_bg,
            fg=chrome_fg,
            bd=0,
            width=4,
        )
        self.close_button.pack(side=tk.RIGHT, fill=tk.Y)

        self.content = tk.Frame(self.frame, bg=surface_bg)
        self.content.pack(fill=tk.BOTH, expand=True)

        self.resize_handle = tk.Label(
            self.frame,
            text="◢",
            bg=surface_bg,
            fg=text_fg,
            cursor="sizing",
            font=("Courier New", 10),
            width=2,
        )
        self.resize_handle.place(relx=1.0, rely=1.0, anchor=tk.SE)

        self._drag_start = None
        self._resize_start = None
        self.titlebar.bind("<ButtonPress-1>", self.start_drag)
        self.titlebar.bind("<B1-Motion>", self.drag)
        self.resize_handle.bind("<ButtonPress-1>", self.start_resize)
        self.resize_handle.bind("<B1-Motion>", self.resize)

    def start_drag(self, event):
        self._drag_start = (event.x_root, event.y_root, *self.canvas.coords(self.window_id))
        self.canvas.tag_raise(self.window_id)

    def drag(self, event):
        if not self._drag_start:
            return
        start_x, start_y, window_x, window_y = self._drag_start
        self.canvas.coords(
            self.window_id,
            window_x + event.x_root - start_x,
            window_y + event.y_root - start_y,
        )

    def start_resize(self, event):
        self._resize_start = (event.x_root, event.y_root, self.width, self.height)
        self.canvas.tag_raise(self.window_id)

    def resize(self, event):
        if not self._resize_start:
            return
        start_x, start_y, start_width, start_height = self._resize_start
        new_width = max(self.min_width, start_width + event.x_root - start_x)
        new_height = max(self.min_height, start_height + event.y_root - start_y)
        self.width = new_width
        self.height = new_height
        self.canvas.itemconfigure(self.window_id, width=new_width, height=new_height)

    def close(self):
        self.canvas.delete(self.window_id)
        self.frame.destroy()
        if self in self.desktop.windows:
            self.desktop.windows.remove(self)


class DesktopGUI:
    """Windows-like desktop GUI"""
    def __init__(self, root):
        self.root = root
        self.settings_path = get_gui_settings_path()
        self.preferences = self.load_preferences()
        self.windows = []
        self.root.title("Python OS Desktop")
        self.root.geometry("1280x720")
        self.root.configure(bg="white")

        self.root.option_add("*Font", ("Courier New", self.preferences["font_size"]))
        self.root.option_add("*Background", "white")
        self.root.option_add("*Foreground", "black")
        self.root.option_add("*Button.background", "white")
        self.root.option_add("*Button.foreground", "black")
        self.root.option_add("*Button.activeBackground", "black")
        self.root.option_add("*Button.activeForeground", "white")
        self.root.option_add("*Entry.background", "white")
        self.root.option_add("*Entry.foreground", "black")
        self.root.option_add("*Listbox.background", "white")
        self.root.option_add("*Listbox.foreground", "black")
        self.root.option_add("*Listbox.selectBackground", "black")
        self.root.option_add("*Listbox.selectForeground", "white")
        self.root.option_add("*Menu.background", "white")
        self.root.option_add("*Menu.foreground", "black")
        self.root.option_add("*Menu.activeBackground", "black")
        self.root.option_add("*Menu.activeForeground", "white")

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TScale", background="white", troughcolor="white", bordercolor="black")
        style.configure("TScrollbar", background="white", troughcolor="white", bordercolor="black")
        
        # Desktop background
        self.desktop_canvas = tk.Canvas(self.root, bg="white", highlightthickness=0)
        self.desktop_canvas.pack(fill=tk.BOTH, expand=True)
        
        # Create a fixed icon layer; placed children do not expand their parent.
        self.icon_container = tk.Frame(self.desktop_canvas, bg="white", width=1280, height=670)
        self.background_label = tk.Label(self.icon_container, bg="white", bd=0)
        self.background_label.place(x=0, y=0, relwidth=1, relheight=1)
        self.background_label.lower()
        self._background_source_path = None
        self._background_original = None
        self._background_photo = None
        self._background_after = None
        self.icon_layer_id = self.desktop_canvas.create_window(
            5,
            5,
            window=self.icon_container,
            anchor=tk.NW,
            width=1270,
            height=660,
        )
        self.desktop_canvas.bind("<Configure>", self.resize_icon_layer)
        
        # Create desktop icons
        self.create_icons()
        
        # Add taskbar
        self.taskbar = Taskbar(
            self.root,
            self,
            clock_24h=self.preferences["clock_24h"],
            show_seconds=self.preferences["show_seconds"],
        )

        self.window_offset = 0
        
        # Right-click context menu
        self.root.bind("<Button-3>", self.show_context_menu)
        self.icon_container.bind("<Button-1>", self.show_desktop_menu)
        self.background_label.bind("<Button-1>", self.show_desktop_menu)

        self.apply_preferences()

    def resize_icon_layer(self, event):
        """Keep the draggable launcher area fitted to the desktop canvas."""
        width = max(1, event.width - 10)
        height = max(1, event.height - 10)
        self.desktop_canvas.itemconfigure(self.icon_layer_id, width=width, height=height)
        if self.preferences.get("background_mode") == "image":
            if self._background_after is not None:
                self.root.after_cancel(self._background_after)
            self._background_after = self.root.after(100, self.apply_desktop_background)

    def load_preferences(self):
        """Load persisted desktop preferences, falling back to safe defaults."""
        defaults = {
            "desktop_inverted": False,
            "desktop_bg": "#ffffff",
            "background_mode": "solid",
            "background_image": "",
            "surface_bg": "#ffffff",
            "text_fg": "#000000",
            "chrome_bg": "#000000",
            "chrome_fg": "#ffffff",
            "font_size": 9,
            "clock_24h": True,
            "show_seconds": True,
            "show_hidden_files": False,
            "file_manager_start": "Home",
            "taskbar_shortcuts": [],
        }
        try:
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                defaults.update({key: saved[key] for key in defaults if key in saved})
        except (OSError, ValueError, TypeError):
            pass
        try:
            defaults["font_size"] = max(8, min(14, int(defaults["font_size"])))
        except (TypeError, ValueError):
            defaults["font_size"] = 9
        color_defaults = {
            "desktop_bg": "#ffffff",
            "surface_bg": "#ffffff",
            "text_fg": "#000000",
            "chrome_bg": "#000000",
            "chrome_fg": "#ffffff",
        }
        for key, fallback in color_defaults.items():
            value = defaults.get(key)
            try:
                if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
                    raise ValueError
                int(value[1:], 16)
            except (TypeError, ValueError):
                defaults[key] = fallback
        if defaults["file_manager_start"] not in {"Home", "Drive A", "Drive B"}:
            defaults["file_manager_start"] = "Home"
        if defaults["background_mode"] not in {"solid", "image"}:
            defaults["background_mode"] = "solid"
        if not isinstance(defaults["background_image"], str):
            defaults["background_image"] = ""
        if not isinstance(defaults["taskbar_shortcuts"], list):
            defaults["taskbar_shortcuts"] = []
        defaults["taskbar_shortcuts"] = [
            item for item in defaults["taskbar_shortcuts"]
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ][:12]
        return defaults

    def save_preferences(self):
        """Persist desktop preferences for the next launch."""
        try:
            self.settings_path.write_text(json.dumps(self.preferences, indent=2), encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Settings", f"Could not save settings: {error}")

    def apply_preferences(self):
        """Apply preferences that can be updated while the desktop is running."""
        desktop_bg = self.preferences["desktop_bg"]
        surface_bg = self.preferences["surface_bg"]
        text_fg = self.preferences["text_fg"]
        chrome_bg = self.preferences["chrome_bg"]
        chrome_fg = self.preferences["chrome_fg"]
        self.root.configure(bg=desktop_bg)
        self.desktop_canvas.configure(bg=desktop_bg)
        self.icon_container.configure(bg=desktop_bg)
        for launcher in self.icon_container.winfo_children():
            if launcher is self.background_label:
                continue
            launcher.configure(bg=surface_bg)
            for child in launcher.winfo_children():
                child.configure(bg=surface_bg, fg=text_fg)
        self.taskbar.apply_colors(chrome_bg, chrome_fg)
        for window in self.windows:
            window.frame.configure(bg=surface_bg)
            window.content.configure(bg=surface_bg)
            window.titlebar.configure(bg=chrome_bg)
            window.title_label.configure(bg=chrome_bg, fg=chrome_fg)
            window.close_button.configure(bg=chrome_bg, fg=chrome_fg)
            window.resize_handle.configure(bg=surface_bg, fg=text_fg)
            if hasattr(window, "sticky_note"):
                window.sticky_toolbar.configure(bg=surface_bg)
                window.sticky_note.configure(bg=surface_bg, fg=text_fg, insertbackground=text_fg)
                window.sticky_status.configure(bg=surface_bg, fg=text_fg)
                for control in window.sticky_toolbar.winfo_children():
                    control.configure(
                        bg=surface_bg,
                        fg=text_fg,
                        activebackground=chrome_bg,
                        activeforeground=chrome_fg,
                    )
        self.taskbar.clock_24h = self.preferences["clock_24h"]
        self.taskbar.show_seconds = self.preferences["show_seconds"]
        self.apply_desktop_background()

        self.root.option_add("*Background", surface_bg)
        self.root.option_add("*Foreground", text_fg)
        self.root.option_add("*Button.background", surface_bg)
        self.root.option_add("*Button.foreground", text_fg)
        self.root.option_add("*Button.activeBackground", chrome_bg)
        self.root.option_add("*Button.activeForeground", chrome_fg)
        self.root.option_add("*Entry.background", surface_bg)
        self.root.option_add("*Entry.foreground", text_fg)
        self.root.option_add("*Listbox.background", surface_bg)
        self.root.option_add("*Listbox.foreground", text_fg)
        self.root.option_add("*Listbox.selectBackground", chrome_bg)
        self.root.option_add("*Listbox.selectForeground", chrome_fg)
        self.root.option_add("*Menu.background", surface_bg)
        self.root.option_add("*Menu.foreground", text_fg)
        self.root.option_add("*Menu.activeBackground", chrome_bg)
        self.root.option_add("*Menu.activeForeground", chrome_fg)

        style = ttk.Style(self.root)
        style.configure(".", background=surface_bg, foreground=text_fg)
        style.configure("TScale", background=surface_bg, troughcolor=surface_bg, bordercolor=chrome_bg)
        style.configure("TScrollbar", background=surface_bg, troughcolor=surface_bg, bordercolor=chrome_bg)

        font = ("Courier New", self.preferences["font_size"])
        self.root.option_add("*Font", font)

        def update_fonts(widget):
            try:
                if widget.winfo_class() not in {"Text", "ScrolledText"} and not getattr(widget, "_keep_font", False):
                    widget.configure(font=font)
            except tk.TclError:
                pass
            for child in widget.winfo_children():
                update_fonts(child)

        update_fonts(self.root)

    def apply_desktop_background(self):
        """Render the selected solid color or cover-scaled image background."""
        self._background_after = None
        color = self.preferences["desktop_bg"]
        image_path = self.preferences.get("background_image", "")
        if self.preferences.get("background_mode") != "image" or not image_path:
            self._background_photo = None
            self.background_label.configure(image="", bg=color)
            self.background_label.lower()
            return
        try:
            from PIL import Image, ImageOps, ImageTk
            if self._background_source_path != image_path or self._background_original is None:
                with Image.open(image_path) as opened:
                    self._background_original = ImageOps.exif_transpose(opened).convert("RGB").copy()
                self._background_source_path = image_path
            width = max(1, self.icon_container.winfo_width())
            height = max(1, self.icon_container.winfo_height())
            fitted = ImageOps.fit(
                self._background_original,
                (width, height),
                method=Image.Resampling.LANCZOS,
            )
            self._background_photo = ImageTk.PhotoImage(fitted)
            self.background_label.configure(image=self._background_photo, bg=color)
        except (ImportError, OSError, ValueError):
            self._background_photo = None
            self.background_label.configure(image="", bg=color)
        self.background_label.lower()
    
    def create_icons(self):
        """Create desktop icons"""
        # Terminal/CLI icon
        self.terminal_icon = DesktopIcon(
            self.icon_container, 
            "Python OS CLI", 
            "terminal",
            self.open_cli,
            10, 10
        )
        
        # File Manager icon
        self.file_manager_icon = DesktopIcon(
            self.icon_container,
            "File Manager",
            "file_explorer",
            self.open_default_file_manager,
            120, 10
        )
        
        # Drive A icon
        self.drive_a_icon = DesktopIcon(
            self.icon_container,
            "Temp Drive (A:)",
            "trash",
            self.open_drive_a,
            230, 10
        )
        
        # Drive B icon
        self.drive_b_icon = DesktopIcon(
            self.icon_container,
            "Storage (B:)",
            "drive",
            self.open_drive_b,
            340, 10
        )
        
        # Settings icon
        self.settings_icon = DesktopIcon(
            self.icon_container,
            "Settings",
            "settings",
            self.open_settings,
            450, 10
        )
        
        # About icon
        self.about_icon = DesktopIcon(
            self.icon_container,
            "About pyOS",
            "info",
            self.show_about,
            560, 10
        )

        # Text Editor icon
        self.text_editor_icon = DesktopIcon(
            self.icon_container,
            "Text Editor",
            "text_editor",
            self.open_text_editor,
            670, 10
        )

        # Internet Browser icon
        self.browser_icon = DesktopIcon(
            self.icon_container,
            "Internet",
            "browser",
            self.open_browser,
            780, 10
        )

        # Python IDE icon
        self.python_ide_icon = DesktopIcon(
            self.icon_container,
            "Python IDE",
            "python_ide",
            self.open_python_ide,
            890, 10
        )

        self.media_player_icon = DesktopIcon(
            self.icon_container,
            "Media Player",
            "media_player",
            self.open_media_player,
            1000, 10
        )

        self.notepad_icon = DesktopIcon(
            self.icon_container,
            "Sticky Notes",
            "notepad",
            self.open_notepad,
            1110, 10
        )

        self.image_viewer_icon = DesktopIcon(
            self.icon_container,
            "Image Viewer",
            "image_viewer",
            self.open_image_viewer,
            10, 84
        )
    
    def open_cli(self):
        """Open CLI application in its own process."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cli_path = os.path.join(script_dir, "pyOScli.py")

        try:
            subprocess.Popen([sys.executable, cli_path])
            messagebox.showinfo("Python OS CLI", "Terminal opened in new window")
        except Exception as e:
            messagebox.showerror("Error", f"Could not open CLI: {e}")

    def create_window(self, title, width=720, height=460):
        """Create a staggered internal desktop window."""
        self.window_offset = (self.window_offset + 24) % 144
        window = DesktopWindow(self, title, 160 + self.window_offset, 100 + self.window_offset, width, height)
        self.windows.append(window)
        return window

    def add_taskbar_shortcut(self, path):
        """Persist and render a file or folder shortcut in the taskbar."""
        path = str(Path(path).resolve())
        shortcuts = self.preferences["taskbar_shortcuts"]
        if any(item.get("path") == path for item in shortcuts):
            return
        item = {
            "path": path,
            "label": Path(path).name[:10] or path[:10],
            "kind": "folder" if Path(path).is_dir() else "file",
        }
        shortcuts.append(item)
        self.taskbar.add_shortcut(item)
        self.save_preferences()

    def remove_taskbar_shortcut(self, path):
        self.preferences["taskbar_shortcuts"] = [
            item for item in self.preferences["taskbar_shortcuts"] if item.get("path") != path
        ]
        self.taskbar.remove_shortcut(path)
        self.save_preferences()

    def open_taskbar_path(self, path):
        target = Path(path)
        if not target.exists():
            messagebox.showerror("Shortcut", f"The target no longer exists:\n{path}")
            return
        if target.is_dir():
            self.open_file_manager(target)
        elif target.suffix.lower() in MEDIA_EXTENSIONS:
            self.open_media_player(target)
        elif target.suffix.lower() in IMAGE_EXTENSIONS:
            self.open_image_viewer(target)
        else:
            self.open_text_editor(target)

    def open_system_file_explorer(self, path=None):
        """Open a folder in the host operating system's file explorer."""
        target = str(Path(path or Path.home()).resolve())
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", target])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
        except OSError as error:
            messagebox.showerror("File Explorer", f"Could not open File Explorer: {error}")

    def show_desktop_menu(self, event):
        """Show desktop actions when the empty desktop is left-clicked."""
        if event.widget not in {self.icon_container, self.background_label}:
            return
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Open File Explorer", command=self.open_default_file_manager)
        add_menu = tk.Menu(menu, tearoff=0)
        add_menu.add_command(label="Shortcut to Taskbar", command=lambda: self.open_add_browser("shortcut"))
        add_menu.add_command(label="File to Taskbar", command=self.choose_file_shortcut)
        add_menu.add_command(label="Directory to Taskbar", command=self.choose_folder_shortcut)
        add_menu.add_separator()
        add_menu.add_command(label="New Empty File", command=self.create_taskbar_file)
        add_menu.add_command(label="New Directory", command=self.create_taskbar_directory)
        menu.add_cascade(label="Add", menu=add_menu)
        menu.tk_popup(event.x_root, event.y_root)

    def choose_file_shortcut(self):
        self.open_add_browser("file_shortcut")

    def choose_folder_shortcut(self):
        self.open_add_browser("folder_shortcut")

    def create_taskbar_file(self):
        self.open_add_browser("new_file")

    def create_taskbar_directory(self):
        self.open_add_browser("new_directory")

    def open_add_browser(self, action):
        """Run add/create actions entirely inside the pyOS file explorer."""
        configurations = {
            "shortcut": ("Add Shortcut to Taskbar", "Add Shortcut", False),
            "file_shortcut": ("Add File Shortcut", "Add Shortcut", False),
            "folder_shortcut": ("Add Folder Shortcut", "Add Shortcut", False),
            "new_file": ("Create Empty File", "Create File", True),
            "new_directory": ("Create Directory", "Create Directory", True),
        }
        title, action_label, needs_name = configurations[action]
        window = self.create_window(title, width=730, height=480)
        current_path = {"path": Path.home()}
        entries = []

        toolbar = tk.Frame(window.content, bg=self.preferences["surface_bg"], relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        path_var = tk.StringVar()
        tk.Button(toolbar, text="Up", command=lambda: navigate(current_path["path"].parent)).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="Home", command=lambda: navigate(Path.home())).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="Drive A", command=lambda: navigate(self.get_drive_a_path())).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="Drive B", command=lambda: navigate(self.get_drive_b_path())).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Entry(toolbar, textvariable=path_var, state="readonly").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=5, pady=4
        )

        list_frame = tk.Frame(window.content, bg=self.preferences["surface_bg"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        file_list = tk.Listbox(list_frame, font=("Courier New", 10), yscrollcommand=scrollbar.set)
        file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.configure(command=file_list.yview)

        footer = tk.Frame(window.content, bg=self.preferences["surface_bg"], relief=tk.RAISED, bd=1)
        footer.pack(fill=tk.X)
        name_var = tk.StringVar()
        status_var = tk.StringVar()
        if needs_name:
            tk.Label(footer, text="Name:", bg=self.preferences["surface_bg"]).pack(side=tk.LEFT, padx=(6, 2))
            name_entry = tk.Entry(footer, textvariable=name_var)
            name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)
        else:
            tk.Label(footer, textvariable=status_var, bg=self.preferences["surface_bg"], anchor=tk.W).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=6
            )

        def populate():
            path = current_path["path"]
            path_var.set(str(path))
            file_list.delete(0, tk.END)
            entries.clear()
            if path.parent != path:
                entries.append(path.parent)
                file_list.insert(tk.END, "[..]")
            try:
                children = path.iterdir()
                if not self.preferences["show_hidden_files"]:
                    children = (item for item in children if not item.name.startswith("."))
                children = sorted(children, key=lambda item: (item.is_file(), item.name.lower()))
            except OSError as error:
                status_var.set(f"Could not open folder: {error}")
                return
            for child in children:
                entries.append(child)
                marker = "[DIR] " if child.is_dir() else "[FILE]"
                file_list.insert(tk.END, f"{marker:<7} {child.name}")

        def navigate(path):
            path = Path(path)
            if path.is_dir():
                current_path["path"] = path
                populate()

        def selected_path():
            selection = file_list.curselection()
            return entries[selection[0]] if selection else None

        def complete_action():
            selected = selected_path()
            try:
                if action == "file_shortcut":
                    if selected is None or not selected.is_file():
                        status_var.set("Select a file to add.")
                        return
                    self.add_taskbar_shortcut(selected)
                elif action == "shortcut":
                    if selected is None:
                        status_var.set("Select a file or directory to add.")
                        return
                    self.add_taskbar_shortcut(selected)
                elif action == "folder_shortcut":
                    target = selected if selected is not None and selected.is_dir() else current_path["path"]
                    self.add_taskbar_shortcut(target)
                else:
                    name = name_var.get().strip()
                    if not name or Path(name).name != name or name in {".", ".."}:
                        messagebox.showerror(title, "Enter a valid name without path separators.")
                        return
                    target = current_path["path"] / name
                    if action == "new_file":
                        target.touch(exist_ok=False)
                    else:
                        target.mkdir()
                    self.add_taskbar_shortcut(target)
                window.close()
            except FileExistsError:
                messagebox.showerror(title, "An item with that name already exists.")
            except OSError as error:
                messagebox.showerror(title, f"Could not complete action: {error}")

        def open_selected(event=None):
            selected = selected_path()
            if selected is None:
                return
            if selected.is_dir():
                navigate(selected)
            elif action in {"file_shortcut", "shortcut"}:
                complete_action()

        file_list.bind("<Double-Button-1>", open_selected)
        if needs_name:
            name_entry.bind("<Return>", lambda event: complete_action())
        tk.Button(footer, text="Cancel", command=window.close).pack(side=tk.RIGHT, padx=4, pady=5)
        tk.Button(footer, text=action_label, command=complete_action).pack(side=tk.RIGHT, padx=4, pady=5)
        populate()

    def open_clock_calendar(self):
        """Open a movable analogue clock and navigable monthly calendar."""
        window = self.create_window("Clock and Calendar", width=570, height=390)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]
        clock_canvas = tk.Canvas(
            window.content, width=270, height=320, bg=surface, highlightthickness=0
        )
        clock_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        calendar_panel = tk.Frame(window.content, bg=surface)
        calendar_panel.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        month_state = {"year": datetime.now().year, "month": datetime.now().month}
        closed = {"value": False}

        def draw_clock():
            if closed["value"]:
                return
            clock_canvas.delete("all")
            width = max(220, clock_canvas.winfo_width())
            height = max(220, clock_canvas.winfo_height())
            center_x, center_y = width / 2, height / 2
            radius = max(70, min(width, height) / 2 - 30)
            clock_canvas.create_oval(
                center_x - radius, center_y - radius, center_x + radius, center_y + radius,
                outline=foreground, width=3
            )
            for number in range(1, 13):
                angle = math.radians(number * 30 - 90)
                clock_canvas.create_text(
                    center_x + math.cos(angle) * (radius - 20),
                    center_y + math.sin(angle) * (radius - 20),
                    text=str(number), fill=foreground, font=("Courier New", 10, "bold")
                )
            now = datetime.now()
            hands = (
                ((now.hour % 12 + now.minute / 60) * 30, radius * 0.5, 4),
                ((now.minute + now.second / 60) * 6, radius * 0.72, 3),
                (now.second * 6, radius * 0.82, 1),
            )
            for degrees, length, line_width in hands:
                angle = math.radians(degrees - 90)
                clock_canvas.create_line(
                    center_x, center_y,
                    center_x + math.cos(angle) * length,
                    center_y + math.sin(angle) * length,
                    fill=foreground, width=line_width
                )
            clock_canvas.create_oval(center_x - 4, center_y - 4, center_x + 4, center_y + 4,
                                     fill=foreground, outline=foreground)
            self.root.after(1000, draw_clock)

        def change_month(offset):
            month = month_state["month"] + offset
            year = month_state["year"]
            if month < 1:
                month, year = 12, year - 1
            elif month > 12:
                month, year = 1, year + 1
            month_state.update(year=year, month=month)
            draw_calendar()

        def draw_calendar():
            for child in calendar_panel.winfo_children():
                child.destroy()
            header = tk.Frame(calendar_panel, bg=surface)
            header.pack(fill=tk.X, pady=(4, 10))
            tk.Button(header, text="<", width=3, command=lambda: change_month(-1)).pack(side=tk.LEFT)
            tk.Label(
                header,
                text=f"{calendar.month_name[month_state['month']]} {month_state['year']}",
                bg=surface, fg=foreground, font=("Courier New", 10, "bold")
            ).pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Button(header, text=">", width=3, command=lambda: change_month(1)).pack(side=tk.RIGHT)
            grid = tk.Frame(calendar_panel, bg=surface)
            grid.pack(fill=tk.BOTH, expand=True)
            for column, day in enumerate(("M", "T", "W", "T", "F", "S", "S")):
                tk.Label(grid, text=day, bg=surface, fg=foreground, width=3).grid(row=0, column=column)
            today = datetime.now().date()
            for row, week in enumerate(calendar.monthcalendar(month_state["year"], month_state["month"]), 1):
                for column, day in enumerate(week):
                    text = str(day) if day else ""
                    selected = day and today.year == month_state["year"] and today.month == month_state["month"] and today.day == day
                    tk.Label(
                        grid, text=text,
                        bg=self.preferences["chrome_bg"] if selected else surface,
                        fg=self.preferences["chrome_fg"] if selected else foreground,
                        width=3, pady=4
                    ).grid(row=row, column=column, sticky="nsew")
            for column in range(7):
                grid.columnconfigure(column, weight=1)

        def close_clock():
            closed["value"] = True
            window.close()

        window.close_button.configure(command=close_clock)
        draw_calendar()
        draw_clock()
        return window

    def get_drive_a_path(self):
        path = Path(os.getenv("TEMP", Path.home())) / "pyOS_Drive_A"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_drive_b_path(self):
        return get_drive_b_dir()

    def open_default_file_manager(self):
        locations = {
            "Home": Path.home(),
            "Drive A": self.get_drive_a_path(),
            "Drive B": self.get_drive_b_path(),
        }
        self.open_file_manager(locations.get(self.preferences["file_manager_start"], Path.home()))

    def open_file_manager(self, start_path):
        """Open an embedded file manager window."""
        current_path = {"path": Path(start_path)}
        window = self.create_window("File Manager", width=760, height=480)

        toolbar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        tk.Button(toolbar, text="Up", command=lambda: navigate(current_path["path"].parent)).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Home", command=lambda: navigate(Path.home())).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Drive A", command=lambda: navigate(self.get_drive_a_path())).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Drive B", command=lambda: navigate(self.get_drive_b_path())).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Refresh", command=lambda: populate()).pack(side=tk.LEFT, padx=4, pady=4)

        path_var = tk.StringVar()
        path_entry = tk.Entry(toolbar, textvariable=path_var, state="readonly")
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=4)

        list_frame = tk.Frame(window.content, bg="white")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        file_list = tk.Listbox(list_frame, font=("Consolas", 10), yscrollcommand=scrollbar.set)
        file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=file_list.yview)

        entries = []

        def populate():
            path = current_path["path"]
            path_var.set(str(path))
            file_list.delete(0, tk.END)
            entries.clear()
            if path.parent != path:
                entries.append(path.parent)
                file_list.insert(tk.END, "[..]")
            try:
                children = path.iterdir()
                if not self.preferences["show_hidden_files"]:
                    children = (item for item in children if not item.name.startswith("."))
                children = sorted(children, key=lambda item: (item.is_file(), item.name.lower()))
            except OSError as e:
                messagebox.showerror("File Manager", f"Could not open folder: {e}")
                return
            for child in children:
                entries.append(child)
                prefix = "[DIR] " if child.is_dir() else "      "
                file_list.insert(tk.END, f"{prefix}{child.name}")

        def navigate(path):
            if path.is_dir():
                current_path["path"] = path
                populate()

        def open_selected(event=None):
            selection = file_list.curselection()
            if not selection:
                return
            selected = entries[selection[0]]
            if selected.is_dir():
                navigate(selected)
            elif selected.suffix.lower() in MEDIA_EXTENSIONS:
                self.open_media_player(selected)
            elif selected.suffix.lower() in IMAGE_EXTENSIONS:
                self.open_image_viewer(selected)
            else:
                self.open_text_editor(selected)

        file_list.bind("<Double-Button-1>", open_selected)
        populate()

    def open_image_viewer(self, path=None):
        """Open an embedded image viewer for a file path."""
        try:
            from PIL import Image, ImageOps, ImageTk
        except ImportError:
            messagebox.showerror(
                "Image Viewer",
                f"Image Viewer requires Pillow. Install it with:\n{sys.executable} -m pip install Pillow",
            )
            return

        window = self.create_window("Image Viewer", width=820, height=570)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]
        toolbar = tk.Frame(window.content, bg=surface, relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        path_var = tk.StringVar(value=str(path) if path else "")
        tk.Label(toolbar, text="Path:", bg=surface, fg=foreground).pack(side=tk.LEFT, padx=(6, 2), pady=5)
        path_entry = tk.Entry(toolbar, textvariable=path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        view_frame = tk.Frame(window.content, bg=surface)
        view_frame.pack(fill=tk.BOTH, expand=True)
        x_scroll = ttk.Scrollbar(view_frame, orient=tk.HORIZONTAL)
        y_scroll = ttk.Scrollbar(view_frame, orient=tk.VERTICAL)
        image_canvas = tk.Canvas(
            view_frame,
            bg=surface,
            highlightthickness=0,
            xscrollcommand=x_scroll.set,
            yscrollcommand=y_scroll.set,
        )
        x_scroll.configure(command=image_canvas.xview)
        y_scroll.configure(command=image_canvas.yview)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        image_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        status_var = tk.StringVar(value="No image selected")
        tk.Label(window.content, textvariable=status_var, bg=surface, fg=foreground, anchor=tk.W).pack(
            fill=tk.X, padx=7, pady=4
        )

        state = {"image": None, "photo": None, "fit": True, "zoom": 1.0, "after": None}

        def render_image():
            image = state["image"]
            if image is None:
                return
            canvas_width = max(1, image_canvas.winfo_width())
            canvas_height = max(1, image_canvas.winfo_height())
            if state["fit"]:
                scale = min(canvas_width / image.width, canvas_height / image.height, 1.0)
            else:
                scale = state["zoom"]
            width = max(1, int(image.width * scale))
            height = max(1, int(image.height * scale))
            resized = image.resize((width, height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(resized)
            state["photo"] = photo
            image_canvas.delete("all")
            x = max(0, (canvas_width - width) // 2)
            y = max(0, (canvas_height - height) // 2)
            image_canvas.create_image(x, y, image=photo, anchor=tk.NW)
            image_canvas.configure(scrollregion=(0, 0, max(canvas_width, width), max(canvas_height, height)))
            mode = "Fit" if state["fit"] else f"{state['zoom'] * 100:.0f}%"
            status_var.set(f"{image.width} x {image.height} | {image.mode} | {mode}")

        def schedule_render(event=None):
            if state["after"] is not None:
                self.root.after_cancel(state["after"])
            state["after"] = self.root.after(80, render_image)

        def load_image(target):
            target = Path(target).expanduser()
            if not target.is_file():
                messagebox.showerror("Image Viewer", f"Image not found:\n{target}")
                return
            try:
                with Image.open(target) as opened:
                    loaded = ImageOps.exif_transpose(opened).copy()
                state.update(image=loaded, fit=True, zoom=1.0)
                path_var.set(str(target.resolve()))
                render_image()
            except (OSError, ValueError) as error:
                messagebox.showerror("Image Viewer", f"Could not open image: {error}")

        def choose_image():
            current = Path(path_var.get()).expanduser() if path_var.get() else Path.home()
            start = current.parent if current.is_file() else current
            self.open_file_picker(start, load_image)

        def set_fit():
            state["fit"] = True
            render_image()

        def set_actual():
            state.update(fit=False, zoom=1.0)
            render_image()

        def change_zoom(factor):
            state["fit"] = False
            state["zoom"] = max(0.1, min(8.0, state["zoom"] * factor))
            render_image()

        tk.Button(toolbar, text="Open", command=choose_image).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="Fit", command=set_fit).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="Actual", command=set_actual).pack(side=tk.LEFT, padx=3, pady=4)
        tk.Button(toolbar, text="-", width=3, command=lambda: change_zoom(0.8)).pack(side=tk.LEFT, padx=2, pady=4)
        tk.Button(toolbar, text="+", width=3, command=lambda: change_zoom(1.25)).pack(side=tk.LEFT, padx=2, pady=4)
        path_entry.bind("<Return>", lambda event: load_image(path_var.get()))
        image_canvas.bind("<Configure>", schedule_render)
        if path:
            self.root.after(0, lambda: load_image(path))
        return window

    def open_notepad(self, initial_text=""):
        """Create an independent, movable, memory-only sticky note."""
        surface_bg = self.preferences["surface_bg"]
        text_fg = self.preferences["text_fg"]
        window = self.create_window("Sticky Note", width=300, height=310)
        window.min_width = 200
        window.min_height = 180
        toolbar = tk.Frame(window.content, bg=surface_bg, relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        status_var = tk.StringVar(value="0 characters")

        note = tk.Text(
            window.content,
            wrap=tk.WORD,
            undo=True,
            bg=surface_bg,
            fg=text_fg,
            insertbackground=text_fg,
            font=("Courier New", self.preferences["font_size"]),
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        note.pack(fill=tk.BOTH, expand=True)

        status = tk.Label(
            window.content,
            textvariable=status_var,
            bg=surface_bg,
            fg=text_fg,
            anchor=tk.E,
        )
        status.pack(fill=tk.X, padx=6, pady=(0, 3))

        if initial_text:
            note.insert("1.0", initial_text)
            status_var.set(f"{len(initial_text):,} characters")

        def clear_note():
            note.delete("1.0", tk.END)
            note.edit_reset()
            status_var.set("0 characters")

        def update_status(event=None):
            if note.edit_modified():
                text = note.get("1.0", "end-1c")
                status_var.set(f"{len(text):,} characters")
                note.edit_modified(False)

        tk.Button(toolbar, text="+", width=3, command=self.open_notepad).pack(side=tk.LEFT, padx=3, pady=3)
        tk.Button(toolbar, text="Undo", command=lambda: note.event_generate("<<Undo>>")).pack(side=tk.LEFT, padx=2, pady=3)
        tk.Button(toolbar, text="Redo", command=lambda: note.event_generate("<<Redo>>")).pack(side=tk.LEFT, padx=2, pady=3)
        tk.Button(toolbar, text="Clear", command=clear_note).pack(side=tk.LEFT, padx=2, pady=3)
        window.sticky_toolbar = toolbar
        window.sticky_note = note
        window.sticky_status = status
        note.bind("<<Modified>>", update_status)
        note.bind("<Control-n>", lambda event: (self.open_notepad(), "break")[1])
        note.focus_set()
        return window

    def open_text_editor(self, path=None):
        """Open an embedded text editor window."""
        file_path = Path(path) if path else Path.home() / "untitled.txt"
        window_title = file_path.name if path else "Text Editor"
        window = self.create_window(window_title, width=780, height=500)

        toolbar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)

        tk.Label(toolbar, text="Path:", bg="white", fg="black").pack(side=tk.LEFT, padx=(6, 2), pady=5)
        path_var = tk.StringVar(value=str(file_path))
        path_entry = tk.Entry(toolbar, textvariable=path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        status_var = tk.StringVar(value="")
        status_label = tk.Label(window.content, textvariable=status_var, bg="white", fg="black", anchor=tk.W)
        status_label.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 4))

        editor = scrolledtext.ScrolledText(
            window.content,
            wrap=tk.WORD,
            font=("Consolas", 10),
            undo=True,
        )
        editor.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        def set_status(message):
            status_var.set(message)
            self.root.after(3000, lambda: status_var.set("") if status_var.get() == message else None)

        def load_file(target):
            try:
                editor.delete("1.0", tk.END)
                if target.exists():
                    editor.insert("1.0", target.read_text(encoding="utf-8", errors="replace"))
                    set_status(f"Opened {target}")
                else:
                    set_status("New file")
                editor.edit_modified(False)
            except OSError as e:
                messagebox.showerror("Text Editor", f"Could not open file: {e}")

        def save_file():
            target = Path(path_var.get()).expanduser()
            try:
                if target.parent:
                    target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(editor.get("1.0", tk.END + "-1c"), encoding="utf-8")
                editor.edit_modified(False)
                set_status(f"Saved {target}")
            except OSError as e:
                messagebox.showerror("Text Editor", f"Could not save file: {e}")

        def new_file():
            path_var.set(str(Path.home() / "untitled.txt"))
            editor.delete("1.0", tk.END)
            editor.edit_modified(False)
            set_status("New file")

        def open_from_path():
            def open_selected_file(target):
                path_var.set(str(target))
                load_file(target)

            start_path = Path(path_var.get()).expanduser()
            initial_dir = start_path.parent if start_path.suffix else start_path
            self.open_file_picker(initial_dir, open_selected_file)

        tk.Button(toolbar, text="New", command=new_file).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Open", command=open_from_path).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Save", command=save_file).pack(side=tk.LEFT, padx=4, pady=4)
        editor.bind("<Control-s>", lambda event: (save_file(), "break")[1])

        load_file(file_path)

    def open_file_picker(self, start_path, on_select):
        """Open an embedded file picker and call on_select with the chosen file."""
        current_path = {"path": Path(start_path) if Path(start_path).is_dir() else Path.home()}
        window = self.create_window("Open File", width=700, height=440)

        toolbar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        tk.Button(toolbar, text="Up", command=lambda: navigate(current_path["path"].parent)).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Home", command=lambda: navigate(Path.home())).pack(side=tk.LEFT, padx=4, pady=4)

        path_var = tk.StringVar()
        path_entry = tk.Entry(toolbar, textvariable=path_var, state="readonly")
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=4)

        list_frame = tk.Frame(window.content, bg="white")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        file_list = tk.Listbox(list_frame, font=("Consolas", 10), yscrollcommand=scrollbar.set)
        file_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=file_list.yview)

        button_bar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        button_bar.pack(fill=tk.X)
        tk.Button(button_bar, text="Cancel", command=window.close).pack(side=tk.RIGHT, padx=6, pady=5)
        tk.Button(button_bar, text="Open", command=lambda: choose_selected()).pack(side=tk.RIGHT, padx=6, pady=5)

        entries = []

        def populate():
            path = current_path["path"]
            path_var.set(str(path))
            file_list.delete(0, tk.END)
            entries.clear()
            if path.parent != path:
                entries.append(path.parent)
                file_list.insert(tk.END, "[..]")
            try:
                children = sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
            except OSError as e:
                messagebox.showerror("Open File", f"Could not open folder: {e}")
                return
            for child in children:
                entries.append(child)
                prefix = "[DIR] " if child.is_dir() else "      "
                file_list.insert(tk.END, f"{prefix}{child.name}")

        def navigate(path):
            if path.is_dir():
                current_path["path"] = path
                populate()

        def choose_selected(event=None):
            selection = file_list.curselection()
            if not selection:
                return
            selected = entries[selection[0]]
            if selected.is_dir():
                navigate(selected)
                return
            on_select(selected)
            window.close()

        file_list.bind("<Double-Button-1>", choose_selected)
        populate()

    def open_media_player(self, path=None):
        """Open an embedded VLC-backed audio and video player."""
        dll_handles = []
        if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
            candidates = [
                Path(os.environ.get("ProgramFiles", "")) / "VideoLAN" / "VLC",
                Path(os.environ.get("ProgramFiles(x86)", "")) / "VideoLAN" / "VLC",
            ]
            for candidate in candidates:
                if candidate.is_dir():
                    try:
                        dll_handles.append(os.add_dll_directory(str(candidate)))
                    except OSError:
                        pass

        try:
            import vlc
            instance = vlc.Instance("--no-video-title-show", "--quiet")
            player = instance.media_player_new()
        except Exception as error:
            messagebox.showerror(
                "Media Player",
                "Media playback requires VLC Media Player and the python-vlc package.\n\n"
                "Install VLC from https://www.videolan.org/vlc/ then run:\n"
                f"{sys.executable} -m pip install python-vlc\n\n"
                f"Details: {error}",
            )
            for handle in dll_handles:
                handle.close()
            return

        window = self.create_window("Media Player", width=840, height=560)
        state = {"loaded": False, "seeking": False, "closed": False, "muted": False}
        media_holder = {"media": None}

        video_surface = tk.Frame(window.content, bg="black")
        video_surface.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 4))
        placeholder = tk.Label(
            video_surface,
            text="Open an audio or video file",
            bg="black",
            fg="white",
            font=("Courier New", 13, "bold"),
        )
        placeholder.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        now_playing = tk.StringVar(value="No media selected")
        tk.Label(
            window.content, textvariable=now_playing, bg="white", fg="black", anchor=tk.W
        ).pack(fill=tk.X, padx=10)

        seek_var = tk.DoubleVar(value=0)
        seek = ttk.Scale(window.content, from_=0, to=1000, variable=seek_var)
        seek.pack(fill=tk.X, padx=10, pady=(4, 0))

        controls = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        controls.pack(fill=tk.X, padx=8, pady=8)
        time_var = tk.StringVar(value="00:00 / 00:00")

        def format_time(milliseconds):
            seconds = max(0, int(milliseconds / 1000))
            hours, remainder = divmod(seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            if hours:
                return f"{hours:d}:{minutes:02d}:{seconds:02d}"
            return f"{minutes:02d}:{seconds:02d}"

        def set_video_output():
            video_surface.update_idletasks()
            window_id = video_surface.winfo_id()
            if sys.platform == "win32":
                player.set_hwnd(window_id)
            elif sys.platform == "darwin":
                player.set_nsobject(window_id)
            else:
                player.set_xwindow(window_id)

        def load_media(selected):
            selected = Path(selected)
            if selected.suffix.lower() not in MEDIA_EXTENSIONS:
                messagebox.showwarning("Media Player", "Choose a supported audio or video file.")
                return
            try:
                media = instance.media_new(str(selected.resolve()))
                player.set_media(media)
                media_holder["media"] = media
                set_video_output()
                player.audio_set_volume(int(volume_var.get()))
                state["loaded"] = True
                placeholder.place_forget()
                now_playing.set(selected.name)
                player.play()
                play_button.config(text="Pause")
            except Exception as error:
                messagebox.showerror("Media Player", f"Could not open media: {error}")

        def choose_media():
            start = Path(path).parent if path else Path.home()
            self.open_file_picker(start, load_media)

        def toggle_playback():
            if not state["loaded"]:
                choose_media()
            elif player.is_playing():
                player.pause()
                play_button.config(text="Play")
            else:
                player.play()
                play_button.config(text="Pause")

        def stop_playback():
            if state["loaded"]:
                player.stop()
                seek_var.set(0)
                play_button.config(text="Play")

        def finish_seek(event=None):
            if state["loaded"]:
                player.set_position(float(seek_var.get()) / 1000.0)
            state["seeking"] = False

        def set_volume(value):
            player.audio_set_volume(int(float(value)))
            if state["muted"]:
                player.audio_set_mute(False)
                state["muted"] = False
                mute_button.config(text="Mute")

        def toggle_mute():
            state["muted"] = not state["muted"]
            player.audio_set_mute(state["muted"])
            mute_button.config(text="Unmute" if state["muted"] else "Mute")

        def refresh_status():
            if state["closed"]:
                return
            if state["loaded"]:
                current = player.get_time()
                duration = player.get_length()
                time_var.set(f"{format_time(current)} / {format_time(duration)}")
                if not state["seeking"] and duration > 0:
                    seek_var.set(max(0, player.get_position()) * 1000)
                if not player.is_playing() and current > 0 and duration > 0 and current >= duration - 500:
                    play_button.config(text="Play")
            self.root.after(250, refresh_status)

        def close_player():
            state["closed"] = True
            try:
                player.stop()
                player.release()
                if media_holder["media"] is not None:
                    media_holder["media"].release()
                instance.release()
            finally:
                for handle in dll_handles:
                    handle.close()
                window.close()

        tk.Button(controls, text="Open", command=choose_media).pack(side=tk.LEFT, padx=4, pady=5)
        play_button = tk.Button(controls, text="Play", width=8, command=toggle_playback)
        play_button.pack(side=tk.LEFT, padx=4, pady=5)
        tk.Button(controls, text="Stop", width=8, command=stop_playback).pack(side=tk.LEFT, padx=4, pady=5)
        tk.Label(controls, textvariable=time_var, bg="white", fg="black", width=15).pack(side=tk.LEFT, padx=8)
        mute_button = tk.Button(controls, text="Mute", command=toggle_mute)
        mute_button.pack(side=tk.RIGHT, padx=4, pady=5)
        volume_var = tk.DoubleVar(value=80)
        ttk.Scale(controls, from_=0, to=100, variable=volume_var, command=set_volume).pack(
            side=tk.RIGHT, padx=4, fill=tk.X
        )
        tk.Label(controls, text="Volume", bg="white", fg="black").pack(side=tk.RIGHT, padx=(8, 0))

        seek.bind("<ButtonPress-1>", lambda event: state.update(seeking=True))
        seek.bind("<ButtonRelease-1>", finish_seek)
        window.close_button.config(command=close_player)
        refresh_status()
        if path:
            load_media(path)

    def open_python_ide(self, path=None):
        """Open an embedded Python code environment."""
        file_path = Path(path) if path else Path.home() / "script.py"
        window = self.create_window("Python IDE", width=900, height=610)

        toolbar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)

        tk.Label(toolbar, text="File:", bg="white", fg="black").pack(side=tk.LEFT, padx=(6, 2), pady=5)
        path_var = tk.StringVar(value=str(file_path))
        path_entry = tk.Entry(toolbar, textvariable=path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        main_pane = ttk.PanedWindow(window.content, orient=tk.VERTICAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        editor_frame = tk.Frame(main_pane, bg="white")
        output_frame = tk.Frame(main_pane, bg="white")
        main_pane.add(editor_frame, weight=3)
        main_pane.add(output_frame, weight=1)

        editor = scrolledtext.ScrolledText(
            editor_frame,
            wrap=tk.NONE,
            font=("Consolas", 10),
            undo=True,
            bg="white",
            fg="black",
        )
        editor.pack(fill=tk.BOTH, expand=True)

        output = scrolledtext.ScrolledText(
            output_frame,
            height=10,
            wrap=tk.WORD,
            font=("Consolas", 10),
            bg="black",
            fg="white",
            insertbackground="white",
            state=tk.DISABLED,
        )
        output.pack(fill=tk.BOTH, expand=True)

        debug_bar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        debug_bar.pack(fill=tk.X)
        tk.Label(debug_bar, text="Debug command:", bg="white", fg="black").pack(side=tk.LEFT, padx=(6, 2), pady=5)
        debug_command_var = tk.StringVar()
        debug_entry = tk.Entry(debug_bar, textvariable=debug_command_var)
        debug_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        status_var = tk.StringVar(value="Ready")
        tk.Label(window.content, textvariable=status_var, bg="white", fg="black", anchor=tk.W).pack(
            side=tk.BOTTOM,
            fill=tk.X,
            padx=6,
            pady=(0, 4),
        )

        state = {"process": None, "mode": None}

        def append_output(text):
            output.configure(state=tk.NORMAL)
            output.insert(tk.END, text)
            output.see(tk.END)
            output.configure(state=tk.DISABLED)

        def clear_output():
            output.configure(state=tk.NORMAL)
            output.delete("1.0", tk.END)
            output.configure(state=tk.DISABLED)

        def load_file(target):
            try:
                editor.delete("1.0", tk.END)
                if target.exists():
                    editor.insert("1.0", target.read_text(encoding="utf-8", errors="replace"))
                    status_var.set(f"Opened {target}")
                else:
                    status_var.set("New Python file")
                editor.edit_modified(False)
            except OSError as e:
                messagebox.showerror("Python IDE", f"Could not open file: {e}")

        def save_file():
            target = Path(path_var.get()).expanduser()
            if target.suffix.lower() != ".py":
                target = target.with_suffix(".py")
                path_var.set(str(target))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(editor.get("1.0", tk.END + "-1c"), encoding="utf-8")
                editor.edit_modified(False)
                status_var.set(f"Saved {target}")
                return target
            except OSError as e:
                messagebox.showerror("Python IDE", f"Could not save file: {e}")
                return None

        def open_from_picker():
            start_path = Path(path_var.get()).expanduser()
            initial_dir = start_path.parent if start_path.suffix else start_path

            def open_selected_file(target):
                path_var.set(str(target))
                load_file(target)

            self.open_file_picker(initial_dir, open_selected_file)

        def new_file():
            stop_process()
            path_var.set(str(Path.home() / "script.py"))
            editor.delete("1.0", tk.END)
            editor.insert("1.0", "print(\"Hello from pyOS\")\n")
            editor.edit_modified(False)
            clear_output()
            status_var.set("New Python file")

        def read_process_output(process):
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    break
                self.root.after(0, append_output, chunk)
            exit_code = process.wait()
            finished_mode = state["mode"] or "process"
            self.root.after(0, lambda: status_var.set(f"{finished_mode.title()} finished with exit code {exit_code}"))
            self.root.after(0, lambda: state.update({"process": None, "mode": None}))

        def start_process(args, mode):
            if state["process"] and state["process"].poll() is None:
                messagebox.showwarning("Python IDE", "A Python process is already running.")
                return
            target = save_file()
            if not target:
                return
            clear_output()
            append_output(f"$ {' '.join(args)}\n\n")
            try:
                process = subprocess.Popen(
                    args,
                    cwd=str(target.parent),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as e:
                messagebox.showerror("Python IDE", f"Could not start Python: {e}")
                return
            state["process"] = process
            state["mode"] = mode
            status_var.set(f"{mode.title()} running")
            threading.Thread(target=read_process_output, args=(process,), daemon=True).start()

        def run_file():
            target = save_file()
            if target:
                start_process([sys.executable, str(target)], "run")

        def debug_file():
            target = save_file()
            if target:
                start_process([sys.executable, "-m", "pdb", str(target)], "debug")
                debug_entry.focus_set()

        def send_debug_command(event=None):
            command = debug_command_var.get()
            debug_command_var.set("")
            process = state["process"]
            if not process or process.poll() is not None:
                status_var.set("No active debug session")
                return "break"
            try:
                append_output(f"(pdb) {command}\n")
                process.stdin.write(command + "\n")
                process.stdin.flush()
            except Exception as e:
                messagebox.showerror("Python IDE", f"Could not send debug command: {e}")
            return "break"

        def stop_process():
            process = state["process"]
            if process and process.poll() is None:
                process.terminate()
                status_var.set("Process stopped")
            state["process"] = None
            state["mode"] = None

        tk.Button(toolbar, text="New", command=new_file).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Open", command=open_from_picker).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Save", command=save_file).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Run", command=run_file, bg="white", fg="black").pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Debug", command=debug_file, bg="white", fg="black").pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Stop", command=stop_process, bg="black", fg="white").pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(debug_bar, text="Send", command=send_debug_command).pack(side=tk.LEFT, padx=4, pady=4)

        original_close = window.close

        def close_ide():
            stop_process()
            original_close()

        window.close_button.configure(command=close_ide)
        editor.bind("<Control-s>", lambda event: (save_file(), "break")[1])
        editor.bind("<F5>", lambda event: (run_file(), "break")[1])
        debug_entry.bind("<Return>", send_debug_command)
        load_file(file_path)

    def open_browser(self):
        """Open a simple embedded internet browser."""
        window = self.create_window("Internet Browser", width=850, height=560)

        toolbar = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)

        tk.Label(toolbar, text="URL:", bg="white", fg="black").pack(side=tk.LEFT, padx=(6, 2), pady=5)
        url_var = tk.StringVar(value="https://example.com")
        url_entry = tk.Entry(toolbar, textvariable=url_var)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        status_var = tk.StringVar(value="Ready")
        status_label = tk.Label(window.content, textvariable=status_var, bg="white", fg="black", anchor=tk.W)
        status_label.pack(side=tk.BOTTOM, fill=tk.X, padx=6, pady=(0, 4))

        browser_area = tk.Frame(window.content, bg="white")
        browser_area.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        renderer = {"html_frame": None, "source_view": None}
        renderer_error = {"message": ""}
        page_cache = {
            "url": "",
            "status": None,
            "headers": {},
            "data": None,
            "source": "",
        }

        def get_html_frame_class():
            try:
                from tkinterweb import HtmlFrame
                return HtmlFrame
            except ImportError:
                status_var.set("Installing HTML/CSS renderer...")
                self.root.update_idletasks()
                try:
                    subprocess.check_call([sys.executable, "-m", "pip", "install", "tkinterweb"])
                    from tkinterweb import HtmlFrame
                    return HtmlFrame
                except Exception as e:
                    renderer_error["message"] = str(e)
                    return None

        HtmlFrame = get_html_frame_class()
        if HtmlFrame:
            html_frame = HtmlFrame(browser_area, messages_enabled=False)
            html_frame.pack(fill=tk.BOTH, expand=True)
            renderer["html_frame"] = html_frame
        else:
            source_view = scrolledtext.ScrolledText(browser_area, wrap=tk.WORD, font=("Consolas", 10))
            source_view.pack(fill=tk.BOTH, expand=True)
            renderer["source_view"] = source_view

        psutil_state = {"module": None}
        try:
            import psutil
            psutil_state["module"] = psutil
        except ImportError:
            psutil_state["module"] = None

        def normalize_url(raw_url):
            raw_url = raw_url.strip()
            if not raw_url:
                return ""
            parsed = urllib.parse.urlparse(raw_url)
            if not parsed.scheme:
                raw_url = "https://" + raw_url
            return raw_url

        def update_network_status(prefix="Ready"):
            psutil = psutil_state["module"]
            if not psutil:
                status_var.set(f"{prefix} | psutil unavailable")
                return
            counters = psutil.net_io_counters()
            sent_mb = counters.bytes_sent / (1024 * 1024)
            recv_mb = counters.bytes_recv / (1024 * 1024)
            status_var.set(f"{prefix} | Sent {sent_mb:.1f} MB | Received {recv_mb:.1f} MB")

        def set_source_content(text):
            source_view = renderer["source_view"]
            if not source_view:
                return
            source_view.configure(state=tk.NORMAL)
            source_view.delete("1.0", tk.END)
            source_view.insert("1.0", text)
            source_view.configure(state=tk.DISABLED)

        def load_url(event=None):
            url = normalize_url(url_var.get())
            if not url:
                return
            url_var.set(url)
            update_network_status("Loading")
            set_source_content("Loading...")
            self.root.update_idletasks()

            html_frame = renderer["html_frame"]
            request = urllib.request.Request(url, headers={"User-Agent": "pyOS Browser/1.0"})
            try:
                with urllib.request.urlopen(request, timeout=12) as response:
                    data = response.read(10 * 1024 * 1024 + 1)
                    if len(data) > 10 * 1024 * 1024:
                        raise ValueError("Page exceeds the 10 MB browser limit")
                    encoding = response.headers.get_content_charset() or "utf-8"
                    page_text = data.decode(encoding, errors="replace")
                    final_url = response.geturl()
                    page_cache.update({
                        "url": final_url,
                        "status": response.status,
                        "headers": dict(response.headers.items()),
                        "data": data,
                        "source": page_text,
                    })
                    url_var.set(final_url)
                    if html_frame:
                        html_frame.load_html(page_text, base_url=final_url)
                        update_network_status(f"Rendered {final_url} | HTML/CSS active | JavaScript unavailable")
                        return
                    set_source_content(page_text)
                    reason = renderer_error["message"] or "renderer unavailable"
                    update_network_status(f"Loaded source for {final_url} | HTML/CSS renderer unavailable: {reason}")
            except urllib.error.URLError as e:
                set_source_content(f"Could not load page:\n{e}")
                update_network_status("Load failed")
            except (OSError, ValueError) as e:
                set_source_content(f"Network error:\n{e}")
                update_network_status("Load failed")

        def inspect_page():
            if page_cache["data"] is None:
                messagebox.showinfo("Page Inspector", "Load a page before inspecting it.")
                return
            inspector = self.create_window("Page Inspector", width=820, height=520)
            notebook = ttk.Notebook(inspector.content)
            notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
            source_tab = scrolledtext.ScrolledText(
                notebook,
                wrap=tk.NONE,
                bg="white",
                fg="black",
                insertbackground="black",
                font=("Courier New", 10),
            )
            response_tab = scrolledtext.ScrolledText(
                notebook,
                wrap=tk.WORD,
                bg="white",
                fg="black",
                insertbackground="black",
                font=("Courier New", 10),
            )
            notebook.add(source_tab, text="Page Source")
            notebook.add(response_tab, text="Response")
            source_tab.insert("1.0", page_cache["source"])
            source_tab.configure(state=tk.DISABLED)
            metadata = [
                f"URL: {page_cache['url']}",
                f"Status: {page_cache['status']}",
                f"Size: {len(page_cache['data']):,} bytes",
                "",
            ]
            metadata.extend(f"{name}: {value}" for name, value in page_cache["headers"].items())
            response_tab.insert("1.0", "\n".join(metadata))
            response_tab.configure(state=tk.DISABLED)

        def save_page():
            if page_cache["data"] is None:
                messagebox.showinfo("Download Page", "Load a page before downloading it.")
                return
            parsed = urllib.parse.urlparse(page_cache["url"])
            suggested_name = Path(parsed.path).name or "index.html"
            destination = filedialog.asksaveasfilename(
                parent=self.root,
                title="Download Page",
                initialdir=str(get_downloads_dir()),
                initialfile=suggested_name,
                defaultextension=".html",
                filetypes=(("HTML pages", "*.html;*.htm"), ("All files", "*.*")),
            )
            if not destination:
                return
            try:
                Path(destination).write_bytes(page_cache["data"])
                update_network_status(f"Saved {destination}")
            except OSError as error:
                messagebox.showerror("Download Page", f"Could not save page: {error}")

        tk.Button(toolbar, text="Go", command=load_url).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Inspect", command=inspect_page).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Save Page", command=save_page).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Network", command=lambda: update_network_status("Network status")).pack(
            side=tk.LEFT,
            padx=4,
            pady=4,
        )
        url_entry.bind("<Return>", load_url)
        if renderer["html_frame"]:
            update_network_status("Ready | HTML/CSS renderer active | JavaScript unavailable")
        else:
            reason = renderer_error["message"] or "tkinterweb unavailable"
            update_network_status(f"Ready | HTML/CSS renderer missing: {reason}")
    
    def open_drive_a(self):
        """Open Drive A (Temporary)"""
        self.open_file_manager(self.get_drive_a_path())
    
    def open_drive_b(self):
        """Open Drive B (Permanent)"""
        self.open_file_manager(self.get_drive_b_path())
    
    def open_settings_legacy(self):
        """Open settings inside the desktop."""
        settings_window = self.create_window("Settings", width=430, height=330)

        tk.Label(
            settings_window.content,
            text="Python OS Settings",
            font=("Courier New", 14, "bold"),
            bg="white",
        ).pack(pady=10)
        
        # Virtual Drives Info
        drives_info = """
Virtual Drives:
• Drive A: Temporary Storage (RAM-like)
• Drive B: Permanent Storage (persistent)
• Drive C: Home Directory

Features:
• GUI-based terminal interface
• File browser with drag-and-drop
• Theme customization
• Network diagnostics
• System monitoring
"""
        tk.Label(
            settings_window.content,
            text=drives_info,
            font=("Courier New", 10),
            justify=tk.LEFT,
            bg="white",
        ).pack(padx=20, pady=10)

        tk.Button(settings_window.content, text="Close", command=settings_window.close).pack(pady=10)
    
    def open_settings(self):
        """Open functional, persistent desktop settings."""
        window = self.create_window("Settings", width=620, height=500)
        desktop_color = tk.StringVar(value=self.preferences["desktop_bg"])
        background_mode = tk.StringVar(value=self.preferences["background_mode"])
        background_image = tk.StringVar(value=self.preferences["background_image"])
        surface_color = tk.StringVar(value=self.preferences["surface_bg"])
        text_color = tk.StringVar(value=self.preferences["text_fg"])
        chrome_color = tk.StringVar(value=self.preferences["chrome_bg"])
        chrome_text_color = tk.StringVar(value=self.preferences["chrome_fg"])
        font_size = tk.IntVar(value=self.preferences["font_size"])
        clock_24h = tk.BooleanVar(value=self.preferences["clock_24h"])
        seconds = tk.BooleanVar(value=self.preferences["show_seconds"])
        hidden = tk.BooleanVar(value=self.preferences["show_hidden_files"])
        start_location = tk.StringVar(value=self.preferences["file_manager_start"])
        status = tk.StringVar()

        notebook = ttk.Notebook(window.content)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        appearance = tk.Frame(notebook, bg="white")
        clock = tk.Frame(notebook, bg="white")
        files = tk.Frame(notebook, bg="white")
        notebook.add(appearance, text="Appearance")
        notebook.add(clock, text="Clock")
        notebook.add(files, text="Files")

        tk.Label(appearance, text="BACKGROUND", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(10, 3)
        )
        mode_row = tk.Frame(appearance, bg="white")
        mode_row.pack(fill=tk.X, padx=16, pady=2)
        tk.Radiobutton(mode_row, text="Solid color", variable=background_mode, value="solid").pack(side=tk.LEFT)
        tk.Radiobutton(mode_row, text="Image", variable=background_mode, value="image").pack(side=tk.LEFT, padx=10)
        image_row = tk.Frame(appearance, bg="white")
        image_row.pack(fill=tk.X, padx=16, pady=(2, 6))
        image_entry = tk.Entry(image_row, textvariable=background_image, state="readonly")
        image_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def select_background_image():
            current = Path(background_image.get()).expanduser() if background_image.get() else Path.home()
            start = current.parent if current.is_file() else current

            def selected(path):
                background_image.set(str(path))
                background_mode.set("image")

            self.open_file_picker(start, selected)

        tk.Button(image_row, text="Choose Image", command=select_background_image).pack(side=tk.LEFT, padx=(5, 0))

        tk.Label(appearance, text="COLOR SCHEME", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(4, 4)
        )

        color_rows = (
            ("Desktop", desktop_color),
            ("Windows and controls", surface_color),
            ("Text", text_color),
            ("Title bars and taskbar", chrome_color),
            ("Title-bar text", chrome_text_color),
        )
        color_swatches = []

        def choose_color(variable, swatch):
            selected = colorchooser.askcolor(variable.get(), parent=self.root, title="Choose OS Color")[1]
            if selected:
                variable.set(selected)
                swatch.configure(bg=selected, activebackground=selected)

        for label_text, variable in color_rows:
            row = tk.Frame(appearance, bg="white")
            row.pack(fill=tk.X, padx=16, pady=2)
            tk.Label(row, text=label_text, width=24, anchor=tk.W).pack(side=tk.LEFT)
            swatch = tk.Button(row, text="", width=4, bg=variable.get(), activebackground=variable.get())
            swatch.configure(command=lambda value=variable, button=swatch: choose_color(value, button))
            swatch.pack(side=tk.LEFT, padx=6)
            color_swatches.append((variable, swatch))
            tk.Label(row, textvariable=variable, width=9, anchor=tk.W).pack(side=tk.LEFT)

        font_row = tk.Frame(appearance, bg="white")
        font_row.pack(fill=tk.X, padx=16, pady=(8, 4))
        tk.Label(font_row, text="Interface font size:").pack(side=tk.LEFT)
        tk.Spinbox(font_row, from_=8, to=14, textvariable=font_size, width=5).pack(side=tk.LEFT, padx=10)

        tk.Label(clock, text="TASKBAR CLOCK", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(18, 8)
        )
        tk.Checkbutton(clock, text="Use 24-hour time", variable=clock_24h, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=5
        )
        tk.Checkbutton(clock, text="Show seconds", variable=seconds, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=5
        )

        tk.Label(files, text="FILE MANAGER", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(18, 8)
        )
        tk.Checkbutton(files, text="Show hidden files", variable=hidden, anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=5
        )
        location_row = tk.Frame(files, bg="white")
        location_row.pack(fill=tk.X, padx=16, pady=10)
        tk.Label(location_row, text="Open File Manager at:").pack(side=tk.LEFT)
        ttk.Combobox(
            location_row,
            textvariable=start_location,
            values=("Home", "Drive A", "Drive B"),
            state="readonly",
            width=14,
        ).pack(side=tk.LEFT, padx=10)
        tk.Label(
            files,
            text="Drive A is temporary. Drive B persists in your home folder.",
            anchor=tk.W,
        ).pack(fill=tk.X, padx=16, pady=8)

        def apply_settings():
            try:
                selected_size = max(8, min(14, int(font_size.get())))
            except (ValueError, tk.TclError):
                selected_size = 9
                font_size.set(selected_size)
            selected_background = background_image.get().strip()
            if background_mode.get() == "image":
                if not Path(selected_background).is_file() or Path(selected_background).suffix.lower() not in IMAGE_EXTENSIONS:
                    messagebox.showerror("Settings", "Choose a supported image file for the desktop background.")
                    return
            self.preferences.update({
                "desktop_inverted": False,
                "desktop_bg": desktop_color.get(),
                "background_mode": background_mode.get(),
                "background_image": selected_background,
                "surface_bg": surface_color.get(),
                "text_fg": text_color.get(),
                "chrome_bg": chrome_color.get(),
                "chrome_fg": chrome_text_color.get(),
                "font_size": selected_size,
                "clock_24h": bool(clock_24h.get()),
                "show_seconds": bool(seconds.get()),
                "show_hidden_files": bool(hidden.get()),
                "file_manager_start": start_location.get(),
            })
            self.apply_preferences()
            self.save_preferences()
            status.set("Settings applied and saved.")

        def restore_defaults():
            desktop_color.set("#ffffff")
            background_mode.set("solid")
            background_image.set("")
            surface_color.set("#ffffff")
            text_color.set("#000000")
            chrome_color.set("#000000")
            chrome_text_color.set("#ffffff")
            for variable, swatch in color_swatches:
                swatch.configure(bg=variable.get(), activebackground=variable.get())
            font_size.set(9)
            clock_24h.set(True)
            seconds.set(True)
            hidden.set(False)
            start_location.set("Home")
            apply_settings()

        footer = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Label(footer, textvariable=status, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        tk.Button(footer, text="Defaults", command=restore_defaults).pack(side=tk.RIGHT, padx=4, pady=5)
        tk.Button(footer, text="Close", command=window.close).pack(side=tk.RIGHT, padx=4, pady=5)
        tk.Button(footer, text="Apply", command=apply_settings).pack(side=tk.RIGHT, padx=4, pady=5)

    def show_about(self):
        """Show about dialog"""
        about_text = """Python OS v4.0 - Desktop Edition

A comprehensive Python-based operating system emulator
with GUI interface, virtual drives, and advanced features.

Features:
• Desktop GUI with Windows-like interface
• Terminal/CLI mode with command execution
• Virtual drives (A: temp, B: permanent, C: home)
• File manager and navigation
• Theme customization
• Network diagnostics
• System information
• 25+ file operations commands

Created with Python & Tkinter
"""
        messagebox.showinfo("About Python OS", about_text)
    
    def show_context_menu(self, event):
        """Show right-click context menu"""
        context_menu = tk.Menu(self.root, tearoff=0)
        context_menu.add_command(label="Open Terminal", command=self.open_cli)
        context_menu.add_command(label="Open File Manager", command=self.open_default_file_manager)
        context_menu.add_command(label="New Sticky Note", command=self.open_notepad)
        context_menu.add_command(label="Open Text Editor", command=self.open_text_editor)
        context_menu.add_command(label="Open Image Viewer", command=self.open_image_viewer)
        context_menu.add_command(label="Open Internet Browser", command=self.open_browser)
        context_menu.add_command(label="Open Python IDE", command=self.open_python_ide)
        context_menu.add_command(label="Open Media Player", command=self.open_media_player)
        context_menu.add_separator()
        context_menu.add_command(label="Refresh", command=self.refresh_desktop)
        context_menu.add_command(label="About", command=self.show_about)
        
        context_menu.post(event.x_root, event.y_root)
    
    def refresh_desktop(self):
        """Refresh desktop"""
        messagebox.showinfo("Refresh", "Desktop refreshed!")


def main():
    """Main entry point"""
    root = tk.Tk()
    app = DesktopGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
