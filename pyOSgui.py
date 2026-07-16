
import tkinter as tk
import tkinter.font as tkfont
from tkinter import colorchooser, filedialog, messagebox, scrolledtext, simpledialog, ttk
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
import ast
import functools
import base64
import socket
import struct
import time
import uuid
import queue
import io
import random
import ipaddress
import shutil
import html
import re
import webbrowser
import xml.etree.ElementTree as ET
import platform
from pathlib import Path

from pyos_config import (
    CONFIG_FILE,
    get_cli_settings_path,
    get_data_dir,
    get_downloads_dir,
    get_drive_b_dir,
    get_gui_settings_path,
    load_config,
    relaunch_in_configured_environment,
)
from pyos_auth import (
    authenticate,
    change_credentials_dialog,
    credentials_path,
    get_username,
    has_passkey,
    passkey_support_status,
    register_passkey_dialog,
    remove_passkeys_dialog,
    verify_credentials,
)

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

WEATHER_DESCRIPTIONS = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Freezing fog", 51: "Light drizzle", 53: "Drizzle",
    55: "Heavy drizzle", 56: "Light freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Light freezing rain",
    67: "Freezing rain", 71: "Light snow", 73: "Snow", 75: "Heavy snow",
    77: "Snow grains", 80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Light snow showers", 86: "Heavy snow showers", 95: "Thunderstorm",
    96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}

MESSENGER_DISCOVERY_PORT = 54545
MESSENGER_MAX_IMAGE_BYTES = 5 * 1024 * 1024
MESSENGER_MAX_PACKET_BYTES = 8 * 1024 * 1024


def browser_input_to_url(raw_value):
    """Convert an address-bar value into a URL or a web-search URL."""
    value = raw_value.strip()
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme.lower() in {"http", "https", "ftp", "file"}:
        return value
    looks_like_host = False
    if not any(character.isspace() for character in value) and "@" not in value:
        try:
            host = urllib.parse.urlsplit("//" + value).hostname or ""
            looks_like_host = host.casefold() == "localhost" or "." in host
            if not looks_like_host and host:
                try:
                    ipaddress.ip_address(host)
                    looks_like_host = True
                except ValueError:
                    pass
        except ValueError:
            looks_like_host = False
    if looks_like_host:
        return "https://" + value
    return "https://www.bing.com/search?" + urllib.parse.urlencode({"q": value})


class PeerMessenger:
    """LAN discovery and direct TCP transport for pyOS Messenger."""

    def __init__(self, username, event_callback):
        self.username = username
        self.event_callback = event_callback
        self.instance_id = uuid.uuid4().hex
        self.peers = {}
        self.history = []
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._udp = None
        self._tcp = None
        self.port = None

    def start(self):
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind(("0.0.0.0", 0))
        self._tcp.listen(8)
        self._tcp.settimeout(0.75)
        self.port = self._tcp.getsockname()[1]

        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._udp.bind(("", MESSENGER_DISCOVERY_PORT))
        self._udp.settimeout(0.75)
        for target in (self._listen_tcp, self._listen_discovery, self._announce_loop):
            threading.Thread(target=target, daemon=True).start()

    def stop(self):
        self._stopping.set()
        for connection in (self._udp, self._tcp):
            if connection:
                try:
                    connection.close()
                except OSError:
                    pass

    def set_event_callback(self, callback):
        self.event_callback = callback

    def _emit(self, event, payload=None):
        callback = self.event_callback
        if callback:
            callback(event, payload)

    def _announcement(self):
        return json.dumps({
            "app": "pyos-messenger",
            "version": 1,
            "id": self.instance_id,
            "username": self.username,
            "port": self.port,
        }).encode("utf-8")

    def _announce_loop(self):
        while not self._stopping.is_set():
            packet = self._announcement()
            for address in (("255.255.255.255", MESSENGER_DISCOVERY_PORT),
                            ("127.0.0.1", MESSENGER_DISCOVERY_PORT)):
                try:
                    self._udp.sendto(packet, address)
                except OSError:
                    pass
            self._expire_peers()
            self._stopping.wait(3)

    def _listen_discovery(self):
        while not self._stopping.is_set():
            try:
                packet, address = self._udp.recvfrom(4096)
                data = json.loads(packet.decode("utf-8"))
                if data.get("app") != "pyos-messenger" or data.get("id") == self.instance_id:
                    continue
                username = str(data.get("username", "")).strip()
                port = int(data.get("port", 0))
                if not 3 <= len(username) <= 32 or not 1 <= port <= 65535:
                    continue
                with self._lock:
                    self.peers[username.casefold()] = {
                        "username": username, "host": address[0], "port": port,
                        "seen": time.monotonic(), "id": data.get("id"),
                    }
                self._emit("peers", self.peer_names())
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue

    def _expire_peers(self):
        cutoff = time.monotonic() - 12
        changed = False
        with self._lock:
            for key in [key for key, peer in self.peers.items() if peer["seen"] < cutoff]:
                del self.peers[key]
                changed = True
        if changed:
            self._emit("peers", self.peer_names())

    def peer_names(self):
        with self._lock:
            return sorted((peer["username"] for peer in self.peers.values()), key=str.casefold)

    def _listen_tcp(self):
        while not self._stopping.is_set():
            try:
                connection, address = self._tcp.accept()
                connection.settimeout(5)
                threading.Thread(
                    target=self._receive_connection, args=(connection, address), daemon=True
                ).start()
            except OSError:
                continue

    @staticmethod
    def _receive_exact(connection, size):
        chunks = bytearray()
        while len(chunks) < size:
            chunk = connection.recv(size - len(chunks))
            if not chunk:
                raise ConnectionError("Connection closed before the message was complete.")
            chunks.extend(chunk)
        return bytes(chunks)

    def _receive_connection(self, connection, address):
        try:
            with connection:
                size = struct.unpack("!I", self._receive_exact(connection, 4))[0]
                if not 1 <= size <= MESSENGER_MAX_PACKET_BYTES:
                    raise ValueError("Incoming message is too large.")
                message = json.loads(self._receive_exact(connection, size).decode("utf-8"))
            message = self._validate_message(message)
            message.update(direction="incoming", host=address[0], timestamp=time.time())
            with self._lock:
                self.history.append(message)
            self._emit("message", message)
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError, ConnectionError) as error:
            self._emit("error", f"Rejected incoming message: {error}")

    @staticmethod
    def _validate_message(message):
        if not isinstance(message, dict) or message.get("protocol") != "pyos-message-1":
            raise ValueError("Unsupported message format.")
        sender = str(message.get("sender", "")).strip()
        kind = message.get("kind")
        if not 3 <= len(sender) <= 32 or kind not in {"text", "image"}:
            raise ValueError("Invalid message metadata.")
        validated = {"sender": sender, "kind": kind, "text": str(message.get("text", ""))[:10000]}
        if kind == "image":
            raw = base64.b64decode(message.get("data", ""), validate=True)
            if len(raw) > MESSENGER_MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the 5 MB limit.")
            validated.update(
                data=raw, filename=Path(str(message.get("filename", "image"))).name[:255]
            )
        return validated

    def send_text(self, username, text):
        text = text.strip()
        if not text:
            raise ValueError("Enter a message.")
        return self._send(username, {"protocol": "pyos-message-1", "sender": self.username,
                                     "kind": "text", "text": text[:10000]})

    def send_image(self, username, path):
        path = Path(path)
        data = path.read_bytes()
        if len(data) > MESSENGER_MAX_IMAGE_BYTES:
            raise ValueError("Images must be 5 MB or smaller.")
        return self._send(username, {
            "protocol": "pyos-message-1", "sender": self.username, "kind": "image",
            "text": "", "filename": path.name, "data": base64.b64encode(data).decode("ascii"),
        })

    def _send(self, username, message):
        with self._lock:
            peer = self.peers.get(username.casefold())
        if not peer:
            raise ValueError(f"{username} is no longer available.")
        encoded = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MESSENGER_MAX_PACKET_BYTES:
            raise ValueError("Message is too large.")
        with socket.create_connection((peer["host"], peer["port"]), timeout=5) as connection:
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        local = self._validate_message(message)
        local.update(direction="outgoing", recipient=peer["username"], timestamp=time.time())
        with self._lock:
            self.history.append(local)
        self._emit("message", local)
        return local

CALCULATOR_FUNCTIONS = {
    "abs": abs,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
    "floor": math.floor,
    "ceil": math.ceil,
}
CALCULATOR_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


@functools.lru_cache(maxsize=256)
def _parse_calculator_expression(expression):
    try:
        return ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("Invalid expression.") from error


def evaluate_calculator_expression(expression, x=None, variables=None):
    """Evaluate a restricted mathematical expression without Python eval()."""
    expression = expression.strip().replace("^", "**")
    if not expression:
        raise ValueError("Enter an expression.")
    if len(expression) > 200:
        raise ValueError("Expression is too long.")
    tree = _parse_calculator_expression(expression)

    binary_operators = {
        ast.Add: lambda left, right: left + right,
        ast.Sub: lambda left, right: left - right,
        ast.Mult: lambda left, right: left * right,
        ast.Div: lambda left, right: left / right,
        ast.Mod: lambda left, right: left % right,
        ast.Pow: lambda left, right: left ** right,
    }
    unary_operators = {ast.UAdd: lambda value: value, ast.USub: lambda value: -value}

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            return node.value
        if isinstance(node, ast.Name):
            if node.id == "x" and x is not None:
                return x
            if variables is not None and node.id in variables:
                return variables[node.id]
            if node.id in CALCULATOR_CONSTANTS:
                return CALCULATOR_CONSTANTS[node.id]
            raise ValueError(f"Unknown value: {node.id}")
        if isinstance(node, ast.UnaryOp) and type(node.op) in unary_operators:
            return unary_operators[type(node.op)](visit(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in binary_operators:
            left = visit(node.left)
            right = visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ValueError("Exponent is too large.")
            return binary_operators[type(node.op)](left, right)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = CALCULATOR_FUNCTIONS.get(node.func.id)
            if function is None or node.keywords:
                raise ValueError("Unsupported function.")
            return function(*(visit(argument) for argument in node.args))
        raise ValueError("Unsupported expression.")

    try:
        result = visit(tree)
    except (ArithmeticError, OverflowError, TypeError) as error:
        raise ValueError(str(error) or "Calculation failed.") from error
    try:
        finite = not isinstance(result, complex) and math.isfinite(float(result))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError("Result is not a finite real number.")
    return result

class FlatButton(tk.Label):
    """Label-based button that honors bg/fg colors on every platform.

    macOS Aqua ignores a tk.Button's background color, which left
    chrome-colored buttons (white text on a black bar) rendering as white
    text on the native pale-grey button face. A Label paints its colors
    faithfully; Enter/Leave swap to the active colors for hover feedback.
    """

    def __init__(self, master=None, **kwargs):
        self._command = kwargs.pop("command", None)
        self._active_bg = kwargs.pop("activebackground", None)
        self._active_fg = kwargs.pop("activeforeground", None)
        kwargs.setdefault("cursor", "hand2")
        super().__init__(master, **kwargs)
        self._hovered = False
        self._normal_bg = self.cget("bg")
        self._normal_fg = self.cget("fg")
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", lambda event: self.invoke())

    def _on_enter(self, _event):
        self._hovered = True
        self._normal_bg = self.cget("bg")
        self._normal_fg = self.cget("fg")
        super().configure(
            bg=self._active_bg or self._normal_fg,
            fg=self._active_fg or self._normal_bg,
        )

    def _on_leave(self, _event):
        if self._hovered:
            self._hovered = False
            super().configure(bg=self._normal_bg, fg=self._normal_fg)

    def invoke(self):
        if self._command is not None:
            self._command()

    def configure(self, cnf=None, **kwargs):
        options = dict(cnf or {})
        options.update(kwargs)
        if "command" in options:
            self._command = options.pop("command")
        if "activebackground" in options:
            self._active_bg = options.pop("activebackground")
        if "activeforeground" in options:
            self._active_fg = options.pop("activeforeground")
        if self._hovered and options.keys() & {"bg", "background", "fg", "foreground"}:
            self._hovered = False
        if not options and (cnf is not None or kwargs):
            return None
        return super().configure(**options)

    config = configure


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
        self.position_changed = None
        self.frame = tk.Frame(parent, bg="white", relief=tk.RAISED, bd=2)
        self.frame._desktop_icon = self
        self.frame.place(x=x, y=y, width=width, height=height)

        self.icon = tk.Canvas(
            self.frame, width=34, height=28, bg="white", highlightthickness=0,
            cursor="hand2",
        )
        self.icon.pack(pady=(2, 0))

        self.name_label = tk.Label(
            self.frame,
            text=name.upper(),
            font=("Courier New", 7, "bold"),
            bg="white",
            fg="black",
            wraplength=88,
            justify=tk.CENTER,
            relief=tk.FLAT,
            cursor="hand2",
        )
        self.name_label.pack(fill=tk.BOTH, expand=True, padx=3, pady=(0, 2))

        self.set_colors("white", "black")

        for widget in (self.icon, self.name_label, self.frame):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag)
            widget.bind("<ButtonRelease-1>", self.finish_drag)

    def set_colors(self, background, foreground):
        self.frame.configure(bg=background)
        self.icon.configure(bg=background)
        self.name_label.configure(bg=background, fg=foreground)
        self._draw_pixel_icon(background, foreground)

    def _draw_pixel_icon(self, background, foreground):
        """Draw a compact monochrome pixel-art glyph for the launcher type."""
        canvas = self.icon
        canvas.delete("all")
        pixel = 3

        def block(x, y, width=1, height=1, fill=foreground, outline=""):
            canvas.create_rectangle(
                x * pixel + 2, y * pixel + 1,
                (x + width) * pixel + 1, (y + height) * pixel,
                fill=fill, outline=outline,
            )

        def outline_box(x, y, width, height):
            block(x, y, width, 1)
            block(x, y + height - 1, width, 1)
            block(x, y, 1, height)
            block(x + width - 1, y, 1, height)

        kind = self.icon_type
        if kind in {"file_explorer", "folder"}:
            block(1, 3, 3, 1)
            block(3, 2, 3, 1)
            outline_box(1, 3, 9, 5)
        elif kind == "terminal":
            outline_box(1, 1, 10, 8)
            block(3, 3); block(4, 4); block(3, 5)
            block(6, 6, 3, 1)
        elif kind == "trash":
            block(3, 1, 5, 1); block(4, 0, 3, 1)
            outline_box(3, 2, 5, 7)
            block(5, 3, 1, 4)
        elif kind == "drive":
            outline_box(1, 2, 10, 6)
            block(2, 5, 8, 1)
            block(8, 3); block(9, 3)
        elif kind == "settings":
            block(5, 0, 2, 2); block(5, 7, 2, 2)
            block(1, 3, 2, 3); block(9, 3, 2, 3)
            outline_box(4, 2, 4, 5)
            block(5, 3, 2, 3, fill=background)
        elif kind == "info":
            outline_box(2, 0, 8, 9)
            block(5, 2, 2, 1); block(5, 4, 2, 4)
        elif kind in {"text_editor", "notepad"}:
            outline_box(2, 0, 8, 9)
            block(4, 2, 4, 1); block(4, 4, 4, 1); block(4, 6, 3, 1)
        elif kind == "browser":
            outline_box(1, 1, 10, 8)
            block(1, 3, 10, 1)
            block(3, 5, 6, 1); block(4, 7, 4, 1)
        elif kind == "python_ide":
            outline_box(1, 1, 10, 8)
            block(3, 3); block(4, 4); block(3, 5)
            block(8, 3); block(7, 4); block(8, 5)
        elif kind == "media_player":
            outline_box(1, 1, 10, 8)
            block(4, 3, 1, 4); block(5, 4, 1, 3); block(6, 5, 1, 1)
        elif kind == "image_viewer":
            outline_box(1, 1, 10, 8)
            block(8, 2, 1, 1)
            block(2, 7, 2, 1); block(3, 6, 2, 1); block(5, 5, 2, 1); block(7, 6, 3, 2)
        elif kind == "calculator":
            outline_box(2, 0, 8, 9)
            block(3, 1, 6, 2, fill=background)
            for x in (3, 5, 7):
                for y in (4, 6):
                    block(x, y)
        elif kind == "messenger":
            outline_box(1, 1, 10, 6)
            block(3, 7, 2, 1); block(2, 8, 2, 1)
            block(3, 3); block(5, 3); block(7, 3)
        elif kind == "ai_chat":
            block(5, 0, 2, 1)
            outline_box(2, 1, 8, 7)
            block(4, 3); block(7, 3)
            block(4, 5, 4, 1)
        elif kind == "games":
            block(2, 3, 8, 4)
            block(1, 5, 2, 3); block(9, 5, 2, 3)
            block(4, 4, 1, 3); block(3, 5, 3, 1)
            block(8, 4); block(9, 5)
        elif kind == "dispenser":
            block(3, 0, 6, 2)
            outline_box(1, 2, 10, 4)
            block(8, 3); block(3, 4, 4, 1)
            block(3, 6, 6, 3)
            block(4, 7, 4, 1, fill=background)
        else:
            outline_box(2, 0, 8, 9)
            block(7, 0, 1, 3); block(8, 2, 2, 1)

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
            if self.position_changed:
                self.position_changed(self.x, self.y)

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

    def restore_position(self, x, y):
        """Restore a persisted position and constrain it once layout is available."""
        self.x = max(0, int(x))
        self.y = max(0, int(y))
        self.frame.place_configure(x=self.x, y=self.y)

        def constrain(attempt=0):
            try:
                parent_width = self.parent.winfo_width()
                parent_height = self.parent.winfo_height()
                if (parent_width <= 1 or parent_height <= 1) and attempt < 5:
                    self.parent.after(25, lambda: constrain(attempt + 1))
                    return
                try:
                    configured_width = int(self.parent.cget("width"))
                    configured_height = int(self.parent.cget("height"))
                except (tk.TclError, TypeError, ValueError):
                    configured_width = configured_height = 1
                parent_width = max(
                    self.width, parent_width, self.parent.winfo_reqwidth(), configured_width,
                )
                parent_height = max(
                    self.height, parent_height, self.parent.winfo_reqheight(), configured_height,
                )
                self.x = max(0, min(self.x, parent_width - self.width))
                self.y = max(0, min(self.y, parent_height - self.height))
                self.frame.place_configure(x=self.x, y=self.y)
            except tk.TclError:
                pass

        self.parent.after_idle(constrain)
    
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
        self.window_buttons = {}
        self.taskbar = tk.Frame(parent, bg="black", height=64, relief=tk.RAISED, bd=2)
        self.taskbar.pack(side=tk.BOTTOM, fill=tk.X)
        self.taskbar.pack_propagate(False)

        self.shortcuts_frame = tk.Frame(self.taskbar, bg="black")
        self.shortcuts_frame.pack(side=tk.LEFT, fill=tk.Y)
        for shortcut in desktop.preferences.get("taskbar_shortcuts", []):
            self.add_shortcut(shortcut)

        self.windows_frame = tk.Frame(self.taskbar, bg="black")
        self.windows_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(5, 0))

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

    def add_minimized_window(self, window):
        if window in self.window_buttons:
            return
        button = FlatButton(
            self.windows_frame,
            text=window.title[:18],
            command=window.restore,
            bg=self.desktop.preferences["chrome_bg"],
            fg=self.desktop.preferences["chrome_fg"],
            activebackground=self.desktop.preferences["surface_bg"],
            activeforeground=self.desktop.preferences["text_fg"],
            relief=tk.RAISED,
            width=18,
        )
        button.pack(side=tk.LEFT, fill=tk.Y, padx=2, pady=5)
        self.window_buttons[window] = button

    def remove_minimized_window(self, window):
        button = self.window_buttons.pop(window, None)
        if button:
            button.destroy()

    def apply_colors(self, background, foreground):
        self.taskbar.configure(bg=background)
        self.shortcuts_frame.configure(bg=background)
        self.windows_frame.configure(bg=background)
        self.time_label.configure(bg=background, fg=foreground)
        for path, item in self.items:
            item.set_colors(background, foreground)
        for button in self.window_buttons.values():
            button.configure(bg=background, fg=foreground)
    
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
        self.title = title
        self.minimized = False
        self.min_width = 320
        self.min_height = 220
        canvas_width, canvas_height = self._canvas_size()
        self.width = min(max(self.min_width, width), canvas_width)
        self.height = min(max(self.min_height, height), canvas_height)
        x = max(0, min(x, canvas_width - self.width))
        y = max(0, min(y, canvas_height - self.height))
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
            width=self.width,
            height=self.height,
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

        self.close_button = FlatButton(
            self.titlebar,
            text="X",
            command=self.close,
            bg=chrome_bg,
            fg=chrome_fg,
            bd=0,
            width=4,
        )
        self.close_button.pack(side=tk.RIGHT, fill=tk.Y)

        self.minimize_button = FlatButton(
            self.titlebar,
            text="_",
            command=self.minimize,
            bg=chrome_bg,
            fg=chrome_fg,
            bd=0,
            width=4,
        )
        self.minimize_button.pack(side=tk.RIGHT, fill=tk.Y)

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

    def _canvas_size(self):
        """Return the usable internal desktop dimensions."""
        self.canvas.update_idletasks()
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 1:
            width = max(1, self.desktop.root.winfo_width())
        if height <= 1:
            height = max(1, self.desktop.root.winfo_height() - 90)
        return width, height

    def constrain_to_desktop(self):
        """Resize and reposition the window so all controls remain reachable."""
        if not self.canvas.type(self.window_id):
            return
        canvas_width, canvas_height = self._canvas_size()
        self.width = min(self.width, canvas_width)
        self.height = min(self.height, canvas_height)
        coordinates = self.canvas.coords(self.window_id)
        if len(coordinates) < 2:
            return
        x = max(0, min(coordinates[0], canvas_width - self.width))
        y = max(0, min(coordinates[1], canvas_height - self.height))
        self.canvas.coords(self.window_id, x, y)
        self.canvas.itemconfigure(self.window_id, width=self.width, height=self.height)

    def start_drag(self, event):
        self._drag_start = (event.x_root, event.y_root, *self.canvas.coords(self.window_id))
        self.canvas.tag_raise(self.window_id)

    def drag(self, event):
        if not self._drag_start:
            return
        start_x, start_y, window_x, window_y = self._drag_start
        canvas_width, canvas_height = self._canvas_size()
        new_x = max(0, min(window_x + event.x_root - start_x, canvas_width - self.width))
        new_y = max(0, min(window_y + event.y_root - start_y, canvas_height - self.height))
        self.canvas.coords(
            self.window_id,
            new_x,
            new_y,
        )

    def start_resize(self, event):
        self._resize_start = (event.x_root, event.y_root, self.width, self.height)
        self.canvas.tag_raise(self.window_id)

    def resize(self, event):
        if not self._resize_start:
            return
        start_x, start_y, start_width, start_height = self._resize_start
        canvas_width, canvas_height = self._canvas_size()
        coordinates = self.canvas.coords(self.window_id)
        window_x, window_y = coordinates[:2]
        maximum_width = max(1, canvas_width - window_x)
        maximum_height = max(1, canvas_height - window_y)
        minimum_width = min(self.min_width, maximum_width)
        minimum_height = min(self.min_height, maximum_height)
        new_width = min(max(minimum_width, start_width + event.x_root - start_x), maximum_width)
        new_height = min(max(minimum_height, start_height + event.y_root - start_y), maximum_height)
        self.width = new_width
        self.height = new_height
        self.canvas.itemconfigure(self.window_id, width=new_width, height=new_height)

    def close(self):
        self.desktop.taskbar.remove_minimized_window(self)
        self.canvas.delete(self.window_id)
        self.frame.destroy()
        if self in self.desktop.windows:
            self.desktop.windows.remove(self)

    def minimize(self):
        if self.minimized:
            return
        self.minimized = True
        self.canvas.itemconfigure(self.window_id, state=tk.HIDDEN)
        self.desktop.taskbar.add_minimized_window(self)

    def restore(self):
        self.constrain_to_desktop()
        if not self.minimized:
            self.canvas.tag_raise(self.window_id)
            return
        self.minimized = False
        self.canvas.itemconfigure(self.window_id, state=tk.NORMAL)
        self.canvas.tag_raise(self.window_id)
        self.desktop.taskbar.remove_minimized_window(self)


class OllamaClient:
    """Minimal stdlib client for a local Ollama server (pyAI backend)."""

    def __init__(self, base_url="http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    def _request(self, path, payload=None, timeout=10):
        url = f"{self.base_url}{path}"
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return urllib.request.urlopen(
            urllib.request.Request(url, data=data, headers=headers), timeout=timeout
        )

    def list_models(self):
        """Return installed model names; doubles as the server liveness probe."""
        with self._request("/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [model.get("name", "") for model in payload.get("models", [])]

    def has_model(self, name):
        wanted = name if ":" in name else f"{name}:"
        for installed in self.list_models():
            if installed == name or installed.startswith(wanted):
                return True
        return False

    def pull(self, name, on_progress, cancel_event):
        """Download a model, reporting NDJSON progress lines via on_progress."""
        # Current Ollama expects "model"; very old servers used "name".
        with self._request("/api/pull", {"model": name, "stream": True}, timeout=60) as response:
            for line in response:
                if cancel_event.is_set():
                    return False
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise OSError(chunk["error"])
                on_progress(chunk.get("status", ""), chunk.get("completed"), chunk.get("total"))
                if chunk.get("status") == "success":
                    return True
        return True

    def chat(self, name, messages, on_token, cancel_event):
        """Stream a chat completion; on_token(text, done) per NDJSON line.

        The 300s timeout is per socket read: a cold model load on CPU can take
        more than a minute before the first token arrives.
        """
        payload = {"model": name, "messages": messages, "stream": True}
        with self._request("/api/chat", payload, timeout=300) as response:
            for line in response:
                if cancel_event.is_set():
                    return
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise OSError(chunk["error"])
                on_token(chunk.get("message", {}).get("content", ""), bool(chunk.get("done")))
                if chunk.get("done"):
                    return

    @staticmethod
    def binary_installed():
        """Detect an Ollama install even before PATH picks it up (fresh winget/brew)."""
        if shutil.which("ollama"):
            return True
        candidates = []
        if sys.platform == "darwin":
            candidates.append(Path("/Applications/Ollama.app"))
            candidates.append(Path.home() / "Applications" / "Ollama.app")
        elif os.name == "nt":
            local_appdata = os.environ.get("LOCALAPPDATA", "")
            if local_appdata:
                candidates.append(Path(local_appdata) / "Programs" / "Ollama" / "ollama.exe")
        else:
            candidates.append(Path("/usr/local/bin/ollama"))
            candidates.append(Path("/usr/bin/ollama"))
        return any(candidate.exists() for candidate in candidates)


class DesktopGUI:
    """Windows-like desktop GUI"""
    def __init__(self, root):
        self.root = root
        self.username = None
        self.messenger_service = None
        self.messenger_window = None
        self.messenger_events = queue.Queue()
        self.messenger_ui_handler = None
        self.notification_toasts = []
        self.tip_after = None
        self.tip_index = 0
        self._notifications_started = False
        self.settings_path = get_gui_settings_path()
        self.virtual_drives_path = self.settings_path.with_name("virtual_drives.json")
        self.preferences = self.load_preferences()
        self.windows = []
        self.custom_app_icons = []
        self.root.title("Python OS Desktop")
        self.root.geometry("1280x720")
        self.root.configure(bg="white")

        self.root.option_add("*Font", (self.preferences["font_family"], self.preferences["font_size"]))
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

        self.create_system_bar()
        
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
        self.root.bind("<Destroy>", self._shutdown_messenger, add="+")
        self.root.after(200, self._poll_messenger_events)

    def lock_desktop(self):
        """Block all desktop interaction until valid credentials are supplied."""
        self.system_user_var.set("Locked")
        self.username = authenticate(self.root, cancellable=False)
        self.system_user_var.set(self.username or "Locked")
        return self.username

    def create_system_bar(self):
        """Create the persistent top bar and its system menus."""
        chrome = self.preferences.get("chrome_bg", "#000000")
        chrome_text = self.preferences.get("chrome_fg", "#ffffff")
        self.system_bar = tk.Frame(self.root, bg=chrome, height=32, relief=tk.RAISED, bd=1)
        self.system_bar.pack(side=tk.TOP, fill=tk.X)
        self.system_bar.pack_propagate(False)
        self.system_menu_buttons = []

        def add_menu(label, postcommand=None):
            button = FlatButton(
                self.system_bar, text=label, bg=chrome, fg=chrome_text,
                activebackground=self.preferences.get("surface_bg", "#ffffff"),
                activeforeground=self.preferences.get("text_fg", "#000000"),
                relief=tk.FLAT, padx=10,
            )
            menu = tk.Menu(button, tearoff=0, postcommand=postcommand)
            def post_menu(owner=button, target=menu):
                target.post(owner.winfo_rootx(), owner.winfo_rooty() + owner.winfo_height())
            button.configure(command=post_menu)
            button._system_menu = menu
            button.pack(side=tk.LEFT, fill=tk.Y)
            self.system_menu_buttons.append(button)
            return menu

        system_menu = add_menu("pyOS")
        system_menu.add_command(label="About pyOS", command=self.show_about)
        system_menu.add_separator()
        system_menu.add_command(label="Lock Desktop", command=self.lock_desktop)
        system_menu.add_separator()
        system_menu.add_command(label="Run Setup...", command=self.run_setup)
        system_menu.add_command(label="Restart pyOS...", command=self.restart_pyos)
        system_menu.add_command(label="Restart and Run Setup...", command=self.restart_and_setup)
        system_menu.add_separator()
        system_menu.add_command(label="Shut Down pyOS...", command=self.shutdown_pyos)

        applications_menu = add_menu("Applications")
        applications_menu.add_command(label="File Manager", command=self.open_default_file_manager)
        applications_menu.add_command(label="Text Editor", command=self.open_text_editor)
        applications_menu.add_command(label="Internet Browser", command=self.open_browser)
        applications_menu.add_command(label="Messenger", command=self.open_messenger)
        applications_menu.add_command(label="Calculator", command=self.open_calculator)
        applications_menu.add_command(label="Games Suite", command=self.open_games_suite)
        applications_menu.add_separator()
        applications_menu.add_command(label="Settings", command=self.open_settings)

        self.window_menu = add_menu("Window", self.rebuild_window_menu)

        help_menu = add_menu("Help")
        help_menu.add_command(label="Show a Tip", command=self.show_tip_now)
        help_menu.add_command(label="About pyOS", command=self.show_about)

        self.system_user_var = tk.StringVar(value="Locked")
        self.system_user_label = tk.Label(
            self.system_bar, textvariable=self.system_user_var, bg=chrome, fg=chrome_text,
            font=("Courier New", 9, "bold"), padx=10,
        )
        self.system_user_label.pack(side=tk.RIGHT, fill=tk.Y)

    def rebuild_window_menu(self):
        self.window_menu.delete(0, tk.END)
        self.window_menu.add_command(label="Minimize All", command=self.minimize_all_windows)
        self.window_menu.add_command(label="Restore All", command=self.restore_all_windows)
        self.window_menu.add_separator()
        if not self.windows:
            self.window_menu.add_command(label="No Open Windows", state=tk.DISABLED)
            return
        for window in self.windows:
            label = ("Restore " if window.minimized else "Show ") + window.title
            self.window_menu.add_command(label=label, command=window.restore)

    def minimize_all_windows(self):
        for window in list(self.windows):
            window.minimize()

    def restore_all_windows(self):
        for window in list(self.windows):
            window.restore()

    def show_tip_now(self):
        tips = (
            "Use the _ button to minimize apps into the taskbar.",
            "Open the pyOS menu for lock, restart, setup, and shutdown controls.",
            "The Window menu can minimize or restore every open pyOS app.",
        )
        self.show_notification("pyOS Tip", random.choice(tips), kind="system", duration=8000)

    def _stop_services(self):
        if self.messenger_service:
            self.messenger_service.stop()

    def shutdown_pyos(self):
        if not messagebox.askyesno(
            "Shut Down pyOS", "Close pyOS and all open pyOS applications?",
            parent=self.root,
        ):
            return
        self._stop_services()
        self.root.destroy()

    def uninstall_pyos(self):
        """Remove pyOS user data and virtual drives while retaining the installation."""
        warning = (
            "This permanently removes:\n\n"
            "• Your pyOS account, passkeys, settings, and custom apps\n"
            "• Drive A, Drive B, and every registered custom virtual drive\n"
            "• Other files in the dedicated pyOS data directory\n"
            "• The pyOS installation configuration\n\n"
            "The pyOS program, virtual environment, modules, and installed packages remain. "
            "Files in your normal Downloads directory are not removed. This cannot be undone."
        )
        if not messagebox.askyesno("Uninstall pyOS", warning, icon=messagebox.WARNING, parent=self.root):
            return
        entered_password = simpledialog.askstring(
            "Uninstall pyOS", "Enter your current pyOS password:", show="*", parent=self.root,
        )
        if entered_password is None:
            return
        username = get_username()
        if username is None or not verify_credentials(username, entered_password):
            messagebox.showerror("Uninstall pyOS", "The password is incorrect.", parent=self.root)
            return
        confirmation = simpledialog.askstring(
            "Confirm Uninstall",
            'Type UNINSTALL to permanently remove all pyOS data:',
            parent=self.root,
        )
        if confirmation != "UNINSTALL":
            messagebox.showinfo("Uninstall pyOS", "Uninstall cancelled.", parent=self.root)
            return

        config = load_config()
        install_dir = Path(config["install_dir"]).expanduser().resolve()
        data_dir = Path(config["data_dir"]).expanduser().resolve()
        downloads_dir = Path(config["downloads_dir"]).expanduser().resolve()
        home = Path.home().resolve()
        failures = []

        def is_dangerous_directory(path):
            path = path.resolve()
            if path == Path(path.anchor) or path in {home, install_dir, downloads_dir}:
                return True
            return any(protected.is_relative_to(path) for protected in (home, install_dir, downloads_dir))

        def remove_path(path, allow_directory=True):
            path = Path(path).expanduser()
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir() and allow_directory:
                    if is_dangerous_directory(path):
                        raise OSError(f"refused unsafe directory target: {path}")
                    shutil.rmtree(path)
            except OSError as error:
                failures.append(f"{path}: {error}")

        registered_drives = self._load_virtual_drives()
        drive_paths = []
        for drive in registered_drives:
            raw_path = drive.get("path")
            if isinstance(raw_path, str) and raw_path.strip():
                try:
                    drive_paths.append(Path(raw_path).expanduser().resolve())
                except OSError as error:
                    failures.append(f"{raw_path}: {error}")
        for path in sorted(set(drive_paths), key=lambda item: len(item.parts), reverse=True):
            remove_path(path)

        remove_path(self.get_drive_a_path())
        remove_path(get_drive_b_dir(create=False))

        data_is_dedicated = (
            data_dir not in {home, install_dir, downloads_dir, Path(data_dir.anchor)}
            and not any(protected.is_relative_to(data_dir) for protected in (home, install_dir, downloads_dir))
        )
        if config.get("configured") and data_is_dedicated:
            remove_path(data_dir)
        else:
            known_data = {
                self.settings_path,
                self.virtual_drives_path,
                get_cli_settings_path(),
                credentials_path(),
                self.settings_path.with_name("email_settings.json"),
                self.settings_path.parent / "apps",
                data_dir / "Drive_B",
            }
            for path in known_data:
                remove_path(path)

        # Standalone-mode files and mod backups can exist outside the configured data directory.
        for path in (
            home / ".pyos_gui_settings.json",
            home / ".pyOS_settings.json",
            home / ".pyos_credentials.json",
            Path(__file__).resolve().parent / ".pyos_mod_backups",
            CONFIG_FILE,
        ):
            remove_path(path)

        self._stop_services()
        if failures:
            preview = "\n".join(failures[:8])
            if len(failures) > 8:
                preview += f"\n...and {len(failures) - 8} more"
            messagebox.showwarning(
                "Uninstall pyOS",
                "pyOS removed all accessible data, but some items could not be deleted:\n\n" + preview,
                parent=self.root,
            )
        else:
            messagebox.showinfo(
                "Uninstall pyOS",
                "All pyOS data and registered virtual drives were removed. "
                "The program, modules, and packages remain installed.",
                parent=self.root,
            )
        self.root.destroy()

    def restart_pyos(self):
        if not messagebox.askyesno(
            "Restart pyOS", "Close and restart pyOS now?", parent=self.root
        ):
            return
        try:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
        except OSError as error:
            messagebox.showerror("Restart pyOS", f"Could not restart pyOS: {error}", parent=self.root)
            return
        self._stop_services()
        self.root.destroy()

    def run_setup(self):
        setup_path = Path(__file__).resolve().with_name("setup.py")
        if not setup_path.is_file():
            messagebox.showerror("pyOS Setup", f"Setup was not found at:\n{setup_path}", parent=self.root)
            return
        try:
            subprocess.Popen([sys.executable, str(setup_path)])
            self.show_notification("pyOS System", "Setup opened in a separate window.")
        except OSError as error:
            messagebox.showerror("pyOS Setup", f"Could not open setup: {error}", parent=self.root)

    def restart_and_setup(self):
        if not messagebox.askyesno(
            "Restart and Run Setup",
            "Close pyOS, run setup, then launch pyOS again after setup closes?",
            parent=self.root,
        ):
            return
        setup_path = Path(__file__).resolve().with_name("setup.py")
        if not setup_path.is_file():
            messagebox.showerror("pyOS Setup", f"Setup was not found at:\n{setup_path}", parent=self.root)
            return
        helper = (
            "import subprocess,sys; "
            "subprocess.run([sys.argv[1],sys.argv[2]]); "
            "subprocess.Popen([sys.argv[1],sys.argv[3]])"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            subprocess.Popen(
                [sys.executable, "-c", helper, sys.executable, str(setup_path),
                 str(Path(__file__).resolve())],
                creationflags=creationflags,
            )
        except OSError as error:
            messagebox.showerror("pyOS Setup", f"Could not start setup: {error}", parent=self.root)
            return
        self._stop_services()
        self.root.destroy()

    def _shutdown_messenger(self, event):
        if event.widget is self.root and self.messenger_service:
            self.messenger_service.stop()

    def _poll_messenger_events(self):
        """Dispatch network events even while the Messenger window is closed."""
        try:
            while True:
                event, payload = self.messenger_events.get_nowait()
                handler = self.messenger_ui_handler
                if handler:
                    handler(event, payload)
                elif event == "message" and payload.get("direction") == "incoming":
                    summary = (payload.get("text") or f"Image: {payload.get('filename', 'image')}")[:90]
                    self.show_notification(
                        "Messenger", f"{payload.get('sender', 'Unknown')}: {summary}", kind="system"
                    )
                elif event == "error":
                    self.show_notification("Messenger Error", str(payload), kind="system")
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(200, self._poll_messenger_events)

    def show_notification(self, title, message, kind="system", duration=6000):
        """Display a non-blocking desktop toast when allowed by preferences."""
        if not self.preferences.get("notifications_enabled", True):
            return
        if kind == "tip" and not self.preferences.get("tips_enabled", True):
            return
        chrome = self.preferences.get("chrome_bg", "#000000")
        chrome_text = self.preferences.get("chrome_fg", "#ffffff")
        surface = self.preferences.get("surface_bg", "#ffffff")
        text = self.preferences.get("text_fg", "#000000")
        frame = tk.Frame(self.root, bg=surface, relief=tk.RAISED, bd=3)
        header = tk.Frame(frame, bg=chrome)
        header.pack(fill=tk.X)
        tk.Label(header, text=title, bg=chrome, fg=chrome_text,
                 font=("Courier New", 9, "bold"), anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=7, pady=3
        )
        tk.Button(header, text="X", command=lambda: self.dismiss_notification(frame),
                  bg=chrome, fg=chrome_text, bd=0, width=3).pack(side=tk.RIGHT, fill=tk.Y)
        tk.Label(frame, text=message, bg=surface, fg=text, justify=tk.LEFT,
                 anchor=tk.NW, wraplength=310).pack(fill=tk.BOTH, expand=True, padx=8, pady=7)
        record = {"frame": frame, "kind": kind, "after": None}
        self.notification_toasts.append(record)
        record["after"] = self.root.after(duration, lambda: self.dismiss_notification(frame))
        self._position_notifications()
        frame.lift()

    def dismiss_notification(self, frame):
        for record in list(self.notification_toasts):
            if record["frame"] is frame:
                if record["after"] is not None:
                    try:
                        self.root.after_cancel(record["after"])
                    except tk.TclError:
                        pass
                self.notification_toasts.remove(record)
                if frame.winfo_exists():
                    frame.destroy()
                break
        self._position_notifications()

    def _position_notifications(self):
        for index, record in enumerate(reversed(self.notification_toasts)):
            record["frame"].place(
                relx=1.0, rely=1.0, x=-12, y=-(76 + index * 98),
                width=340, height=90, anchor=tk.SE,
            )
            record["frame"].lift()

    def start_notifications(self):
        if self._notifications_started:
            return
        self._notifications_started = True
        if self.preferences.get("notifications_enabled", True):
            self.root.after(
                500,
                lambda: self.show_notification(
                    "pyOS System", f"Welcome, {self.username or 'user'}. pyOS is ready."
                ),
            )
        self._schedule_next_tip(initial=True)

    def _schedule_next_tip(self, initial=False):
        if self.tip_after is not None:
            try:
                self.root.after_cancel(self.tip_after)
            except tk.TclError:
                pass
            self.tip_after = None
        if not (self.preferences.get("notifications_enabled", True)
                and self.preferences.get("tips_enabled", True)):
            return
        delay = 7000 if initial else 90000
        self.tip_after = self.root.after(delay, self._show_next_tip)

    def _show_next_tip(self):
        self.tip_after = None
        tips = (
            "Use the _ button to minimize an app. Restore it from the taskbar.",
            "Drag an app's title bar to move it around the desktop.",
            "The Calculator graph can be panned with the mouse and zoomed with the wheel.",
            "Right-click the desktop for quick access to applications and Lock Desktop.",
            "Settings controls appearance, notifications, security, files, and the clock.",
        )
        self.show_notification("pyOS Tip", tips[self.tip_index % len(tips)], kind="tip", duration=8000)
        self.tip_index += 1
        self._schedule_next_tip()

    def apply_notification_preferences(self):
        if not self.preferences.get("notifications_enabled", True):
            for record in list(self.notification_toasts):
                self.dismiss_notification(record["frame"])
        elif not self.preferences.get("tips_enabled", True):
            for record in list(self.notification_toasts):
                if record["kind"] == "tip":
                    self.dismiss_notification(record["frame"])
        if self._notifications_started:
            self._schedule_next_tip(initial=True)

    def resize_icon_layer(self, event):
        """Keep the draggable launcher area fitted to the desktop canvas."""
        width = max(1, event.width - 10)
        height = max(1, event.height - 10)
        self.desktop_canvas.itemconfigure(self.icon_layer_id, width=width, height=height)
        for window in list(self.windows):
            window.constrain_to_desktop()
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
            "font_family": "Courier New",
            "clock_24h": True,
            "show_seconds": True,
            "show_hidden_files": False,
            "file_manager_start": "Home",
            "taskbar_shortcuts": [],
            "icon_positions": {},
            "notifications_enabled": True,
            "tips_enabled": True,
            "ai_chat_model": "llama3.2",
            "ai_chat_url": "http://localhost:11434",
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
        if not isinstance(defaults.get("font_family"), str) or not defaults["font_family"].strip():
            defaults["font_family"] = "Courier New"
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
        if not isinstance(defaults["icon_positions"], dict):
            defaults["icon_positions"] = {}
        valid_positions = {}
        for key, position in defaults["icon_positions"].items():
            if not isinstance(key, str) or not isinstance(position, dict):
                continue
            try:
                x = max(0, min(10000, int(position["x"])))
                y = max(0, min(10000, int(position["y"])))
            except (KeyError, TypeError, ValueError):
                continue
            valid_positions[key[:260]] = {"x": x, "y": y}
        defaults["icon_positions"] = valid_positions
        defaults["notifications_enabled"] = bool(defaults["notifications_enabled"])
        defaults["tips_enabled"] = bool(defaults["tips_enabled"])
        if not isinstance(defaults.get("ai_chat_model"), str) or not defaults["ai_chat_model"].strip():
            defaults["ai_chat_model"] = "llama3.2"
        if not isinstance(defaults.get("ai_chat_url"), str) or not defaults["ai_chat_url"].startswith("http"):
            defaults["ai_chat_url"] = "http://localhost:11434"
        return defaults

    def save_preferences(self):
        """Persist desktop preferences for the next launch."""
        try:
            self.settings_path.write_text(json.dumps(self.preferences, indent=2), encoding="utf-8")
        except OSError as error:
            messagebox.showerror("Settings", f"Could not save settings: {error}")

    def _apply_widget_fonts(self, widget):
        """Apply the selected family while retaining intentional text styling."""
        if getattr(widget, "_skip_font_apply", False):
            return
        try:
            existing = tkfont.Font(root=self.root, font=widget.cget("font"))
            preserve_size = widget.winfo_class() in {"Text", "ScrolledText"} or getattr(widget, "_keep_font", False)
            size = existing.cget("size") if preserve_size else self.preferences["font_size"]
            font_spec = [self.preferences["font_family"], size]
            if existing.cget("weight") == "bold":
                font_spec.append("bold")
            if existing.cget("slant") == "italic":
                font_spec.append("italic")
            if existing.cget("underline"):
                font_spec.append("underline")
            if existing.cget("overstrike"):
                font_spec.append("overstrike")
            widget.configure(font=tuple(font_spec))
        except tk.TclError:
            pass
        try:
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            self._apply_widget_fonts(child)

    def apply_preferences(self):
        """Apply preferences that can be updated while the desktop is running."""
        desktop_bg = self.preferences["desktop_bg"]
        surface_bg = self.preferences["surface_bg"]
        text_fg = self.preferences["text_fg"]
        chrome_bg = self.preferences["chrome_bg"]
        chrome_fg = self.preferences["chrome_fg"]
        self.root.configure(bg=desktop_bg)
        self.system_bar.configure(bg=chrome_bg)
        self.system_user_label.configure(bg=chrome_bg, fg=chrome_fg)
        for button in self.system_menu_buttons:
            button.configure(
                bg=chrome_bg, fg=chrome_fg,
                activebackground=surface_bg, activeforeground=text_fg,
            )
            try:
                button._system_menu.configure(
                    bg=surface_bg, fg=text_fg,
                    activebackground=chrome_bg, activeforeground=chrome_fg,
                )
            except tk.TclError:
                pass
        self.desktop_canvas.configure(bg=desktop_bg)
        self.icon_container.configure(bg=desktop_bg)
        for launcher in self.icon_container.winfo_children():
            if launcher is self.background_label:
                continue
            desktop_icon = getattr(launcher, "_desktop_icon", None)
            if desktop_icon is not None:
                desktop_icon.set_colors(surface_bg, text_fg)
                continue
            launcher.configure(bg=surface_bg)
        self.taskbar.apply_colors(chrome_bg, chrome_fg)
        for window in self.windows:
            window.frame.configure(bg=surface_bg)
            window.content.configure(bg=surface_bg)
            window.titlebar.configure(bg=chrome_bg)
            window.title_label.configure(bg=chrome_bg, fg=chrome_fg)
            window.close_button.configure(bg=chrome_bg, fg=chrome_fg)
            window.minimize_button.configure(bg=chrome_bg, fg=chrome_fg)
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

        family = self.preferences["font_family"]
        font = (family, self.preferences["font_size"])
        self.root.option_add("*Font", font)
        style.configure(".", font=font)

        self._apply_widget_fonts(self.root)

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

    def _register_desktop_icon(self, icon, key):
        """Restore a launcher without allowing startup-time icon collisions."""
        placed_icons = getattr(self, "_placed_desktop_icons", [])
        # Custom launchers may have been destroyed during a refresh.
        active_icons = []
        for existing in placed_icons:
            try:
                if existing.frame.winfo_exists():
                    active_icons.append(existing)
            except tk.TclError:
                pass
        self._placed_desktop_icons = active_icons

        position = self.preferences["icon_positions"].get(key)
        if position:
            candidate_x, candidate_y = position["x"], position["y"]
        else:
            candidate_x, candidate_y = icon.x, icon.y

        def overlaps(x, y):
            padding = 4
            return any(
                x < existing.x + existing.width + padding
                and x + icon.width + padding > existing.x
                and y < existing.y + existing.height + padding
                and y + icon.height + padding > existing.y
                for existing in self._placed_desktop_icons
            )

        moved_to_free_cell = False
        if overlaps(candidate_x, candidate_y):
            available_width = max(
                icon.width, self.icon_container.winfo_width(), self.icon_container.winfo_reqwidth()
            )
            columns = max(1, (available_width - DesktopIcon.GRID_MARGIN) // DesktopIcon.GRID_X)
            slot = 0
            while True:
                x = DesktopIcon.GRID_MARGIN + (slot % columns) * DesktopIcon.GRID_X
                y = DesktopIcon.GRID_MARGIN + (slot // columns) * DesktopIcon.GRID_Y
                if not overlaps(x, y):
                    candidate_x, candidate_y = x, y
                    moved_to_free_cell = True
                    break
                slot += 1

        if position or moved_to_free_cell:
            icon.restore_position(candidate_x, candidate_y)
        if moved_to_free_cell:
            self.preferences["icon_positions"][key] = {
                "x": int(candidate_x), "y": int(candidate_y),
            }
            self.save_preferences()
        self._placed_desktop_icons.append(icon)

        def remember(x, y):
            self.preferences["icon_positions"][key] = {"x": int(x), "y": int(y)}
            self.save_preferences()

        icon.position_changed = remember
    
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

        self.calculator_icon = DesktopIcon(
            self.icon_container,
            "Calculator",
            "calculator",
            self.open_calculator,
            120, 84
        )

        self.messenger_icon = DesktopIcon(
            self.icon_container,
            "Messenger",
            "messenger",
            self.open_messenger,
            230, 84
        )

        self.games_icon = DesktopIcon(
            self.icon_container,
            "Games Suite",
            "games",
            self.open_games_suite,
            340, 84
        )

        self.modding_icon = DesktopIcon(
            self.icon_container,
            "Modding Environment",
            "python_ide",
            self.open_modding_environment,
            450, 84
        )

        self.virtual_drives_icon = DesktopIcon(
            self.icon_container,
            "Virtual Drives",
            "drive",
            self.open_virtual_drive_manager,
            560, 84
        )

        self.weather_icon = DesktopIcon(
            self.icon_container,
            "Weather",
            "info",
            self.open_weather,
            670, 84
        )

        self.news_icon = DesktopIcon(
            self.icon_container,
            "News",
            "browser",
            self.open_news,
            780, 84
        )
        built_in_icons = (
            ("terminal", self.terminal_icon),
            ("files", self.file_manager_icon),
            ("drive-a", self.drive_a_icon),
            ("drive-b", self.drive_b_icon),
            ("settings", self.settings_icon),
            ("about", self.about_icon),
            ("editor", self.text_editor_icon),
            ("browser", self.browser_icon),
            ("ide", self.python_ide_icon),
            ("media", self.media_player_icon),
            ("notepad", self.notepad_icon),
            ("images", self.image_viewer_icon),
            ("calculator", self.calculator_icon),
            ("messenger", self.messenger_icon),
            ("games", self.games_icon),
            ("modding", self.modding_icon),
            ("virtual-drives", self.virtual_drives_icon),
            ("weather", self.weather_icon),
            ("news", self.news_icon),
        )
        for key, icon in built_in_icons:
            self._register_desktop_icon(icon, "builtin:" + key)

        self.dispenser_icon = DesktopIcon(
            self.icon_container,
            "Dispenser",
            "dispenser",
            self.open_dispenser,
            890, 84
        )

        self.ai_chat_icon = DesktopIcon(
            self.icon_container,
            "pyAI",
            "ai_chat",
            self.open_ai_chat,
            1000, 84
        )
        self.refresh_custom_app_icons()

    def _custom_app_name(self, path):
        """Read a custom app's declared name without executing its code."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == "APP_NAME" for target in targets):
                    value = ast.literal_eval(node.value)
                    if isinstance(value, str) and value.strip():
                        return value.strip()[:80]
        except (OSError, SyntaxError, ValueError, TypeError):
            pass
        return path.stem.replace("_", " ").title()

    def _launch_custom_app(self, path):
        try:
            self.run_custom_app(path)
        except Exception as error:
            messagebox.showerror(
                "Custom App", f"{Path(path).name} could not run:\n\n{error}", parent=self.root,
            )

    def refresh_custom_app_icons(self):
        """Synchronize App Maker files with launchers on the desktop."""
        for icon in self.custom_app_icons:
            try:
                icon.frame.destroy()
            except tk.TclError:
                pass
        self.custom_app_icons.clear()
        apps = sorted(self._custom_apps_directory().glob("*.py"), key=lambda item: item.name.casefold())
        available_width = max(
            120, self.icon_container.winfo_width(), self.icon_container.winfo_reqwidth()
        )
        columns = max(1, min(11, (available_width - 10) // 110))
        first_slot = 20
        for offset, path in enumerate(apps):
            slot = first_slot + offset
            x = 10 + (slot % columns) * 110
            y = 10 + (slot // columns) * 74
            icon = DesktopIcon(
                self.icon_container,
                self._custom_app_name(path),
                "python_ide",
                lambda target=path: self._launch_custom_app(target),
                x,
                y,
            )
            icon.set_colors(self.preferences["surface_bg"], self.preferences["text_fg"])
            self._register_desktop_icon(icon, "custom:" + path.name.casefold())
            self.custom_app_icons.append(icon)
    
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
        self.root.after_idle(lambda target=window.frame: self._apply_widget_fonts(target))
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

    def _load_virtual_drives(self):
        try:
            drives = json.loads(self.virtual_drives_path.read_text(encoding="utf-8"))
            if isinstance(drives, list):
                return [drive for drive in drives if isinstance(drive, dict)]
        except (OSError, ValueError, TypeError):
            pass
        return []

    def _save_virtual_drives(self, drives):
        self.virtual_drives_path.parent.mkdir(parents=True, exist_ok=True)
        self.virtual_drives_path.write_text(json.dumps(drives, indent=2), encoding="utf-8")

    def open_virtual_drive_manager(self):
        """Create and manage additional directory-backed virtual drives."""
        window = self.create_window("Virtual Drive Manager", width=720, height=500)
        drives = self._load_virtual_drives()
        surface = self.preferences["surface_bg"]
        status = tk.StringVar(value="Select a drive or create a new one.")

        list_frame = tk.Frame(window.content, bg=surface)
        list_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)
        drive_list = tk.Listbox(list_frame, width=25, height=20)
        drive_list.pack(fill=tk.BOTH, expand=True)

        form = tk.Frame(window.content, bg=surface)
        form.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10), pady=10)
        name = tk.StringVar()
        location = tk.StringVar(value=str(Path.home() / "pyOS_Virtual_Drives"))
        quota = tk.StringVar(value="1024")
        storage = tk.StringVar(value="persistent")
        read_only = tk.BooleanVar(value=False)

        def row(label, variable, browse=False):
            tk.Label(form, text=label, bg=surface, anchor=tk.W).pack(fill=tk.X, pady=(5, 1))
            holder = tk.Frame(form, bg=surface)
            holder.pack(fill=tk.X)
            tk.Entry(holder, textvariable=variable).pack(side=tk.LEFT, fill=tk.X, expand=True)
            if browse:
                def choose():
                    selected = filedialog.askdirectory(parent=self.root, initialdir=variable.get() or str(Path.home()))
                    if selected:
                        variable.set(selected)
                tk.Button(holder, text="Browse", command=choose).pack(side=tk.LEFT, padx=(5, 0))

        row("Drive name", name)
        row("Parent location", location, True)
        row("Quota (MB, configuration metadata)", quota)
        tk.Label(form, text="Storage mode", bg=surface, anchor=tk.W).pack(fill=tk.X, pady=(8, 1))
        ttk.Combobox(form, textvariable=storage, values=("persistent", "temporary"), state="readonly").pack(fill=tk.X)
        tk.Checkbutton(form, text="Read-only", variable=read_only, bg=surface).pack(anchor=tk.W, pady=6)

        def refresh():
            drive_list.delete(0, tk.END)
            for drive in drives:
                drive_list.insert(tk.END, drive.get("name", "Unnamed"))

        def selected_index():
            selection = drive_list.curselection()
            return selection[0] if selection else None

        def create_drive():
            drive_name = name.get().strip()
            if not drive_name or any(character in drive_name for character in '<>:"/\\|?*'):
                messagebox.showerror("Virtual Drives", "Enter a valid drive name.", parent=self.root)
                return
            if any(drive.get("name", "").casefold() == drive_name.casefold() for drive in drives):
                messagebox.showerror("Virtual Drives", "A drive with that name already exists.", parent=self.root)
                return
            try:
                quota_mb = max(1, int(quota.get()))
                parent = Path(location.get()).expanduser().resolve()
                path = parent / drive_name
                path.mkdir(parents=True, exist_ok=False)
                drive = {"name": drive_name, "path": str(path), "quota_mb": quota_mb,
                         "storage": storage.get(), "read_only": bool(read_only.get()),
                         "created": datetime.now().isoformat(timespec="seconds")}
                drives.append(drive)
                self._save_virtual_drives(drives)
            except FileExistsError:
                messagebox.showerror("Virtual Drives", "That directory already exists.", parent=self.root)
                return
            except (OSError, ValueError) as error:
                messagebox.showerror("Virtual Drives", f"Could not create drive: {error}", parent=self.root)
                return
            refresh()
            status.set(f"Created {drive_name} at {path}")

        def open_drive():
            index = selected_index()
            if index is not None:
                path = Path(drives[index].get("path", ""))
                path.mkdir(parents=True, exist_ok=True)
                self.open_file_manager(path)

        def remove_drive():
            index = selected_index()
            if index is None:
                return
            removed = drives.pop(index)
            self._save_virtual_drives(drives)
            refresh()
            status.set(f"Unregistered {removed.get('name')}; its files were not deleted.")

        buttons = tk.Frame(form, bg=surface)
        buttons.pack(fill=tk.X, pady=10)
        tk.Button(buttons, text="Create", command=create_drive).pack(side=tk.LEFT)
        tk.Button(buttons, text="Open", command=open_drive).pack(side=tk.LEFT, padx=5)
        tk.Button(buttons, text="Unregister", command=remove_drive).pack(side=tk.LEFT)
        tk.Label(form, textvariable=status, bg=surface, justify=tk.LEFT, wraplength=420).pack(fill=tk.X)
        drive_list.bind("<Double-Button-1>", lambda event: open_drive())
        refresh()

    def open_modding_environment(self):
        """Edit pyOS modules and settings with backups and syntax validation."""
        window = self.create_window("Modding Environment", width=900, height=570)
        project = Path(__file__).resolve().parent
        excluded_parts = {
            ".git", ".idea", ".pyos_mod_backups", ".backups", "__pycache__",
            ".venv", "venv", "env", "site-packages",
        }
        project_modules = [
            path for path in project.rglob("*.py")
            if not any(part.casefold() in excluded_parts for part in path.relative_to(project).parts)
        ]
        apps_directory = self._custom_apps_directory()
        custom_apps = list(apps_directory.glob("*.py"))
        candidates = sorted(
            dict.fromkeys(path.resolve() for path in project_modules + custom_apps),
            key=lambda path: str(path).casefold(),
        )
        if self.settings_path.exists():
            candidates.append(self.settings_path.resolve())
        current = {"path": None}
        status = tk.StringVar(value="Choose a pyOS module to begin.")
        sidebar = tk.Frame(window.content, bg=self.preferences["surface_bg"], width=190)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=6, pady=6)
        files = tk.Listbox(sidebar, width=25)
        files.pack(fill=tk.BOTH, expand=True)
        def candidate_label(path):
            try:
                return str(path.relative_to(project))
            except ValueError:
                try:
                    return "Apps / " + str(path.relative_to(apps_directory))
                except ValueError:
                    return path.name

        for path in candidates:
            files.insert(tk.END, candidate_label(path))
        workspace = tk.Frame(window.content, bg=self.preferences["surface_bg"])
        workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6), pady=6)
        toolbar = tk.Frame(workspace, bg=self.preferences["surface_bg"])
        toolbar.pack(fill=tk.X, pady=(0, 5))
        editor = scrolledtext.ScrolledText(workspace, wrap=tk.NONE, undo=True, font=("Courier New", 10))
        editor.pack(fill=tk.BOTH, expand=True)
        status_label = tk.Label(
            workspace,
            textvariable=status,
            anchor=tk.W,
            bg=self.preferences["surface_bg"],
        )
        status_label.pack(fill=tk.X, pady=(5, 0))

        def load_selected(event=None):
            selection = files.curselection()
            if not selection:
                return
            path = candidates[selection[0]]
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as error:
                messagebox.showerror("Modding Environment", str(error), parent=self.root)
                return
            current["path"] = path
            editor.delete("1.0", tk.END)
            editor.insert("1.0", content)
            editor.edit_modified(False)
            status.set(str(path))

        def save_mod():
            path = current["path"]
            if path is None:
                return
            content = editor.get("1.0", "end-1c")
            try:
                if path.suffix.lower() == ".py":
                    compile(content, str(path), "exec")
                elif path.suffix.lower() == ".json":
                    json.loads(content)
                backup_dir = project / ".pyos_mod_backups"
                backup_dir.mkdir(exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                shutil.copy2(path, backup_dir / f"{path.name}.{stamp}.bak")
                path.write_text(content, encoding="utf-8")
                editor.edit_modified(False)
                status.set(f"Saved {path.name}; backup created in {backup_dir.name}.")
                if path.parent.resolve() == apps_directory.resolve():
                    self.refresh_custom_app_icons()
            except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as error:
                messagebox.showerror("Modding Environment", f"Not saved: {error}", parent=self.root)

        tk.Button(toolbar, text="Save + Validate", command=save_mod).pack(side=tk.LEFT)
        tk.Button(toolbar, text="Reload", command=load_selected).pack(side=tk.LEFT, padx=5)
        tk.Button(toolbar, text="App Maker", command=self.open_app_maker).pack(side=tk.LEFT, padx=(8, 0))
        files.bind("<<ListboxSelect>>", load_selected)
        editor.bind("<Control-s>", lambda event: (save_mod(), "break")[1])

    def _custom_apps_directory(self):
        path = self.settings_path.parent / "apps"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_custom_app(self, path):
        """Validate and run a user-created app inside a pyOS window."""
        path = Path(path).resolve()
        apps_directory = self._custom_apps_directory().resolve()
        if path.parent != apps_directory or path.suffix.lower() != ".py":
            raise ValueError("The app must be a Python file in the pyOS apps directory.")
        source = path.read_text(encoding="utf-8")
        code = compile(source, str(path), "exec")
        namespace = {
            "__name__": f"pyos_app_{path.stem}",
            "__file__": str(path),
            "tk": tk,
            "ttk": ttk,
            "messagebox": messagebox,
        }
        exec(code, namespace)
        builder = namespace.get("build")
        if not callable(builder):
            raise ValueError("App must define build(app, window).")
        title = str(namespace.get("APP_NAME", path.stem.replace("_", " ").title()))[:80]
        app_window = self.create_window(title, width=640, height=440)
        try:
            builder(self, app_window)
        except Exception:
            app_window.close()
            raise
        return app_window

    def open_app_maker(self):
        """Create, edit, and launch Python apps hosted inside pyOS."""
        window = self.create_window("App Maker", width=920, height=590)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]
        apps_directory = self._custom_apps_directory()
        current = {"path": None}
        status = tk.StringVar(value="Create an app or select an existing one.")
        app_name = tk.StringVar(value="My App")

        sidebar = tk.Frame(window.content, bg=surface, width=190)
        sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=8, pady=8)
        tk.Label(sidebar, text="YOUR APPS", font=("Courier New", 11, "bold"),
                 bg=surface, fg=foreground).pack(fill=tk.X, pady=(0, 5))
        app_list = tk.Listbox(sidebar, width=24)
        app_list.pack(fill=tk.BOTH, expand=True)

        workspace = tk.Frame(window.content, bg=surface)
        workspace.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8), pady=8)
        toolbar = tk.Frame(workspace, bg=surface)
        toolbar.pack(fill=tk.X, pady=(0, 5))
        tk.Label(toolbar, text="Name:", bg=surface, fg=foreground).pack(side=tk.LEFT)
        tk.Entry(toolbar, textvariable=app_name, width=22).pack(side=tk.LEFT, padx=5)
        editor = scrolledtext.ScrolledText(
            workspace, wrap=tk.NONE, undo=True, font=("Courier New", 10),
        )
        editor.pack(fill=tk.BOTH, expand=True)
        tk.Label(workspace, textvariable=status, anchor=tk.W, bg=surface, fg=foreground,
                 wraplength=650).pack(fill=tk.X, pady=(5, 0))

        template = '''APP_NAME = "My App"


def build(app, window):
    """Build this app inside the supplied pyOS window."""
    content = window.content
    content.configure(bg="white")

    title = tk.Label(
        content,
        text="Hello from my pyOS app!",
        font=("Courier New", 16, "bold"),
        bg="white",
    )
    title.pack(pady=30)

    message = tk.StringVar(value="Press the button to begin.")
    tk.Label(content, textvariable=message, bg="white").pack(pady=10)
    tk.Button(
        content,
        text="Run Action",
        command=lambda: message.set("Your app is running inside pyOS."),
    ).pack(pady=10)
'''

        def app_paths():
            return sorted(apps_directory.glob("*.py"), key=lambda item: item.name.casefold())

        def refresh(select_path=None):
            paths = app_paths()
            app_list.delete(0, tk.END)
            for item in paths:
                app_list.insert(tk.END, item.stem.replace("_", " ").title())
            if select_path in paths:
                index = paths.index(select_path)
                app_list.selection_set(index)
                app_list.see(index)

        def new_app():
            current["path"] = None
            app_name.set("My App")
            editor.delete("1.0", tk.END)
            editor.insert("1.0", template)
            editor.edit_modified(False)
            status.set("New app template. Choose a name, then Save App.")

        def load_app(event=None):
            selection = app_list.curselection()
            paths = app_paths()
            if not selection or selection[0] >= len(paths):
                return
            path = paths[selection[0]]
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as error:
                messagebox.showerror("App Maker", str(error), parent=self.root)
                return
            current["path"] = path
            app_name.set(path.stem.replace("_", " ").title())
            editor.delete("1.0", tk.END)
            editor.insert("1.0", source)
            editor.edit_modified(False)
            status.set(str(path))

        def target_path():
            display_name = app_name.get().strip()
            slug = "_".join(display_name.lower().split())
            slug = "".join(character for character in slug if character.isalnum() or character == "_")
            slug = slug.strip("_")
            if not slug or not display_name:
                raise ValueError("Enter an app name containing letters or numbers.")
            return apps_directory / f"{slug}.py"

        def save_app():
            source = editor.get("1.0", "end-1c")
            try:
                syntax_tree = ast.parse(source, filename=str(target_path()), mode="exec")
                compile(syntax_tree, str(target_path()), "exec")
                if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build"
                           for node in syntax_tree.body):
                    raise ValueError("App must define build(app, window).")
                destination = target_path()
                old_path = current["path"]
                if destination.exists():
                    backup_dir = apps_directory / ".backups"
                    backup_dir.mkdir(exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                    shutil.copy2(destination, backup_dir / f"{destination.name}.{stamp}.bak")
                destination.write_text(source, encoding="utf-8")
                if old_path and old_path != destination and old_path.exists():
                    old_path.unlink()
                    old_key = "custom:" + old_path.name.casefold()
                    new_key = "custom:" + destination.name.casefold()
                    old_position = self.preferences["icon_positions"].pop(old_key, None)
                    if old_position is not None:
                        self.preferences["icon_positions"][new_key] = old_position
                        self.save_preferences()
                current["path"] = destination
                editor.edit_modified(False)
                refresh(destination)
                self.refresh_custom_app_icons()
                status.set(f"Saved and validated {destination.name}.")
                return destination
            except (OSError, SyntaxError, ValueError) as error:
                messagebox.showerror("App Maker", f"App was not saved: {error}", parent=self.root)
                return None

        def run_app():
            path = save_app()
            if path is None:
                return
            try:
                self.run_custom_app(path)
                status.set(f"Running {path.stem.replace('_', ' ').title()} inside pyOS.")
            except Exception as error:
                messagebox.showerror("App Maker", f"App could not run: {error}", parent=self.root)

        def delete_app():
            path = current["path"]
            if path is None or not path.exists():
                return
            if not messagebox.askyesno("App Maker", f"Delete {path.name}?", parent=self.root):
                return
            try:
                path.unlink()
            except OSError as error:
                messagebox.showerror("App Maker", str(error), parent=self.root)
                return
            self.preferences["icon_positions"].pop("custom:" + path.name.casefold(), None)
            self.save_preferences()
            new_app()
            refresh()
            self.refresh_custom_app_icons()
            status.set("App deleted.")

        tk.Button(toolbar, text="New", command=new_app).pack(side=tk.LEFT, padx=(8, 0))
        tk.Button(toolbar, text="Save App", command=save_app).pack(side=tk.LEFT, padx=4)
        tk.Button(toolbar, text="Run Inside pyOS", command=run_app).pack(side=tk.LEFT)
        tk.Button(toolbar, text="Delete", command=delete_app).pack(side=tk.RIGHT)
        app_list.bind("<<ListboxSelect>>", load_app)
        editor.bind("<Control-s>", lambda event: (save_app(), "break")[1])
        refresh()
        new_app()

    def open_weather(self):
        """Show current conditions and a seven-day forecast for a selected location."""
        window = self.create_window("Weather", width=760, height=560)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]
        location_query = tk.StringVar()
        status = tk.StringVar(value="Detecting your approximate location...")

        toolbar = tk.Frame(window.content, bg=surface)
        toolbar.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(toolbar, text="City or postcode:", bg=surface, fg=foreground).pack(side=tk.LEFT)
        location_entry = tk.Entry(toolbar, textvariable=location_query)
        location_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        heading = tk.Label(
            window.content, text="WEATHER", font=("Courier New", 17, "bold"),
            bg=surface, fg=foreground, anchor=tk.W,
        )
        heading.pack(fill=tk.X, padx=14)
        current_label = tk.Label(
            window.content, text="", font=("Courier New", 11), justify=tk.LEFT,
            anchor=tk.NW, bg=surface, fg=foreground,
        )
        current_label.pack(fill=tk.X, padx=14, pady=(6, 12))
        tk.Label(
            window.content, text="7-DAY FORECAST", font=("Courier New", 11, "bold"),
            bg=surface, fg=foreground, anchor=tk.W,
        ).pack(fill=tk.X, padx=14)
        forecast = scrolledtext.ScrolledText(
            window.content, height=13, wrap=tk.NONE, font=("Courier New", 10),
            bg=surface, fg=foreground,
        )
        forecast.pack(fill=tk.BOTH, expand=True, padx=10, pady=6)
        forecast.configure(state=tk.DISABLED)
        tk.Label(
            window.content, textvariable=status, anchor=tk.W, bg=surface, fg=foreground,
        ).pack(fill=tk.X, padx=12, pady=(0, 8))

        def request_json(url):
            request = urllib.request.Request(url, headers={"User-Agent": "pyOS-Weather/1.0"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        def fetch_forecast(latitude, longitude):
            params = urllib.parse.urlencode({
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset,wind_speed_10m_max",
                "timezone": "auto",
                "forecast_days": 7,
            })
            return request_json("https://api.open-meteo.com/v1/forecast?" + params)

        def search_location(query):
            params = urllib.parse.urlencode({"name": query, "count": 1, "language": "en", "format": "json"})
            data = request_json("https://geocoding-api.open-meteo.com/v1/search?" + params)
            results = data.get("results") or []
            if not results:
                raise ValueError(f"No location found for '{query}'.")
            result = results[0]
            label = ", ".join(filter(None, (result.get("name"), result.get("admin1"), result.get("country"))))
            return result["latitude"], result["longitude"], label

        def auto_location():
            data = request_json("https://ipapi.co/json/")
            latitude, longitude = data.get("latitude"), data.get("longitude")
            if latitude is None or longitude is None:
                raise ValueError("Automatic location detection was unavailable. Enter a city instead.")
            label = ", ".join(filter(None, (data.get("city"), data.get("region"), data.get("country_name"))))
            return latitude, longitude, label or "Current location"

        def render(data, label):
            current = data.get("current", {})
            units = data.get("current_units", {})
            description = WEATHER_DESCRIPTIONS.get(current.get("weather_code"), "Unknown conditions")
            heading.configure(text=label.upper())
            current_label.configure(text=(
                f"{description} | {current.get('temperature_2m', '?')}{units.get('temperature_2m', '°C')}"
                f" (feels like {current.get('apparent_temperature', '?')}{units.get('apparent_temperature', '°C')})\n"
                f"Humidity: {current.get('relative_humidity_2m', '?')}%    Cloud: {current.get('cloud_cover', '?')}%    "
                f"Rain: {current.get('precipitation', '?')} mm\n"
                f"Wind: {current.get('wind_speed_10m', '?')} km/h, gusting {current.get('wind_gusts_10m', '?')} km/h    "
                f"Pressure: {current.get('pressure_msl', '?')} hPa"
            ))
            daily = data.get("daily", {})
            rows = ["DATE         CONDITIONS                 LOW / HIGH   RAIN   WIND"]
            count = len(daily.get("time", []))
            for index in range(count):
                code = daily.get("weather_code", [None] * count)[index]
                condition = WEATHER_DESCRIPTIONS.get(code, "Unknown")[:26]
                rows.append(
                    f"{daily['time'][index]:<12} {condition:<26} "
                    f"{daily['temperature_2m_min'][index]:>4.0f}° / {daily['temperature_2m_max'][index]:>4.0f}°   "
                    f"{daily['precipitation_probability_max'][index]:>3}%   "
                    f"{daily['wind_speed_10m_max'][index]:>3.0f} km/h"
                )
            forecast.configure(state=tk.NORMAL)
            forecast.delete("1.0", tk.END)
            forecast.insert("1.0", "\n".join(rows))
            forecast.configure(state=tk.DISABLED)
            status.set(f"Updated {datetime.now().strftime('%H:%M')} • Forecast data: Open-Meteo")

        def load_weather(use_auto=False):
            query = location_query.get().strip()
            if not use_auto and len(query) < 2:
                status.set("Enter at least two characters for a location.")
                return
            status.set("Loading weather data...")

            def worker():
                try:
                    latitude, longitude, label = auto_location() if use_auto else search_location(query)
                    data = fetch_forecast(latitude, longitude)
                    self.root.after(0, lambda: render(data, label))
                except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                    self.root.after(0, lambda message=str(error): status.set(f"Weather unavailable: {message}"))

            threading.Thread(target=worker, daemon=True).start()

        tk.Button(toolbar, text="Search", command=load_weather).pack(side=tk.LEFT)
        tk.Button(toolbar, text="My Location", command=lambda: load_weather(True)).pack(side=tk.LEFT, padx=(5, 0))
        location_entry.bind("<Return>", lambda event: load_weather())
        load_weather(True)

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

    def open_news(self):
        """Browse current headlines and search Google News RSS feeds."""
        window = self.create_window("News", width=880, height=570)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]
        category = tk.StringVar(value="Top Stories")
        search_text = tk.StringVar()
        status = tk.StringVar(value="Loading current headlines...")
        articles = []

        toolbar = tk.Frame(window.content, bg=surface)
        toolbar.pack(fill=tk.X, padx=8, pady=8)
        tk.Label(toolbar, text="Section:", bg=surface, fg=foreground).pack(side=tk.LEFT)
        section_box = ttk.Combobox(
            toolbar, textvariable=category,
            values=("Top Stories", "World", "UK", "Business", "Technology", "Science", "Health", "Sports", "Entertainment"),
            state="readonly", width=15,
        )
        section_box.pack(side=tk.LEFT, padx=5)
        tk.Label(toolbar, text="Search:", bg=surface, fg=foreground).pack(side=tk.LEFT, padx=(10, 0))
        search_entry = tk.Entry(toolbar, textvariable=search_text)
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)

        pane = tk.PanedWindow(window.content, orient=tk.HORIZONTAL, bg=surface, sashwidth=5)
        pane.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        headline_list = tk.Listbox(pane, width=48)
        detail = scrolledtext.ScrolledText(pane, wrap=tk.WORD, state=tk.DISABLED)
        pane.add(headline_list, minsize=300)
        pane.add(detail, minsize=300)

        footer = tk.Frame(window.content, bg=surface)
        footer.pack(fill=tk.X, padx=8, pady=(0, 8))
        tk.Label(footer, textvariable=status, bg=surface, fg=foreground, anchor=tk.W).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )

        topics = {
            "World": "WORLD", "UK": "NATION", "Business": "BUSINESS",
            "Technology": "TECHNOLOGY", "Science": "SCIENCE", "Health": "HEALTH",
            "Sports": "SPORTS", "Entertainment": "ENTERTAINMENT",
        }

        def feed_url():
            query = search_text.get().strip()
            locale = "hl=en-GB&gl=GB&ceid=GB%3Aen"
            if query:
                return "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query}) + "&" + locale
            topic = topics.get(category.get())
            if topic:
                return f"https://news.google.com/rss/headlines/section/topic/{topic}?{locale}"
            return "https://news.google.com/rss?" + locale

        def clean_summary(raw):
            text = re.sub(r"<[^>]+>", " ", raw or "")
            return re.sub(r"\s+", " ", html.unescape(text)).strip()

        def show_article(event=None):
            selection = headline_list.curselection()
            if not selection or selection[0] >= len(articles):
                return
            article = articles[selection[0]]
            text = (f"{article['title']}\n\nSource: {article['source']}\n"
                    f"Published: {article['published']}\n\n{article['summary']}")
            detail.configure(state=tk.NORMAL)
            detail.delete("1.0", tk.END)
            detail.insert("1.0", text)
            detail.configure(state=tk.DISABLED)

        def open_article():
            selection = headline_list.curselection()
            if selection and selection[0] < len(articles):
                webbrowser.open(articles[selection[0]]["link"])

        def refresh_news(event=None):
            url = feed_url()
            status.set("Updating headlines...")

            def worker():
                try:
                    request = urllib.request.Request(url, headers={"User-Agent": "pyOS-News/1.0"})
                    with urllib.request.urlopen(request, timeout=12) as response:
                        root = ET.fromstring(response.read())
                    fetched = []
                    for item in root.findall("./channel/item")[:50]:
                        source = item.find("source")
                        fetched.append({
                            "title": item.findtext("title", "Untitled"),
                            "link": item.findtext("link", ""),
                            "published": item.findtext("pubDate", "Unknown"),
                            "source": source.text if source is not None and source.text else "Unknown",
                            "summary": clean_summary(item.findtext("description", "")),
                        })

                    def display():
                        articles[:] = fetched
                        headline_list.delete(0, tk.END)
                        for article in articles:
                            headline_list.insert(tk.END, article["title"])
                        status.set(f"{len(articles)} stories • Updated {datetime.now().strftime('%H:%M')}")
                        if articles:
                            headline_list.selection_set(0)
                            show_article()
                    self.root.after(0, display)
                except (OSError, ET.ParseError, ValueError) as error:
                    self.root.after(0, lambda message=str(error): status.set(f"News unavailable: {message}"))

            threading.Thread(target=worker, daemon=True).start()

        tk.Button(toolbar, text="Search / Refresh", command=refresh_news).pack(side=tk.LEFT)
        tk.Button(footer, text="Open Full Story", command=open_article).pack(side=tk.RIGHT, padx=(6, 0))
        headline_list.bind("<<ListboxSelect>>", show_article)
        headline_list.bind("<Double-Button-1>", lambda event: open_article())
        search_entry.bind("<Return>", refresh_news)
        section_box.bind("<<ComboboxSelected>>", refresh_news)
        refresh_news()

    def open_games_suite(self):
        """Open the launcher for pyOS's small built-in games."""
        window = self.create_window("Games Suite", width=560, height=390)
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        tk.Label(
            window.content, text="pyOS GAMES SUITE", bg=background, fg=foreground,
            font=("Courier New", 18, "bold"),
        ).pack(pady=(22, 8))
        tk.Label(
            window.content, text="Choose a game", bg=background, fg=foreground,
            font=("Courier New", 11),
        ).pack(pady=(0, 15))
        games = (
            ("SNAKE", "Eat food, grow, and avoid the walls.", self.open_snake),
            ("SUDOKU", "Complete a generated 9 x 9 number puzzle.", self.open_sudoku),
            ("AUTOMATED CHESS", "Play White against a computer opponent.", self.open_chess),
        )
        for title, description, command in games:
            card = tk.Frame(window.content, bg=background, relief=tk.RAISED, bd=2)
            card.pack(fill=tk.X, padx=35, pady=5)
            tk.Button(
                card, text=title, command=command, width=20, bg=background, fg=foreground,
                font=("Courier New", 10, "bold"),
            ).pack(side=tk.LEFT, padx=8, pady=8)
            tk.Label(card, text=description, bg=background, fg=foreground, anchor=tk.W).pack(
                side=tk.LEFT, fill=tk.X, expand=True, padx=5
            )

    def open_snake(self):
        """Open a keyboard-controlled Snake game."""
        window = self.create_window("Snake", width=570, height=540)
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        status = tk.StringVar(value="Press Start, then use arrow keys or WASD.")
        score = tk.StringVar(value="Score: 0")
        header = tk.Frame(window.content, bg=background)
        header.pack(fill=tk.X, padx=10, pady=7)
        tk.Label(header, textvariable=score, bg=background, fg=foreground,
                 font=("Courier New", 11, "bold")).pack(side=tk.LEFT)
        tk.Label(header, textvariable=status, bg=background, fg=foreground).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=15
        )
        canvas = tk.Canvas(window.content, width=500, height=420, bg="#101010",
                           highlightthickness=2, highlightbackground=foreground)
        canvas.pack(padx=10, pady=(0, 6))
        state = {"snake": [], "food": None, "direction": (1, 0), "next": (1, 0),
                 "running": False, "after": None}
        cell, columns, rows = 20, 25, 21

        def place_food():
            available = [(x, y) for x in range(columns) for y in range(rows)
                         if (x, y) not in state["snake"]]
            state["food"] = random.choice(available) if available else None

        def draw():
            canvas.delete("all")
            if state["food"] is not None:
                x, y = state["food"]
                canvas.create_oval(x * cell + 3, y * cell + 3, (x + 1) * cell - 3,
                                   (y + 1) * cell - 3, fill="#e53935", outline="")
            for index, (x, y) in enumerate(state["snake"]):
                color = "#9cff57" if index == 0 else "#43a047"
                canvas.create_rectangle(x * cell + 1, y * cell + 1, (x + 1) * cell - 1,
                                        (y + 1) * cell - 1, fill=color, outline="#101010")

        def stop(message):
            state["running"] = False
            state["after"] = None
            status.set(message)

        def tick():
            state["after"] = None
            if not state["running"] or not window.frame.winfo_exists():
                return
            state["direction"] = state["next"]
            dx, dy = state["direction"]
            head = (state["snake"][0][0] + dx, state["snake"][0][1] + dy)
            if (head[0] < 0 or head[0] >= columns or head[1] < 0 or head[1] >= rows
                    or head in state["snake"][:-1]):
                stop("Game over. Press New Game to try again.")
                return
            state["snake"].insert(0, head)
            if head == state["food"]:
                score.set(f"Score: {len(state['snake']) - 4}")
                place_food()
            else:
                state["snake"].pop()
            draw()
            state["after"] = self.root.after(max(65, 150 - len(state["snake"]) * 2), tick)

        def change_direction(direction):
            current = state["direction"]
            if direction != (-current[0], -current[1]):
                state["next"] = direction

        def key_pressed(event):
            directions = {
                "Left": (-1, 0), "a": (-1, 0), "A": (-1, 0),
                "Right": (1, 0), "d": (1, 0), "D": (1, 0),
                "Up": (0, -1), "w": (0, -1), "W": (0, -1),
                "Down": (0, 1), "s": (0, 1), "S": (0, 1),
            }
            if event.keysym in directions:
                change_direction(directions[event.keysym])
                return "break"

        def new_game():
            if state["after"] is not None:
                self.root.after_cancel(state["after"])
            state.update(snake=[(8, 10), (7, 10), (6, 10), (5, 10)],
                         direction=(1, 0), next=(1, 0), running=True, after=None)
            score.set("Score: 0")
            status.set("Running")
            place_food()
            draw()
            canvas.focus_set()
            state["after"] = self.root.after(150, tick)

        tk.Button(window.content, text="New Game", command=new_game).pack(pady=(0, 7))
        canvas.bind("<KeyPress>", key_pressed)
        window.frame.bind("<Destroy>", lambda event: (
            self.root.after_cancel(state["after"]) if event.widget is window.frame
            and state["after"] is not None else None
        ), add="+")
        new_game()

    def open_sudoku(self):
        """Open a generated Sudoku puzzle."""
        window = self.create_window("Sudoku", width=550, height=610)
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        status = tk.StringVar(value="Fill every row, column, and 3 x 3 box with 1-9.")
        tk.Label(window.content, textvariable=status, bg=background, fg=foreground).pack(pady=8)
        board_frame = tk.Frame(window.content, bg=foreground, bd=3)
        board_frame.pack(padx=15, pady=4)
        entries = [[None for _ in range(9)] for _ in range(9)]
        state = {"solution": None, "puzzle": None}
        validation = (self.root.register(lambda value: value == "" or
                                         (len(value) == 1 and value in "123456789")), "%P")
        for row in range(9):
            for column in range(9):
                cell_frame = tk.Frame(
                    board_frame, bg=foreground,
                    padx=(2 if column % 3 == 0 else 0),
                    pady=(2 if row % 3 == 0 else 0),
                )
                cell_frame.grid(row=row, column=column)
                entry = tk.Entry(
                    cell_frame, width=2, justify=tk.CENTER, font=("Courier New", 17, "bold"),
                    validate="key", validatecommand=validation, relief=tk.FLAT,
                )
                entry.pack(ipadx=4, ipady=4, padx=1, pady=1)
                entries[row][column] = entry

        def generate_puzzle():
            base = 3
            pattern = lambda row, column: (base * (row % base) + row // base + column) % 9
            groups = range(base)
            rows = [group * base + row for group in random.sample(list(groups), base)
                    for row in random.sample(list(groups), base)]
            columns = [group * base + column for group in random.sample(list(groups), base)
                       for column in random.sample(list(groups), base)]
            numbers = random.sample(range(1, 10), 9)
            solution = [[numbers[pattern(row, column)] for column in columns] for row in rows]
            puzzle = [line[:] for line in solution]
            for index in random.sample(range(81), 48):
                puzzle[index // 9][index % 9] = 0
            return puzzle, solution

        def new_puzzle():
            puzzle, solution = generate_puzzle()
            state.update(puzzle=puzzle, solution=solution)
            status.set("Fill every row, column, and 3 x 3 box with 1-9.")
            for row in range(9):
                for column in range(9):
                    entry = entries[row][column]
                    entry.configure(state=tk.NORMAL, bg="#ffffff", fg="#1565c0")
                    entry.delete(0, tk.END)
                    if puzzle[row][column]:
                        entry.insert(0, str(puzzle[row][column]))
                        entry.configure(state=tk.DISABLED, disabledbackground="#e0e0e0",
                                        disabledforeground="#000000")

        def check_puzzle():
            incomplete = False
            incorrect = 0
            for row in range(9):
                for column in range(9):
                    entry = entries[row][column]
                    if state["puzzle"][row][column]:
                        continue
                    value = entry.get()
                    entry.configure(bg="#ffffff")
                    if not value:
                        incomplete = True
                    elif int(value) != state["solution"][row][column]:
                        entry.configure(bg="#ffcdd2")
                        incorrect += 1
            if incorrect:
                status.set(f"{incorrect} incorrect cell(s) are highlighted.")
            elif incomplete:
                status.set("Correct so far, but the puzzle is incomplete.")
            else:
                status.set("Solved correctly!")
                self.show_notification("Sudoku", "Puzzle solved correctly!", kind="system")

        controls = tk.Frame(window.content, bg=background)
        controls.pack(pady=8)
        tk.Button(controls, text="New Puzzle", command=new_puzzle).pack(side=tk.LEFT, padx=5)
        tk.Button(controls, text="Check", command=check_puzzle).pack(side=tk.LEFT, padx=5)
        new_puzzle()

    def open_chess(self):
        """Open chess against a lightweight automated opponent."""
        try:
            import chess
        except ImportError:
            messagebox.showerror(
                "Automated Chess",
                "Chess requires the 'chess' package. Run setup again to install it.",
                parent=self.root,
            )
            return
        window = self.create_window("Automated Chess", width=670, height=700)
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        status = tk.StringVar(value="You are White. Select a piece, then its destination.")
        tk.Label(window.content, textvariable=status, bg=background, fg=foreground).pack(pady=6)
        board_canvas = tk.Canvas(window.content, width=576, height=576, highlightthickness=2,
                                 highlightbackground=foreground)
        board_canvas.pack(padx=10, pady=4)
        board = chess.Board()
        state = {"selected": None, "thinking": False, "after": None}
        size = 72
        pieces = {
            "K": "♔", "Q": "♕", "R": "♖", "B": "♗", "N": "♘", "P": "♙",
            "k": "♚", "q": "♛", "r": "♜", "b": "♝", "n": "♞", "p": "♟",
        }

        def draw_board():
            board_canvas.delete("all")
            legal_targets = set()
            if state["selected"] is not None:
                legal_targets = {move.to_square for move in board.legal_moves
                                 if move.from_square == state["selected"]}
            for display_row in range(8):
                for file_index in range(8):
                    square = chess.square(file_index, 7 - display_row)
                    x0, y0 = file_index * size, display_row * size
                    color = "#f0d9b5" if (file_index + display_row) % 2 == 0 else "#8b5a2b"
                    if square == state["selected"]:
                        color = "#f6e05e"
                    board_canvas.create_rectangle(x0, y0, x0 + size, y0 + size,
                                                  fill=color, outline=color)
                    if square in legal_targets:
                        board_canvas.create_oval(x0 + 28, y0 + 28, x0 + 44, y0 + 44,
                                                 fill="#43a047", outline="")
                    piece = board.piece_at(square)
                    if piece:
                        board_canvas.create_text(x0 + size / 2, y0 + size / 2,
                                                 text=pieces[piece.symbol()],
                                                 font=("Segoe UI Symbol", 42))
            for index, letter in enumerate("abcdefgh"):
                board_canvas.create_text(index * size + 7, 568, text=letter,
                                         anchor=tk.SW, font=("Courier New", 8, "bold"))

        def material_score():
            values = {chess.PAWN: 1, chess.KNIGHT: 3, chess.BISHOP: 3.2,
                      chess.ROOK: 5, chess.QUEEN: 9, chess.KING: 0}
            return sum((1 if piece.color == chess.WHITE else -1) * values[piece.piece_type]
                       for piece in board.piece_map().values())

        def update_game_status():
            if board.is_checkmate():
                winner = "White" if board.turn == chess.BLACK else "Computer"
                status.set(f"Checkmate. {winner} wins.")
                self.show_notification("Automated Chess", f"Checkmate. {winner} wins.")
                return True
            if board.is_game_over():
                status.set(f"Game over: {board.outcome().termination.name.replace('_', ' ').title()}.")
                return True
            return False

        def choose_computer_move():
            moves = list(board.legal_moves)
            random.shuffle(moves)
            best_move, best_score = moves[0], float("inf")
            for move in moves:
                board.push(move)
                if board.is_checkmate():
                    score = -10000
                else:
                    replies = list(board.legal_moves)
                    if replies:
                        reply_scores = []
                        for reply in replies:
                            board.push(reply)
                            reply_scores.append(material_score())
                            board.pop()
                        score = max(reply_scores)
                    else:
                        score = material_score()
                board.pop()
                if score < best_score:
                    best_move, best_score = move, score
            return best_move

        def computer_move():
            state["after"] = None
            if not window.frame.winfo_exists() or board.turn != chess.BLACK or board.is_game_over():
                state["thinking"] = False
                return
            board.push(choose_computer_move())
            state["thinking"] = False
            draw_board()
            if not update_game_status():
                status.set("Your turn (White).")

        def clicked(event):
            if state["thinking"] or board.turn != chess.WHITE or board.is_game_over():
                return
            file_index, display_row = event.x // size, event.y // size
            if not (0 <= file_index < 8 and 0 <= display_row < 8):
                return
            square = chess.square(file_index, 7 - display_row)
            if state["selected"] is None:
                piece = board.piece_at(square)
                if piece and piece.color == chess.WHITE:
                    state["selected"] = square
                    draw_board()
                return
            candidates = [move for move in board.legal_moves
                          if move.from_square == state["selected"] and move.to_square == square]
            state["selected"] = None
            if candidates:
                move = next((candidate for candidate in candidates
                             if candidate.promotion == chess.QUEEN), candidates[0])
                board.push(move)
                draw_board()
                if not update_game_status():
                    state["thinking"] = True
                    status.set("Computer is thinking...")
                    state["after"] = self.root.after(250, computer_move)
            else:
                draw_board()

        def new_game():
            if state["after"] is not None:
                self.root.after_cancel(state["after"])
            board.reset()
            state.update(selected=None, thinking=False, after=None)
            status.set("You are White. Select a piece, then its destination.")
            draw_board()

        board_canvas.bind("<Button-1>", clicked)
        tk.Button(window.content, text="New Game", command=new_game).pack(pady=5)
        window.frame.bind("<Destroy>", lambda event: (
            self.root.after_cancel(state["after"]) if event.widget is window.frame
            and state["after"] is not None else None
        ), add="+")
        draw_board()

    def open_messenger(self):
        """Open the LAN peer-to-peer text and image messenger."""
        if self.messenger_window and self.messenger_window.frame.winfo_exists():
            self.desktop_canvas.tag_raise(self.messenger_window.window_id)
            return
        if self.messenger_service is None:
            accepted = messagebox.askokcancel(
                "Peer-to-Peer Messenger Warning",
                "SECURITY AND PRIVACY WARNING\n\n"
                "Connecting exposes your pyOS username, IP address, and online status to other "
                "pyOS users on the local network. Messages are sent directly and are not "
                "end-to-end encrypted. Hackers or malicious peers could gain information about "
                "you or send harmful content once connected.\n\n"
                "Only connect on a trusted network and only open images from people you trust. "
                "Continue?",
                parent=self.root,
            )
            if not accepted:
                return
            username = get_username()
            if not username:
                messagebox.showerror("Messenger", "Create a pyOS account before using Messenger.")
                return
            try:
                self.messenger_service = PeerMessenger(
                    username, lambda event, payload: self.messenger_events.put((event, payload))
                )
                self.messenger_service.start()
            except OSError as error:
                if self.messenger_service:
                    self.messenger_service.stop()
                self.messenger_service = None
                messagebox.showerror(
                    "Messenger",
                    f"Could not start peer discovery: {error}\nCheck firewall and port settings.",
                )
                return

        while not self.messenger_events.empty():
            try:
                self.messenger_events.get_nowait()
            except queue.Empty:
                break
        service = self.messenger_service
        window = self.create_window("Peer-to-Peer Messenger", width=860, height=590)
        self.messenger_window = window
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        status = tk.StringVar(value=f"Online as {service.username}. Waiting for peers...")
        selected_peer = tk.StringVar()
        last_received_image = {"message": None}
        image_references = []

        warning = tk.Label(
            window.content,
            text="WARNING: P2P connections reveal network information. Hackers could gain information about you.",
            bg="#fff2cc", fg="#8a1c1c", anchor=tk.W, justify=tk.LEFT, wraplength=800,
        )
        warning.pack(fill=tk.X, padx=7, pady=(7, 3))
        body = tk.PanedWindow(window.content, orient=tk.HORIZONTAL, sashwidth=5, bg=foreground)
        body.pack(fill=tk.BOTH, expand=True, padx=7, pady=4)
        peer_frame = tk.Frame(body, bg=background, width=190)
        chat_frame = tk.Frame(body, bg=background)
        body.add(peer_frame, minsize=160)
        body.add(chat_frame, minsize=400)

        tk.Label(peer_frame, text="ONLINE USERS", bg=background, fg=foreground,
                 font=("Courier New", 10, "bold")).pack(fill=tk.X, padx=5, pady=5)
        peers = tk.Listbox(
            peer_frame, bg=background, fg=foreground, selectbackground="#1976d2",
            selectforeground="#ffffff", exportselection=False,
        )
        peers.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))

        conversation = scrolledtext.ScrolledText(
            chat_frame, state=tk.DISABLED, wrap=tk.WORD, bg=background, fg=foreground,
            font=("Courier New", 10),
        )
        conversation.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        def choose_peer(event=None):
            selection = peers.curselection()
            selected_peer.set(peers.get(selection[0]) if selection else "")
            status.set(f"Selected {selected_peer.get()}." if selection else "Select an online user.")

        peers.bind("<<ListboxSelect>>", choose_peer)

        def render_message(message):
            direction = message.get("direction")
            name = "You" if direction == "outgoing" else message.get("sender", "Unknown")
            destination = f" to {message.get('recipient')}" if direction == "outgoing" else ""
            stamp = datetime.fromtimestamp(message.get("timestamp", time.time())).strftime("%H:%M:%S")
            conversation.configure(state=tk.NORMAL)
            conversation.insert(tk.END, f"[{stamp}] {name}{destination}:\n")
            if message.get("kind") == "text":
                conversation.insert(tk.END, message.get("text", "") + "\n\n")
            else:
                conversation.insert(tk.END, f"[Image: {message.get('filename', 'image')}]\n")
                try:
                    from PIL import Image, ImageTk
                    image = Image.open(io.BytesIO(message["data"]))
                    if image.width * image.height > 25_000_000:
                        raise ValueError("Image dimensions are too large to preview safely.")
                    image.thumbnail((360, 240))
                    photo = ImageTk.PhotoImage(image)
                    image_references.append(photo)
                    conversation.image_create(tk.END, image=photo)
                    conversation.insert(tk.END, "\n\n")
                except Exception:
                    conversation.insert(tk.END, "Preview unavailable.\n\n")
                if direction == "incoming":
                    last_received_image["message"] = message
            conversation.configure(state=tk.DISABLED)
            conversation.see(tk.END)

        for historic_message in service.history:
            render_message(historic_message)

        input_row = tk.Frame(chat_frame, bg=background)
        input_row.pack(fill=tk.X, padx=5, pady=(0, 5))
        message_text = tk.StringVar()
        message_entry = tk.Entry(
            input_row, textvariable=message_text, bg=background, fg=foreground,
            insertbackground=foreground,
        )
        message_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def run_send(action):
            def worker():
                try:
                    action()
                except (OSError, ValueError) as error:
                    self.messenger_events.put(("error", f"Send failed: {error}"))
            threading.Thread(target=worker, daemon=True).start()

        def send_text(event=None):
            recipient = selected_peer.get()
            text = message_text.get()
            if not recipient:
                status.set("Select an online user first.")
                return "break"
            if not text.strip():
                return "break"
            message_text.set("")
            run_send(lambda: service.send_text(recipient, text))
            return "break"

        def send_image():
            recipient = selected_peer.get()
            if not recipient:
                status.set("Select an online user first.")
                return

            def selected(path):
                path = Path(path)
                if path.suffix.lower() not in IMAGE_EXTENSIONS:
                    status.set("Choose a supported image file.")
                    return
                run_send(lambda: service.send_image(recipient, path))

            self.open_file_picker(Path.home(), selected)

        def save_received_image():
            message = last_received_image["message"]
            if not message:
                status.set("No received image is available to save.")
                return
            destination = filedialog.asksaveasfilename(
                parent=self.root,
                title="Save Received Image",
                initialdir=str(get_downloads_dir()),
                initialfile=message.get("filename", "received-image"),
            )
            if destination:
                try:
                    Path(destination).write_bytes(message["data"])
                    status.set(f"Saved {destination}")
                except OSError as error:
                    status.set(f"Could not save image: {error}")

        tk.Button(input_row, text="Send", command=send_text).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(input_row, text="Send Image", command=send_image).pack(side=tk.LEFT, padx=(4, 0))
        tk.Button(input_row, text="Save Last Image", command=save_received_image).pack(
            side=tk.LEFT, padx=(4, 0)
        )
        message_entry.bind("<Return>", send_text)
        tk.Label(window.content, textvariable=status, bg=background, fg=foreground, anchor=tk.W).pack(
            fill=tk.X, padx=9, pady=(0, 7)
        )

        def refresh_peers(names):
            previous = selected_peer.get()
            peers.delete(0, tk.END)
            for name in names:
                peers.insert(tk.END, name)
            if previous in names:
                index = names.index(previous)
                peers.selection_set(index)
                peers.activate(index)
            elif previous:
                selected_peer.set("")

        refresh_peers(service.peer_names())

        def handle_messenger_event(event, payload):
            if not window.frame.winfo_exists():
                return
            if event == "peers":
                refresh_peers(payload)
            elif event == "message":
                render_message(payload)
                status.set("Message sent." if payload.get("direction") == "outgoing"
                           else f"Message received from {payload.get('sender')}.")
                if payload.get("direction") == "incoming":
                    summary = (payload.get("text") or f"Image: {payload.get('filename', 'image')}")[:90]
                    self.show_notification(
                        "Messenger", f"{payload.get('sender', 'Unknown')}: {summary}", kind="system"
                    )
            elif event == "error":
                status.set(payload)

        def messenger_destroyed(event):
            if event.widget is window.frame:
                self.messenger_window = None
                if self.messenger_ui_handler is handle_messenger_event:
                    self.messenger_ui_handler = None

        window.frame.bind("<Destroy>", messenger_destroyed, add="+")
        self.messenger_ui_handler = handle_messenger_event
        message_entry.focus_set()

    def open_calculator(self):
        """Open a basic and graphing calculator."""
        window = self.create_window("Calculator", width=700, height=560)
        background = self.preferences.get("surface_bg", "#ffffff")
        foreground = self.preferences.get("text_fg", "#000000")
        chrome = self.preferences.get("chrome_bg", "#000000")

        notebook = ttk.Notebook(window.content)
        notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        basic = tk.Frame(notebook, bg=background)
        graph = tk.Frame(notebook, bg=background)
        notebook.add(basic, text="Basic")
        notebook.add(graph, text="Graph")

        variable_values = {
            letter: tk.StringVar(value=str(math.e) if letter == "e" else "0")
            for letter in "abcdefghijklmnopqrstuvwxyz"
        }
        variable_limits = {
            letter: {"min": tk.StringVar(value="-10"), "max": tk.StringVar(value="10")}
            for letter in "abcdefghijklmnopqrstuvwxyz"
        }

        def read_variables():
            values = {}
            for letter, variable in variable_values.items():
                try:
                    value = float(variable.get())
                except ValueError as error:
                    raise ValueError(f"Variable {letter} must be a number.") from error
                if not math.isfinite(value):
                    raise ValueError(f"Variable {letter} must be finite.")
                values[letter] = value
            return values

        expression = tk.StringVar()
        result_text = tk.StringVar(value="Ready")
        display = tk.Entry(
            basic,
            textvariable=expression,
            justify=tk.RIGHT,
            font=("Courier New", 18),
            bg=background,
            fg=foreground,
            insertbackground=foreground,
            relief=tk.SUNKEN,
            bd=2,
        )
        display.pack(fill=tk.X, padx=12, pady=(14, 4), ipady=8)
        tk.Label(basic, textvariable=result_text, bg=background, fg=foreground, anchor=tk.E).pack(
            fill=tk.X, padx=14, pady=(0, 8)
        )

        def calculate(event=None):
            try:
                value = evaluate_calculator_expression(expression.get(), variables=read_variables())
                rendered = f"{value:.12g}"
                expression.set(rendered)
                result_text.set(f"= {rendered}")
            except ValueError as error:
                result_text.set(f"Error: {error}")
            return "break"

        def press(value):
            if value == "C":
                expression.set("")
                result_text.set("Ready")
            elif value == "DEL":
                expression.set(expression.get()[:-1])
            elif value == "=":
                calculate()
            else:
                display.insert(tk.INSERT, value)
                display.focus_set()

        keypad = tk.Frame(basic, bg=background)
        keypad.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))
        buttons = (
            ("C", "DEL", "(", ")", "/"),
            ("7", "8", "9", "*", "sqrt("),
            ("4", "5", "6", "-", "^"),
            ("1", "2", "3", "+", "pi"),
            ("0", ".", "%", "e", "="),
        )
        for row_index, row in enumerate(buttons):
            keypad.rowconfigure(row_index, weight=1)
            for column_index, label in enumerate(row):
                keypad.columnconfigure(column_index, weight=1)
                tk.Button(
                    keypad,
                    text=label,
                    command=lambda value=label: press(value),
                    bg=background,
                    fg=foreground,
                    activebackground=chrome,
                    activeforeground=self.preferences.get("chrome_fg", "#ffffff"),
                    font=("Courier New", 11, "bold"),
                ).grid(row=row_index, column=column_index, sticky="nsew", padx=2, pady=2)
        display.bind("<Return>", calculate)

        palette = ("#d32f2f", "#1976d2", "#388e3c", "#7b1fa2", "#f57c00", "#0097a7", "#c2185b")
        graph_rows = []
        redraw_after = {"id": None}
        graph_status = tk.StringVar(value="Expressions may be written as y = ... or just ...")
        ranges = {
            "x min": tk.StringVar(value="-10"), "x max": tk.StringVar(value="10"),
            "y min": tk.StringVar(value="-5"), "y max": tk.StringVar(value="5"),
        }

        graph_panes = tk.PanedWindow(graph, orient=tk.HORIZONTAL, sashwidth=5, bg=foreground)
        graph_panes.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(graph_panes, bg=background, width=245)
        graph_panes.add(sidebar, minsize=210)
        plot_area = tk.Frame(graph_panes, bg=background)
        graph_panes.add(plot_area, minsize=280)

        side_tabs = ttk.Notebook(sidebar)
        side_tabs.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        expressions_tab = tk.Frame(side_tabs, bg=background)
        variables_tab = tk.Frame(side_tabs, bg=background)
        side_tabs.add(expressions_tab, text="Expressions")
        side_tabs.add(variables_tab, text="Variables")

        expression_canvas = tk.Canvas(expressions_tab, bg=background, highlightthickness=0)
        expression_scroll = ttk.Scrollbar(expressions_tab, orient=tk.VERTICAL, command=expression_canvas.yview)
        expression_list = tk.Frame(expression_canvas, bg=background)
        expression_window = expression_canvas.create_window((0, 0), window=expression_list, anchor=tk.NW)
        expression_canvas.configure(yscrollcommand=expression_scroll.set)
        expression_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        expression_canvas.pack(fill=tk.BOTH, expand=True)
        expression_list.bind(
            "<Configure>", lambda event: expression_canvas.configure(scrollregion=expression_canvas.bbox("all"))
        )
        expression_canvas.bind(
            "<Configure>", lambda event: expression_canvas.itemconfigure(expression_window, width=event.width)
        )

        variable_canvas = tk.Canvas(variables_tab, bg=background, highlightthickness=0)
        variable_scroll = ttk.Scrollbar(variables_tab, orient=tk.VERTICAL, command=variable_canvas.yview)
        variable_list = tk.Frame(variable_canvas, bg=background)
        variable_window = variable_canvas.create_window((0, 0), window=variable_list, anchor=tk.NW)
        variable_canvas.configure(yscrollcommand=variable_scroll.set)
        variable_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        variable_canvas.pack(fill=tk.BOTH, expand=True)
        variable_list.bind(
            "<Configure>", lambda event: variable_canvas.configure(scrollregion=variable_canvas.bbox("all"))
        )
        variable_canvas.bind(
            "<Configure>", lambda event: variable_canvas.itemconfigure(variable_window, width=event.width)
        )

        tk.Label(
            variable_list, text="Drag a slider or enter a value.\nMin/max set each slider's threshold.\nx is replaced by the graph position.",
            bg=background, fg=foreground, justify=tk.LEFT,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=6, pady=6)
        variable_scales = {}

        def apply_slider_bounds(letter, event=None):
            try:
                minimum = float(variable_limits[letter]["min"].get())
                maximum = float(variable_limits[letter]["max"].get())
                if not math.isfinite(minimum) or not math.isfinite(maximum) or minimum >= maximum:
                    raise ValueError
            except ValueError:
                graph_status.set(f"Variable {letter}: min must be less than max.")
                return "break"
            scale = variable_scales[letter]
            scale.configure(from_=minimum, to=maximum, resolution=max((maximum - minimum) / 500, 1e-9))
            try:
                current = float(variable_values[letter].get())
            except ValueError:
                current = minimum
            variable_values[letter].set(f"{max(minimum, min(maximum, current)):.12g}")
            schedule_graph()
            return "break"

        for index, letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
            row = index * 2 + 1
            tk.Label(variable_list, text=f"{letter} =", bg=background, fg=foreground).grid(
                row=row, column=0, sticky="e", padx=(6, 2), pady=(5, 0)
            )
            scale = tk.Scale(
                variable_list, from_=-10, to=10, resolution=0.04, orient=tk.HORIZONTAL,
                showvalue=False, variable=variable_values[letter], bg=background, fg=foreground,
                highlightthickness=0, troughcolor="#d0d0d0",
            )
            scale.grid(row=row, column=1, sticky="ew", pady=(5, 0))
            variable_scales[letter] = scale
            entry = tk.Entry(
                variable_list, textvariable=variable_values[letter], width=7,
                bg=background, fg=foreground, insertbackground=foreground,
            )
            entry.grid(row=row, column=2, sticky="ew", padx=(3, 6), pady=(5, 0))
            limits = tk.Frame(variable_list, bg=background)
            limits.grid(row=row + 1, column=1, columnspan=2, sticky="ew", padx=(0, 6), pady=(0, 3))
            tk.Label(limits, text="min", bg=background, fg=foreground).pack(side=tk.LEFT)
            minimum_entry = tk.Entry(
                limits, textvariable=variable_limits[letter]["min"], width=7,
                bg=background, fg=foreground, insertbackground=foreground,
            )
            minimum_entry.pack(side=tk.LEFT, padx=(2, 8))
            tk.Label(limits, text="max", bg=background, fg=foreground).pack(side=tk.LEFT)
            maximum_entry = tk.Entry(
                limits, textvariable=variable_limits[letter]["max"], width=7,
                bg=background, fg=foreground, insertbackground=foreground,
            )
            maximum_entry.pack(side=tk.LEFT, padx=2)
            for limit_entry in (minimum_entry, maximum_entry):
                limit_entry.bind("<Return>", lambda event, name=letter: apply_slider_bounds(name, event))
                limit_entry.bind("<FocusOut>", lambda event, name=letter: apply_slider_bounds(name, event))
        variable_list.columnconfigure(1, weight=1)

        range_bar = tk.Frame(plot_area, bg=background)
        range_bar.pack(fill=tk.X, padx=6, pady=(6, 2))
        for index, (label, variable) in enumerate(ranges.items()):
            tk.Label(range_bar, text=label, bg=background, fg=foreground).grid(row=0, column=index * 2)
            tk.Entry(range_bar, textvariable=variable, width=6, bg=background, fg=foreground).grid(
                row=0, column=index * 2 + 1, padx=(2, 6)
            )
        tk.Label(
            plot_area, text="Drag the graph to pan. Use the mouse wheel to zoom at the pointer.",
            bg=background, fg=foreground, anchor=tk.W,
        ).pack(fill=tk.X, padx=8)
        tk.Label(plot_area, textvariable=graph_status, bg=background, fg=foreground, anchor=tk.W).pack(
            fill=tk.X, padx=8
        )
        plot = tk.Canvas(plot_area, bg=background, highlightthickness=1, highlightbackground=foreground)
        plot.pack(fill=tk.BOTH, expand=True, padx=7, pady=(2, 7))

        def normalized_expression(text):
            text = text.strip()
            if "=" in text:
                left, right = text.split("=", 1)
                if left.strip().lower() != "y":
                    raise ValueError("Equations must use the form y = expression.")
                text = right.strip()
            return text

        def draw_graph(event=None):
            redraw_after["id"] = None
            plot.delete("all")
            width, height = max(2, plot.winfo_width()), max(2, plot.winfo_height())
            try:
                xmin, xmax, ymin, ymax = (
                    float(ranges[name].get()) for name in ("x min", "x max", "y min", "y max")
                )
                if not all(math.isfinite(value) for value in (xmin, xmax, ymin, ymax)):
                    raise ValueError("Ranges must be finite numbers.")
                if xmin >= xmax or ymin >= ymax:
                    raise ValueError("Minimum ranges must be less than maximum ranges.")
                if not math.isfinite(xmax - xmin) or not math.isfinite(ymax - ymin):
                    raise ValueError("Ranges are too large.")
                variables = read_variables()
            except ValueError as error:
                graph_status.set(f"Error: {error}")
                return "break"

            screen_x = lambda value: (value - xmin) / (xmax - xmin) * width
            screen_y = lambda value: height - (value - ymin) / (ymax - ymin) * height

            def grid_step(span):
                rough_step = span / 10
                magnitude = 10 ** math.floor(math.log10(rough_step))
                normalized = rough_step / magnitude
                if normalized <= 1:
                    multiplier = 1
                elif normalized <= 2:
                    multiplier = 2
                elif normalized <= 5:
                    multiplier = 5
                else:
                    multiplier = 10
                return multiplier * magnitude

            x_step, y_step = grid_step(xmax - xmin), grid_step(ymax - ymin)
            x_grid = math.ceil(xmin / x_step) * x_step
            while x_grid <= xmax + x_step * 1e-9:
                pixel = screen_x(x_grid)
                plot.create_line(pixel, 0, pixel, height, fill="#b0b0b0", dash=(2, 4))
                x_grid += x_step
            y_grid = math.ceil(ymin / y_step) * y_step
            while y_grid <= ymax + y_step * 1e-9:
                pixel = screen_y(y_grid)
                plot.create_line(0, pixel, width, pixel, fill="#b0b0b0", dash=(2, 4))
                y_grid += y_step
            if xmin <= 0 <= xmax:
                plot.create_line(screen_x(0), 0, screen_x(0), height, fill=foreground, width=2)
            if ymin <= 0 <= ymax:
                plot.create_line(0, screen_y(0), width, screen_y(0), fill=foreground, width=2)

            rendered, errors = 0, []
            for row_number, row in enumerate(graph_rows, 1):
                if not row["enabled"].get() or not row["expression"].get().strip():
                    continue
                try:
                    formula = normalized_expression(row["expression"].get())
                    validation_error = None
                    for sample_x in (xmin, (xmin + xmax) / 2, xmax):
                        try:
                            evaluate_calculator_expression(formula, sample_x, variables)
                            validation_error = None
                            break
                        except ValueError as error:
                            validation_error = error
                    if validation_error is not None:
                        raise validation_error
                except ValueError as error:
                    errors.append(f"{row_number}: {error}")
                    continue
                previous = None
                segment = []

                def flush_segment():
                    nonlocal rendered
                    if len(segment) >= 4:
                        plot.create_line(*segment, fill=row["color"], width=2, smooth=False)
                        rendered += 1
                    segment.clear()

                for pixel_x in range(width):
                    value_x = xmin + pixel_x / max(1, width - 1) * (xmax - xmin)
                    try:
                        value_y = float(evaluate_calculator_expression(formula, value_x, variables))
                        pixel_y = screen_y(value_y)
                        current = (pixel_x, pixel_y)
                        if previous is not None and abs(pixel_y - previous[1]) < height * 1.5:
                            if not segment:
                                segment.extend(previous)
                            segment.extend(current)
                        else:
                            flush_segment()
                        if -height * 2 <= pixel_y <= height * 3:
                            previous = current
                        else:
                            flush_segment()
                            previous = None
                    except (ValueError, OverflowError):
                        flush_segment()
                        previous = None
                flush_segment()
            if errors:
                graph_status.set("Error " + "; ".join(errors[:2]))
            else:
                graph_status.set(f"Rendered {sum(r['enabled'].get() for r in graph_rows)} expression(s).")
            return "break"

        def schedule_graph(*args):
            if redraw_after["id"] is not None:
                self.root.after_cancel(redraw_after["id"])
            redraw_after["id"] = self.root.after(16, draw_graph)

        pan_state = {"start": None}

        def current_ranges():
            values = tuple(float(ranges[name].get()) for name in ("x min", "x max", "y min", "y max"))
            if not all(math.isfinite(value) for value in values):
                raise ValueError
            return values

        def set_ranges(xmin, xmax, ymin, ymax):
            for name, value in zip(("x min", "x max", "y min", "y max"), (xmin, xmax, ymin, ymax)):
                ranges[name].set(f"{value:.12g}")

        def start_pan(event):
            try:
                pan_state["start"] = (event.x, event.y, *current_ranges())
                plot.configure(cursor="fleur")
            except ValueError:
                pan_state["start"] = None

        def pan_graph(event):
            if pan_state["start"] is None:
                return
            start_x, start_y, xmin, xmax, ymin, ymax = pan_state["start"]
            width, height = max(1, plot.winfo_width()), max(1, plot.winfo_height())
            x_shift = -(event.x - start_x) / width * (xmax - xmin)
            y_shift = (event.y - start_y) / height * (ymax - ymin)
            set_ranges(xmin + x_shift, xmax + x_shift, ymin + y_shift, ymax + y_shift)

        def finish_pan(event=None):
            pan_state["start"] = None
            plot.configure(cursor="crosshair")

        def zoom_graph(event):
            try:
                xmin, xmax, ymin, ymax = current_ranges()
            except ValueError:
                return "break"
            direction = event.delta if getattr(event, "delta", 0) else (1 if event.num == 4 else -1)
            factor = 0.8 if direction > 0 else 1.25
            width, height = max(1, plot.winfo_width()), max(1, plot.winfo_height())
            center_x = xmin + event.x / width * (xmax - xmin)
            center_y = ymax - event.y / height * (ymax - ymin)
            new_xmin = center_x + (xmin - center_x) * factor
            new_xmax = center_x + (xmax - center_x) * factor
            new_ymin = center_y + (ymin - center_y) * factor
            new_ymax = center_y + (ymax - center_y) * factor
            set_ranges(new_xmin, new_xmax, new_ymin, new_ymax)
            return "break"

        def cancel_redraw(event):
            if event.widget is plot and redraw_after["id"] is not None:
                self.root.after_cancel(redraw_after["id"])
                redraw_after["id"] = None

        def remove_expression(row):
            if len(graph_rows) == 1:
                row["expression"].set("")
            else:
                graph_rows.remove(row)
                row["frame"].destroy()
            schedule_graph()

        def cycle_color(row):
            index = (palette.index(row["color"]) + 1) % len(palette)
            row["color"] = palette[index]
            row["color_button"].configure(bg=row["color"], activebackground=row["color"])
            schedule_graph()

        def add_expression(initial=""):
            row = {
                "expression": tk.StringVar(value=initial),
                "enabled": tk.BooleanVar(value=True),
                "color": palette[len(graph_rows) % len(palette)],
            }
            frame = tk.Frame(expression_list, bg=background, relief=tk.GROOVE, bd=1)
            frame.pack(fill=tk.X, padx=3, pady=3)
            row["frame"] = frame
            tk.Checkbutton(
                frame, variable=row["enabled"], bg=background, command=schedule_graph,
                activebackground=background,
            ).pack(side=tk.LEFT)
            color_button = tk.Button(frame, width=2, bg=row["color"], activebackground=row["color"])
            color_button.configure(command=lambda item=row: cycle_color(item))
            color_button.pack(side=tk.LEFT, padx=(0, 3), pady=4)
            row["color_button"] = color_button
            entry = tk.Entry(
                frame, textvariable=row["expression"], bg=background, fg=foreground,
                insertbackground=foreground, font=("Courier New", 10),
            )
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=4)
            entry.bind("<Return>", draw_graph)
            entry.bind("<KeyRelease>", schedule_graph)
            tk.Button(frame, text="x", width=2, command=lambda item=row: remove_expression(item)).pack(
                side=tk.RIGHT, padx=3, pady=3
            )
            graph_rows.append(row)
            entry.focus_set()
            schedule_graph()

        footer = tk.Frame(expressions_tab, bg=background)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Button(footer, text="+ Add expression", command=add_expression).pack(
            side=tk.LEFT, padx=4, pady=4
        )
        tk.Button(footer, text="Plot", command=draw_graph).pack(side=tk.RIGHT, padx=4, pady=4)
        for variable in (*variable_values.values(), *ranges.values()):
            variable.trace_add("write", schedule_graph)
        plot.bind("<Configure>", schedule_graph)
        plot.bind("<Destroy>", cancel_redraw)
        plot.bind("<ButtonPress-1>", start_pan)
        plot.bind("<B1-Motion>", pan_graph)
        plot.bind("<ButtonRelease-1>", finish_pan)
        plot.bind("<MouseWheel>", zoom_graph)
        plot.bind("<Button-4>", zoom_graph)
        plot.bind("<Button-5>", zoom_graph)
        plot.configure(cursor="crosshair")
        add_expression("y = sin(x)")
        add_expression("y = x^2 / 5")
        display.focus_set()

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

    def open_ai_chat(self):
        """Open pyAI, the chat assistant backed by a local Ollama server."""
        surface_bg = self.preferences["surface_bg"]
        text_fg = self.preferences["text_fg"]
        font_size = self.preferences["font_size"]
        window = self.create_window("pyAI", width=640, height=520)
        window.min_width = 420
        window.min_height = 360

        state = {
            "phase": "checking",
            "closed": False,
            "cancel": threading.Event(),
            "history": [],
        }

        def client():
            return OllamaClient(self.preferences["ai_chat_url"])

        toolbar = tk.Frame(window.content, bg=surface_bg, relief=tk.RAISED, bd=1)
        toolbar.pack(fill=tk.X)
        tk.Label(toolbar, text="Model:", bg=surface_bg, fg=text_fg).pack(side=tk.LEFT, padx=(6, 2), pady=3)
        model_var = tk.StringVar(value=self.preferences["ai_chat_model"])
        model_entry = tk.Entry(
            toolbar, textvariable=model_var, width=16,
            bg=surface_bg, fg=text_fg, insertbackground=text_fg,
        )
        model_entry.pack(side=tk.LEFT, pady=3)

        transcript = scrolledtext.ScrolledText(
            window.content,
            wrap=tk.WORD,
            state=tk.DISABLED,
            bg=surface_bg,
            fg=text_fg,
            font=("Courier New", font_size),
            relief=tk.FLAT,
            padx=10,
            pady=8,
        )
        transcript.tag_configure("you", font=("Courier New", font_size, "bold"))
        transcript.tag_configure("system", font=("Courier New", font_size, "italic"))

        action_frame = tk.Frame(window.content, bg=surface_bg)
        action_label = tk.Label(
            action_frame, bg=surface_bg, fg=text_fg,
            wraplength=560, justify=tk.LEFT, anchor=tk.W,
        )
        action_button = tk.Button(action_frame, text="")
        retry_button = tk.Button(action_frame, text="Retry")

        input_frame = tk.Frame(window.content, bg=surface_bg)
        input_box = tk.Text(
            input_frame, height=3, wrap=tk.WORD,
            bg=surface_bg, fg=text_fg, insertbackground=text_fg,
            font=("Courier New", font_size), relief=tk.SOLID, bd=1,
        )
        input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 3), pady=4)
        send_button = tk.Button(input_frame, text="Send", width=6, state=tk.DISABLED)
        send_button.pack(side=tk.RIGHT, padx=(3, 6), pady=4)

        status_var = tk.StringVar(value="Checking Ollama...")
        status = tk.Label(window.content, textvariable=status_var, bg=surface_bg, fg=text_fg, anchor=tk.W)

        # Top-to-bottom pack order; action_frame collapses when its children hide.
        transcript.pack(fill=tk.BOTH, expand=True)
        action_frame.pack(fill=tk.X)
        input_frame.pack(fill=tk.X)
        status.pack(fill=tk.X, padx=6, pady=(0, 3))

        def alive():
            return not state["closed"] and transcript.winfo_exists()

        def append_transcript(text, tag=None):
            transcript.config(state=tk.NORMAL)
            if tag:
                transcript.insert(tk.END, text, tag)
            else:
                transcript.insert(tk.END, text)
            transcript.see(tk.END)
            transcript.config(state=tk.DISABLED)

        def hide_action():
            action_label.pack_forget()
            action_button.pack_forget()
            retry_button.pack_forget()

        def show_action(message, button_text=None, command=None, show_retry=True):
            hide_action()
            action_label.config(text=message)
            action_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=4)
            if show_retry:
                retry_button.config(command=run_check)
                retry_button.pack(side=tk.RIGHT, padx=(3, 6), pady=4)
            if button_text:
                action_button.config(text=button_text, command=command)
                action_button.pack(side=tk.RIGHT, padx=3, pady=4)

        def current_model():
            return model_var.get().strip() or "llama3.2"

        def set_ready():
            state["phase"] = "ready"
            hide_action()
            send_button.config(state=tk.NORMAL, command=send_message)
            status_var.set(f"{current_model()} — ready")
            input_box.focus_set()

        def handle_offline():
            if OllamaClient.binary_installed():
                state["phase"] = "not_running"
                status_var.set("Ollama is not running")
                show_action(
                    "Ollama is installed but not running. Start it, or run 'ollama serve' in a terminal.",
                    "Start Ollama", start_server,
                )
            else:
                state["phase"] = "not_installed"
                status_var.set("Ollama is not installed")
                show_action(
                    "pyAI needs Ollama, a free local AI runtime. Install it from ollama.com/download "
                    "(or re-run pyOS Setup, which can install it), then press Retry.",
                    "Get Ollama", lambda: webbrowser.open("https://ollama.com/download"),
                )

        def handle_models(names, model):
            wanted = model if ":" in model else f"{model}:"
            if any(installed == model or installed.startswith(wanted) for installed in names):
                set_ready()
            else:
                state["phase"] = "model_missing"
                status_var.set(f"Model {model} not downloaded")
                show_action(
                    f"The model '{model}' is not downloaded yet (about 2 GB for llama3.2). "
                    "Download it now? This is a one-time step.",
                    "Download model", start_pull,
                )

        def run_check():
            state["phase"] = "checking"
            hide_action()
            send_button.config(state=tk.DISABLED)
            status_var.set("Checking Ollama...")
            model = current_model()

            def worker():
                try:
                    names = client().list_models()
                except (urllib.error.URLError, OSError, ValueError):
                    self.root.after(0, lambda: alive() and handle_offline())
                    return
                self.root.after(0, lambda found=names: alive() and handle_models(found, model))

            threading.Thread(target=worker, daemon=True).start()

        def poll_server(attempts):
            def worker():
                try:
                    client().list_models()
                except (urllib.error.URLError, OSError, ValueError):
                    if attempts > 1:
                        self.root.after(1000, lambda: alive() and poll_server(attempts - 1))
                    else:
                        self.root.after(0, lambda: alive() and handle_offline())
                    return
                self.root.after(0, lambda: alive() and run_check())

            threading.Thread(target=worker, daemon=True).start()

        def start_server():
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
            except OSError as error:
                status_var.set(f"Could not start Ollama: {error}")
                return
            hide_action()
            status_var.set("Starting Ollama...")
            poll_server(10)

        def start_pull():
            model = current_model()
            hide_action()
            state["phase"] = "pulling"
            state["cancel"] = threading.Event()
            cancel_event = state["cancel"]
            status_var.set(f"Pulling {model}...")
            show_action(f"Downloading '{model}'. You can keep using the desktop meanwhile.",
                        "Cancel", cancel_event.set, show_retry=False)

            def on_progress(status_text, completed, total):
                if completed and total:
                    message = f"Pulling {model}: {int(completed * 100 / total)}%"
                else:
                    message = f"Pulling {model}: {status_text or '...'}"
                self.root.after(0, lambda text=message: alive() and status_var.set(text))

            def worker():
                try:
                    client().pull(model, on_progress, cancel_event)
                    error = None
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    error = str(exc)

                def done():
                    if not alive():
                        return
                    if cancel_event.is_set():
                        status_var.set("Download cancelled")
                        handle_models([], model)
                    elif error:
                        status_var.set(f"Download failed: {error}")
                        handle_models([], model)
                    else:
                        run_check()

                self.root.after(0, done)

            threading.Thread(target=worker, daemon=True).start()

        def finish_generation(tokens, error, cancel_event):
            if not alive():
                return
            reply = "".join(tokens)
            if reply:
                # A stopped partial reply is still valid conversation context.
                state["history"].append({"role": "assistant", "content": reply})
            if cancel_event.is_set():
                append_transcript(" [stopped]\n\n", "system")
            elif error:
                append_transcript(f"\n[error: {error}]\n\n", "system")
            else:
                append_transcript("\n\n")
            set_ready()

        def send_message(event=None):
            if state["phase"] != "ready":
                return "break"
            prompt = input_box.get("1.0", "end-1c").strip()
            if not prompt:
                return "break"
            input_box.delete("1.0", tk.END)
            model = current_model()
            state["history"].append({"role": "user", "content": prompt})
            append_transcript(f"YOU> {prompt}\n", "you")
            append_transcript(f"{model.upper()}> ")
            state["phase"] = "generating"
            state["cancel"] = threading.Event()
            cancel_event = state["cancel"]
            send_button.config(state=tk.DISABLED)
            status_var.set("Generating... (Stop below)")
            show_action("", "Stop", cancel_event.set, show_retry=False)
            tokens = []

            def on_token(text, done):
                if text:
                    tokens.append(text)
                    self.root.after(0, lambda chunk=text: alive() and append_transcript(chunk))

            def worker():
                try:
                    client().chat(model, list(state["history"]), on_token, cancel_event)
                    error = None
                except (urllib.error.URLError, OSError, ValueError) as exc:
                    error = str(exc)
                self.root.after(0, lambda: finish_generation(tokens, error, cancel_event))

            threading.Thread(target=worker, daemon=True).start()
            return "break"

        def new_chat():
            if state["phase"] in {"generating", "pulling"}:
                state["cancel"].set()
            state["history"] = []
            transcript.config(state=tk.NORMAL)
            transcript.delete("1.0", tk.END)
            transcript.config(state=tk.DISABLED)

        def apply_model(event=None):
            name = model_var.get().strip()
            if not name:
                model_var.set(self.preferences["ai_chat_model"])
                return
            if name != self.preferences["ai_chat_model"]:
                self.preferences["ai_chat_model"] = name
                self.save_preferences()
                if state["phase"] not in {"generating", "pulling"}:
                    run_check()

        def close_chat():
            state["closed"] = True
            state["cancel"].set()
            window.close()

        tk.Button(toolbar, text="New Chat", command=new_chat).pack(side=tk.LEFT, padx=6, pady=3)
        model_entry.bind("<Return>", apply_model)
        model_entry.bind("<FocusOut>", apply_model)
        input_box.bind("<Return>", send_message)
        input_box.bind("<Shift-Return>", lambda event: None)
        window.close_button.configure(command=close_chat)

        append_transcript(
            "pyAI runs a language model locally through Ollama.\n"
            "Nothing you type leaves this computer.\n\n", "system",
        )
        run_check()
        return window

    def open_dispenser(self):
        """Open the Dispenser, a dot matrix printer that prints sausage pixel art."""
        surface_bg = self.preferences["surface_bg"]
        text_fg = self.preferences["text_fg"]
        window = self.create_window("Dispenser", width=700, height=520)

        scale = 10
        art_width = len(DISPENSER_ARTWORK[0][0]) * scale
        line_height = 20

        scrollbar = tk.Scrollbar(window.content, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 4), pady=8)
        paper = tk.Canvas(
            window.content,
            bg=surface_bg,
            highlightthickness=0,
            yscrollcommand=scrollbar.set,
        )
        paper.pack(fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)
        scrollbar.configure(command=paper.yview)

        prompt = tk.Frame(window.content, bg=surface_bg, relief=tk.RAISED, bd=1)
        tk.Label(
            prompt,
            text="Sausage complete. Would you like another sausage?",
            bg=surface_bg,
            fg=text_fg,
        ).pack(side=tk.LEFT, padx=10, pady=8)

        state = {"cursor": 12, "photos": [], "last_rows": None}

        def show_bottom(extent):
            paper.configure(scrollregion=(0, 0, art_width, extent))
            paper.yview_moveto(1.0)

        def pick_rows():
            """Serve a random artwork, never the same sausage twice in a row."""
            choices = [rows for rows in DISPENSER_ARTWORK if rows is not state["last_rows"]]
            state["last_rows"] = random.choice(choices)
            return state["last_rows"]

        def print_art(on_done):
            rows = pick_rows()
            photo = tk.PhotoImage(width=art_width, height=len(rows) * scale)
            state["photos"].append(photo)
            width = paper.winfo_width()
            x = max(12, ((width if width > 1 else art_width + 24) - art_width) // 2)
            top = state["cursor"]
            paper.create_image(x, top, image=photo, anchor=tk.NW)
            job = {"row": 0}

            def tick():
                if not window.frame.winfo_exists():
                    return
                if job["row"] >= len(rows):
                    state["cursor"] = top + len(rows) * scale + line_height
                    show_bottom(state["cursor"])
                    on_done()
                    return
                colors = (DISPENSER_PALETTE[c] for c in rows[job["row"]])
                data = "{" + " ".join(" ".join([color] * scale) for color in colors) + "}"
                photo.put(data, to=(0, job["row"] * scale, art_width, (job["row"] + 1) * scale))
                job["row"] += 1
                show_bottom(top + job["row"] * scale + line_height)
                self.root.after(45, tick)

            tick()

        def print_text(message, on_done):
            item = paper.create_text(
                12, state["cursor"], anchor=tk.NW, fill=text_fg,
                font=("Courier New", 10), text="",
            )
            job = {"shown": 0}

            def tick():
                if not window.frame.winfo_exists():
                    return
                job["shown"] += 2
                paper.itemconfigure(item, text=message[:job["shown"]])
                show_bottom(state["cursor"] + line_height)
                if job["shown"] >= len(message):
                    state["cursor"] += line_height
                    on_done()
                else:
                    self.root.after(25, tick)

            tick()

        def show_prompt():
            prompt.pack(side=tk.BOTTOM, fill=tk.X, before=scrollbar)

        def another_sausage():
            prompt.pack_forget()
            paper.delete("all")
            state["photos"].clear()
            state["cursor"] = 12
            show_bottom(1)
            print_art(show_prompt)

        def refuse_sausage():
            prompt.pack_forget()
            print_text(
                "You selected No, but everyone loves sausages, so here's another one.",
                lambda: print_art(show_prompt),
            )

        tk.Button(prompt, text="No", width=6, command=refuse_sausage).pack(side=tk.RIGHT, padx=(4, 10), pady=6)
        tk.Button(prompt, text="Yes", width=6, command=another_sausage).pack(side=tk.RIGHT, padx=4, pady=6)

        print_art(show_prompt)
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

        tk.Label(toolbar, text="Search or URL:", bg="white", fg="black").pack(
            side=tk.LEFT, padx=(6, 2), pady=5
        )
        url_var = tk.StringVar(value="https://example.com")
        url_entry = tk.Entry(toolbar, textvariable=url_var)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4, pady=5)

        status_var = tk.StringVar(value="Ready")
        javascript_enabled = tk.BooleanVar(value=False)
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
                    subprocess.check_call([
                        sys.executable, "-m", "pip", "install", "tkinterweb[javascript]>=4.25,<5.0"
                    ])
                    from tkinterweb import HtmlFrame
                    return HtmlFrame
                except Exception as e:
                    renderer_error["message"] = str(e)
                    return None

        HtmlFrame = get_html_frame_class()
        if HtmlFrame:
            html_frame = HtmlFrame(
                browser_area, messages_enabled=False, javascript_enabled=False,
                crash_prevention_enabled=True,
            )
            html_frame._skip_font_apply = True
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
            return browser_input_to_url(raw_url)

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
                        javascript_state = "on" if javascript_enabled.get() else "off"
                        update_network_status(
                            f"Rendered {final_url} | HTML/CSS active | JavaScript {javascript_state}"
                        )
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

        def toggle_javascript():
            html_frame = renderer["html_frame"]
            if not html_frame:
                javascript_enabled.set(False)
                status_var.set("JavaScript requires the HTML renderer.")
                return
            if javascript_enabled.get():
                try:
                    import pythonmonkey  # noqa: F401
                except ImportError:
                    javascript_enabled.set(False)
                    messagebox.showerror(
                        "JavaScript",
                        "PythonMonkey is not installed. Run setup again to install JavaScript support.",
                        parent=self.root,
                    )
                    return
                accepted = messagebox.askokcancel(
                    "Enable Experimental JavaScript?",
                    "JavaScript support is experimental and the available DOM is incomplete. "
                    "Scripts from websites can be malicious and may expose data or affect pyOS.\n\n"
                    "Only enable JavaScript for websites you trust. Continue?",
                    parent=self.root,
                )
                if not accepted:
                    javascript_enabled.set(False)
                    return
            try:
                html_frame.configure(javascript_enabled=javascript_enabled.get())
                if javascript_enabled.get():
                    html_frame.javascript.eval("""
                        for (const name of [
                            "python", "require", "module", "exports", "__filename", "__dirname"
                        ]) {
                            try { globalThis[name] = undefined; } catch (error) {}
                            try {
                                Object.defineProperty(globalThis, name, {
                                    value: undefined, writable: false, configurable: false
                                });
                            } catch (error) {}
                        }
                    """)
                status_var.set(f"JavaScript {'enabled' if javascript_enabled.get() else 'disabled'}.")
                if page_cache["url"]:
                    load_url()
            except Exception as error:
                javascript_enabled.set(False)
                try:
                    html_frame.configure(javascript_enabled=False)
                except Exception:
                    pass
                status_var.set(f"Could not change JavaScript mode: {error}")

        tk.Button(toolbar, text="Go", command=load_url).pack(side=tk.LEFT, padx=4, pady=4)
        javascript_toggle = tk.Checkbutton(
            toolbar, text="JavaScript", variable=javascript_enabled, command=toggle_javascript,
            bg="white", fg="black",
        )
        javascript_toggle.pack(side=tk.LEFT, padx=4, pady=4)
        if not renderer["html_frame"]:
            javascript_toggle.configure(state=tk.DISABLED)
        tk.Button(toolbar, text="Inspect", command=inspect_page).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Save Page", command=save_page).pack(side=tk.LEFT, padx=4, pady=4)
        tk.Button(toolbar, text="Network", command=lambda: update_network_status("Network status")).pack(
            side=tk.LEFT,
            padx=4,
            pady=4,
        )
        url_entry.bind("<Return>", load_url)
        if renderer["html_frame"]:
            update_network_status("Ready | HTML/CSS renderer active | JavaScript off")
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
        font_family = tk.StringVar(value=self.preferences["font_family"])
        clock_24h = tk.BooleanVar(value=self.preferences["clock_24h"])
        seconds = tk.BooleanVar(value=self.preferences["show_seconds"])
        hidden = tk.BooleanVar(value=self.preferences["show_hidden_files"])
        start_location = tk.StringVar(value=self.preferences["file_manager_start"])
        notifications_enabled = tk.BooleanVar(value=self.preferences["notifications_enabled"])
        tips_enabled = tk.BooleanVar(value=self.preferences["tips_enabled"])
        status = tk.StringVar()

        notebook = ttk.Notebook(window.content)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        appearance = tk.Frame(notebook, bg="white")
        clock = tk.Frame(notebook, bg="white")
        files = tk.Frame(notebook, bg="white")
        security = tk.Frame(notebook, bg="white")
        notifications = tk.Frame(notebook, bg="white")
        notebook.add(appearance, text="Appearance")
        notebook.add(clock, text="Clock")
        notebook.add(files, text="Files")
        notebook.add(security, text="Security")
        notebook.add(notifications, text="Notifications")

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
        family_row = tk.Frame(appearance, bg="white")
        family_row.pack(fill=tk.X, padx=16, pady=(2, 6))
        tk.Label(family_row, text="Text font:").pack(side=tk.LEFT)
        available_fonts = sorted(set(tkfont.families(self.root)), key=str.casefold)
        ttk.Combobox(
            family_row, textvariable=font_family, values=available_fonts,
            state="readonly", width=28,
        ).pack(side=tk.LEFT, padx=10)

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

        tk.Label(
            notifications, text="DESKTOP NOTIFICATIONS", font=("Courier New", 11, "bold"), anchor=tk.W
        ).pack(fill=tk.X, padx=16, pady=(18, 8))
        tk.Checkbutton(
            notifications, text="Enable system notifications", variable=notifications_enabled,
            anchor=tk.W,
        ).pack(fill=tk.X, padx=16, pady=5)
        tk.Checkbutton(
            notifications, text="Show pyOS tips", variable=tips_enabled, anchor=tk.W,
        ).pack(fill=tk.X, padx=16, pady=5)
        tk.Label(
            notifications,
            text="Turning off system notifications also suppresses tips.\nChanges apply after pressing Apply.",
            justify=tk.LEFT, anchor=tk.W,
        ).pack(fill=tk.X, padx=16, pady=10)

        account_name = tk.StringVar(value=get_username() or "Not configured")
        tk.Label(security, text="PYOS ACCOUNT", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(18, 8)
        )
        tk.Label(security, textvariable=account_name, anchor=tk.W).pack(fill=tk.X, padx=16, pady=4)
        tk.Label(
            security,
            text="Changing the account requires the current password.\nThe new credentials apply to both the desktop and CLI.",
            justify=tk.LEFT,
            anchor=tk.W,
        ).pack(fill=tk.X, padx=16, pady=8)

        def change_account_settings():
            username = change_credentials_dialog(self.root)
            if username:
                self.username = username
                if self.messenger_service:
                    self.messenger_service.username = username
                account_name.set(username)
                status.set("Account credentials changed.")

        tk.Button(security, text="Change Username / Password", command=change_account_settings).pack(
            anchor=tk.W, padx=16, pady=5
        )
        tk.Button(security, text="Lock Desktop Now", command=self.lock_desktop).pack(
            anchor=tk.W, padx=16, pady=5
        )

        passkey_state = tk.StringVar()
        tk.Label(security, text="WINDOWS HELLO PASSKEY", font=("Courier New", 11, "bold"), anchor=tk.W).pack(
            fill=tk.X, padx=16, pady=(18, 5)
        )
        tk.Label(security, textvariable=passkey_state, justify=tk.LEFT, anchor=tk.W, wraplength=540).pack(
            fill=tk.X, padx=16, pady=4
        )
        passkey_buttons = tk.Frame(security, bg="white")
        passkey_buttons.pack(fill=tk.X, padx=16, pady=5)

        def refresh_passkey_state():
            available, reason = passkey_support_status()
            registered = has_passkey()
            if registered:
                passkey_state.set("Registered. Use 'Use Passkey' on the lock screen. Password fallback remains available.")
            else:
                passkey_state.set(reason if not available else "No passkey registered.")
            add_passkey_button.configure(state=tk.NORMAL if available and not registered else tk.DISABLED)
            remove_passkey_button.configure(state=tk.NORMAL if registered else tk.DISABLED)

        def add_passkey():
            try:
                if register_passkey_dialog(self.root) is not None:
                    status.set("Windows Hello passkey registered.")
            except Exception as error:
                messagebox.showerror("Passkey", f"Could not register passkey: {error}", parent=self.root)
            refresh_passkey_state()

        def remove_passkey():
            try:
                if remove_passkeys_dialog(self.root):
                    status.set("Passkey removed from pyOS. Your password remains active.")
            except ValueError as error:
                messagebox.showerror("Passkey", str(error), parent=self.root)
            refresh_passkey_state()

        add_passkey_button = tk.Button(passkey_buttons, text="Register Windows Hello Passkey", command=add_passkey)
        add_passkey_button.pack(side=tk.LEFT)
        remove_passkey_button = tk.Button(passkey_buttons, text="Remove Passkey", command=remove_passkey)
        remove_passkey_button.pack(side=tk.LEFT, padx=6)
        refresh_passkey_state()

        tk.Label(
            security, text="DANGER ZONE", font=("Courier New", 11, "bold"),
            anchor=tk.W, fg="#990000",
        ).pack(fill=tk.X, padx=16, pady=(18, 5))
        tk.Label(
            security,
            text="Permanently remove all pyOS user data and virtual drives while keeping the program and packages.",
            justify=tk.LEFT, anchor=tk.W, wraplength=540,
        ).pack(fill=tk.X, padx=16, pady=4)
        tk.Button(
            security, text="Uninstall pyOS...", command=self.uninstall_pyos,
            fg="#990000",
        ).pack(anchor=tk.W, padx=16, pady=5)

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
                "font_family": font_family.get() or "Courier New",
                "clock_24h": bool(clock_24h.get()),
                "show_seconds": bool(seconds.get()),
                "show_hidden_files": bool(hidden.get()),
                "file_manager_start": start_location.get(),
                "notifications_enabled": bool(notifications_enabled.get()),
                "tips_enabled": bool(tips_enabled.get()),
            })
            self.apply_preferences()
            self.apply_notification_preferences()
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
            font_family.set("Courier New")
            clock_24h.set(True)
            seconds.set(True)
            hidden.set(False)
            start_location.set("Home")
            notifications_enabled.set(True)
            tips_enabled.set(True)
            apply_settings()

        footer = tk.Frame(window.content, bg="white", relief=tk.RAISED, bd=1)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Label(footer, textvariable=status, anchor=tk.W).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)
        tk.Button(footer, text="Defaults", command=restore_defaults).pack(side=tk.RIGHT, padx=4, pady=5)
        tk.Button(footer, text="Close", command=window.close).pack(side=tk.RIGHT, padx=4, pady=5)
        tk.Button(footer, text="Apply", command=apply_settings).pack(side=tk.RIGHT, padx=4, pady=5)

    def show_about(self):
        """Show current pyOS information in an embedded desktop window."""
        window = self.create_window("About pyOS", width=720, height=520)
        surface = self.preferences["surface_bg"]
        foreground = self.preferences["text_fg"]

        header = tk.Frame(window.content, bg=self.preferences["chrome_bg"], height=88)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        about_title = tk.Label(
            header, text="pyOS", font=("Courier New", 24, "bold"),
            bg=self.preferences["chrome_bg"], fg=self.preferences["chrome_fg"],
        )
        about_title._keep_font = True
        about_title.pack(anchor=tk.W, padx=18, pady=(12, 0))
        tk.Label(
            header, text="Python Desktop Environment",
            bg=self.preferences["chrome_bg"], fg=self.preferences["chrome_fg"],
        ).pack(anchor=tk.W, padx=20)

        notebook = ttk.Notebook(window.content)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        overview = tk.Frame(notebook, bg=surface)
        applications = tk.Frame(notebook, bg=surface)
        privacy = tk.Frame(notebook, bg=surface)
        system = tk.Frame(notebook, bg=surface)
        notebook.add(overview, text="Overview")
        notebook.add(applications, text="Applications")
        notebook.add(privacy, text="Security & Privacy")
        notebook.add(system, text="System")

        overview_text = (
            "pyOS is a Python and Tkinter desktop environment with movable, resizable, "
            "minimizable applications and pixel-art desktop launchers.\n\n"
            "The desktop includes persistent appearance settings, installed-font selection, "
            "notifications, authentication, Windows Hello passkeys where supported, an "
            "Command Center, and directory-backed virtual drives.\n\n"
            "App windows are constrained to the usable desktop so their title bars and "
            "controls remain reachable."
        )
        tk.Label(
            overview, text=overview_text, justify=tk.LEFT, anchor=tk.NW,
            wraplength=640, bg=surface, fg=foreground,
        ).pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        apps_text = (
            "CORE\nFile Manager • Text Editor • Sticky Notes • Internet Browser\n"
            "Image Viewer • Media Player • Python IDE • Calculator\n\n"
            "CONNECTED\nWeather • News • LAN Peer-to-Peer Messenger\n\n"
            "CREATE AND CUSTOMIZE\nModding Environment • App Maker • Virtual Drive Manager\n\n"
            "ENTERTAINMENT\nSnake • Sudoku • Automated Chess\n\n"
            "Apps made with App Maker run in pyOS windows and automatically receive "
            "their own desktop launchers."
        )
        tk.Label(
            applications, text=apps_text, justify=tk.LEFT, anchor=tk.NW,
            wraplength=640, bg=surface, fg=foreground,
        ).pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        privacy_text = (
            "• Account passwords use salted PBKDF2-SHA256 hashes.\n"
            "• Passkey private keys remain with Windows Hello.\n"
            "• Messenger is LAN-only and is not end-to-end encrypted.\n"
            "• Weather's My Location feature uses IP-based approximate geolocation.\n"
            "• Weather and News contact external data providers.\n"
            "• App Maker apps are unrestricted Python and should only be run when trusted.\n"
            "• Settings > Security can remove pyOS data and virtual drives while preserving modules and packages."
        )
        tk.Label(
            privacy, text=privacy_text, justify=tk.LEFT, anchor=tk.NW,
            wraplength=640, bg=surface, fg=foreground,
        ).pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        config = load_config()
        system_text = (
            f"User: {self.username or get_username() or 'Not signed in'}\n"
            f"Python: {platform.python_version()}\n"
            f"Platform: {platform.platform()}\n"
            f"Tk: {tk.TkVersion}\n\n"
            f"Installation: {Path(config['install_dir']).expanduser()}\n"
            f"Data: {Path(config['data_dir']).expanduser()}\n"
            f"Drive B: {get_drive_b_dir(create=False)}\n"
            f"Custom apps: {self._custom_apps_directory()}\n\n"
            "Built with Python and Tkinter."
        )
        tk.Label(
            system, text=system_text, justify=tk.LEFT, anchor=tk.NW,
            wraplength=640, bg=surface, fg=foreground,
        ).pack(fill=tk.BOTH, expand=True, padx=16, pady=16)

        footer = tk.Frame(window.content, bg=surface)
        footer.pack(fill=tk.X, padx=10, pady=(0, 10))
        tk.Button(footer, text="Settings", command=self.open_settings).pack(side=tk.LEFT)
        tk.Button(footer, text="Close", command=window.close).pack(side=tk.RIGHT)
    
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
        context_menu.add_command(label="Open Calculator", command=self.open_calculator)
        context_menu.add_command(label="Open Messenger", command=self.open_messenger)
        context_menu.add_command(label="Open Games Suite", command=self.open_games_suite)
        context_menu.add_command(label="Open Modding Environment", command=self.open_modding_environment)
        context_menu.add_command(label="Manage Virtual Drives", command=self.open_virtual_drive_manager)
        context_menu.add_command(label="Open Weather", command=self.open_weather)
        context_menu.add_command(label="Open News", command=self.open_news)
        context_menu.add_separator()
        context_menu.add_command(label="Lock Desktop", command=self.lock_desktop)
        context_menu.add_command(label="Refresh", command=self.refresh_desktop)
        context_menu.add_command(label="About", command=self.show_about)
        
        context_menu.post(event.x_root, event.y_root)
    
    def refresh_desktop(self):
        """Refresh desktop"""
        messagebox.showinfo("Refresh", "Desktop refreshed!")


# Dispenser artwork: 48x32 pixel-art sausage scenes, one char per pixel,
# rendered through DISPENSER_PALETTE by open_dispenser.
DISPENSER_PALETTE = {
    ".": "#f2ecdc",
    "w": "#ffffff",
    "0": "#141317",
    "K": "#33180d",
    "B": "#5f2a12",
    "b": "#7d3c18",
    "r": "#9c4f1e",
    "o": "#b96530",
    "h": "#d98a4e",
    "H": "#f0b476",
    "R": "#c22321",
    "Y": "#e0a512",
    "y": "#f4cf47",
    "n": "#c98f4b",
    "N": "#e8bd7d",
    "e": "#f7f1df",
    "E": "#f2b21b",
    "v": "#6f9c2c",
    "G": "#26251f",
    "g": "#4a4a48",
    "m": "#8b8b8b",
    "M": "#c9c9c4",
    "S": "#dedbcd",
    "F": "#e6541c",
    "f": "#f7952b",
    "W": "#ffe8a3",
    "c": "#8fd3f0",
    "C": "#4ba3d8",
    "u": "#2a6fb0",
    "p": "#f4a25b",
    "P": "#e2643c",
    "q": "#a03a56",
    "Q": "#5c2a5e",
    "D": "#221a3e",
    "d": "#37306b",
    "*": "#f7f3d0",
    "s": "#ffd94a",
    "t": "#3f7d3a",
    "T": "#295c2d",
    "a": "#2e6e8e",
    "A": "#7fc4d8",
    "k": "#6b4a2f",
    "l": "#8a6a43",
    "x": "#5a5566",
    "X": "#38344a",
    "z": "#c7b9a5",
    "1": "#f0c29a",
    "2": "#d99b6b",
    "3": "#b06f44",
    "4": "#8a5a33",
    "5": "#5e3a20",
    "L": "#d95f5f",
    "i": "#f6ead2",
}

DISPENSER_ARTWORK = (
    (
        "................................................",
        "................................................",
        "................................................",
        "................................................",
        "................................................",
        "................................................",
        "................................................",
        "........................S.......................",
        ".......................S........................",
        "................S......S........S...............",
        "...............S........S......S................",
        "...............S.........S.....S................",
        "................S........S......S...............",
        ".................S......S........S..............",
        ".................S...............S..............",
        "................S...............S...............",
        "................................................",
        "...........KKKKK................................",
        ".........KKKKKKKKKKKKKKKKKKKKKKK................",
        ".........KHhhrhhhhhrrhhhhhhKKKKKKKKKKK..........",
        "kkkkkkkkkKHHHooHHHHHooHHHHorhhhhhrrhhKKkkkkkkkkk",
        "llllllBKKKhhhhrrhhhhhooHHHHooHHHHHoHHHKwwlllllll",
        "lllllllwwKooooobboooohrrhhhhrrhhhhhhhHKwKBllllll",
        "llllllwwwKrrrrrobbooooobooooobboooooohKKwwwlllll",
        "lllllMMwwKKbrrrrrBBrrrrrBrrrrrBBooooooKwwwMMllll",
        "kkkkkkMMwwKKKvKKKKKKKbbbBBbbbrrBBrrrrrKwwMMkkkkk",
        "lllllllMMMwwvvwwKKKKKKKKKKKKKKKKKKKvvKKMMMllllll",
        "llllllllMMMMMMwwwwwwwwwwwwwwwwwwKKKKKMMMMlllllll",
        "llllllllllMMMMMMMMMMMMMMwMMMMMMMMMMMMMMlllllllll",
        "llllllllllllllMMMMMMMMMMMMMMMMMMMMMlllllllllllll",
        "kkkkkkkkkkkkkkkkkkkkkkkkMkkkkkkkkkkkkkkkkkkkkkkk",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
    ),
    (
        "................................................",
        "...............S...............S................",
        "..............S...............S.................",
        "..............S...............S.................",
        "...............S...............S................",
        "................S...............S...............",
        "................S...............S...............",
        "...............S...............S................",
        "................................................",
        "................................................",
        "................................................",
        "................................................",
        "........KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKK........",
        "......KKyhhhhhhhyhhhhhhhyhhhhhhhyhhhhhhhKK......",
        "......KHYyHHHHHyYyHHHHHyYyHHHHHyYyHHHHHyHK......",
        "......KHHYyHHHyYHYyHHHyYHYyHHHyYHYyHHHyYHK......",
        "...BKKKhhhYyhyYhhhYyhyYhhhYyhyYhhhYyhyYhhKKKB...",
        "......KooooYyYoooooYyYoooooYyYoooooYyYoooK......",
        "......KrrrrrYrrrrrrrYrrrrrrrYrrrrrrrYrrrrK......",
        "......KKbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbKK......",
        "........KKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKKn.......",
        "........nnNnnNnnNnnNnnNnnNnnNnnNnnNnnNnnn.......",
        ".......nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn......",
        ".......knnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnk......",
        "........nnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnnn.......",
        ".........nknnnnnnnnnnnnnnnnnnnnnnnnnnnkn........",
        "LLLLwwwwLLLkkknnnnnnnnnnnnnnnnnnnnnkkkwwLLLLwwww",
        "LLLLwwwwLLLLwwkkkkkkkkkknkkkkkkkkkkLwwwwLLLLwwww",
        "wwwwLLLLwwwwLLLLwwwwLLLLkwwwLLLLwwwwLLLLwwwwLLLL",
        "wwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLL",
        "wwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLL",
        "wwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLLwwwwLLLL",
    ),
    (
        "000000000000000000000000000000000000000000000000",
        "000000000000000000000000000000000000000000000000",
        "000000000000000000000000000000M00000000000000000",
        "00000000000000M00000000000000M000000000000000000",
        "0000000000000M000000000000000M000000000000000000",
        "0000000000000M0000000000000000M00000000000000000",
        "00000000000000M0000000000000000M0000000000000000",
        "G0G0G0G0G0G0G0GMG0G0G0G0G0G0G0GMG0G0G0G0G0G0G0G0",
        "GGGGGGGGGGGGGGGMGGGGGGGGGGGGGGMGGGGGGGGGGGGGGGGG",
        "GGGGGGGGGGGGGGMGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
        "GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG",
        "0000000000000000000000000000000000KKKK0000000000",
        "000g0000000g00KKKKKKKKKKKKKKKKKKKKKKKKK0000g0000",
        "000g00000KKKKKKKKKKKhhhhhhhhhhhhhrrhhhhK000g0000",
        "mmmgmmmmKKhhrrhhhhhroHHHHHHooHHHHHooHHHKmmmgmmmm",
        "ggggggggKHHHHooHHHHHooHHHHHHrrhhhhhrhhhKKKBggggg",
        "000g0BK0KhhhhhrrhhhhhrrhhhhhobbooooooooK000g0000",
        "000g000KKoooooobbooooobbbooooobboorrrrrK000g0000",
        "000g0000KooooorrBBrrrrrBBBrrrrrBBrrrrrKK000g0000",
        "000g0000KrrrrrrrrBBbbbbbbBBbKKKKKKKKKKK0000g0000",
        "mmmgmmmmmKKKKKKKKKKKKKKKKKKKKKKKKKmgmmmmmmmgmmmm",
        "ggggggggggKKKKgggggggggggggggggggggggggggggggggg",
        "00Wg0000WW0g000WW00g000W000g00W0000gW000000gW000",
        "0WWW0000Wf00000WW00000Wf00000WfW0000fW00000fWW00",
        "0fff0000ff00000ff00000ff0000Wfff0000ffW0000fff00",
        "Wfff0000ffWW000ffW000WffW000ffff0000fff0000fff00",
        "ffff000Wffff00Wfff000ffff00Wffff000Wfff00WWfffW0",
        "fFFFW0WfFFff00fFFfW0WfFFfWWffFFFW00fFFfW0ffFFFf0",
        "FFFFf0ffFFFF00fFFFf0fFFFFfffFFFFf0ffFFFf0ffFFFf0",
        "FFFFFfFFFFFFffFFFFFfFFFFFFFFFFFFFffFFFFFfFFFFFFf",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
    ),
    (
        "QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQQQQQQQyQQQQQQQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQQQQQQQyQQQQQQQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQyQQQQQQQQQQQyQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQQyQQQQsQQQQyQQQQQQQQQQQQQQQQQQ",
        "qQqQqQqQqQqQqQqQqQqsssssssssssqQqQqQqQqQqQqQqQqQ",
        "qqqqqqqqqqqqqqqqqqsssssssssssssqqqqqqqqqqqqqqqqq",
        "qqqqqqqqqqqqqyqqssssssssWssssssssqqyqqqqqqqqqqqq",
        "qqqqqqqqqqqqqqysssssWWWWWWWWWsssssyqqqqqqqqqqqqq",
        "qqqqqqqqqqqqqqqssssWWWWWWWWWWWssssqqqqqqqqqqqqqq",
        "qPqPqPqPqPqPqPssKKKKKKKKKKKKKKKKsssPqPqPqPqPqPqP",
        "PPPPPPPPPPPPPPsKhhhrrhhhhhrrhhhhKssPPPPPPPPPPPPP",
        "PPPPPPPPPPPPPPsKHHHHooHHHHHooHHHKssPPPPPPPPPPPPP",
        "PPPPPPPPPPyyBKKKhhhhhrrhhhhhrrhhKKKBPyyPPPMPPPPP",
        "PPPPPPPPPPPPPPsKoooooobbooooobooKssPPPPPPMMMPPPP",
        "pPpPpPpMpPpPpPsKrrrrrrrBBrrrrrrrKssPpPpPMMMMMPpP",
        "ppppppMMMpppppssKKKKKKKKKKKKKKKKsssppppXXXXXXXpp",
        "pppppMMMMMpppppssssWWWWWWWWWWWsssspppXXXXXXXXXXX",
        "pppXXXXXXXXXpppsssssWWWWWWWWWsssssppXXXXXXXXXXXX",
        "ppXXXXXXXXXXXpppssssssssWssssssssppXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXspssssssssssssssspsXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXXssssssssssssssssXXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXXXXsssssssssssssXXXXXXXXXXXXXXXXX",
        "XXXXXXXXXXXXXXXXXXXsssssssssssXXXXXXXXXXXXXXXXXX",
        "ttttttTttttttTttttttTttttttTttttttTttttttTtttttt",
        "tTttttttTttttttTttttttTttttttTttttttTttttttTtttt",
        "tttTttttttTttttttTttttttTttttttTttttttTttttttTtt",
        "tttttTttttttTttttttTttttttTttttttTttttttTttttttT",
        "TttttttTttttttTttttttTttttttTttttttTttttttTttttt",
        "ttTttttttTttttttTttttttTttttttTttttttTttttttTttt",
        "ttttTttttttTttttttTttttttTttttttTttttttTttttttTt",
        "ttttttTttttttTttttttTttttttTttttttTttttttTtttttt",
    ),
    (
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD*DDDDDDDD*",
        "DDDDDDDDDDDDDDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDD*DDD*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDMMDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDMMDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD*D*D*DDD",
        "DDDDwMDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDKKKKKKKDDDD*DDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDKhhhhhKKKKKKKDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDBKDKHHHHHrrhhhhKKKKKKDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDKKhhhHHHooHHHhrrhhKKKDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDKooohhhhroHHHHooHhhhKDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDKrooboooorhhhhhoHHHHKD*DDDDDDDDDDDD",
        "DDDDDDDDDDDDDKrrrBrrrooboooohrhhhKDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDD*KKKbBbrrrBBroooboooKKKBDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDKKKKKKbbBBrrrrrroKDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDKKKKKKKbbbrrKDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDKKKKKKKDDDDDDd*DDDDDDD",
        "DD*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDdddudddDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDddCuuuuuuddDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDMDDDDdCCCCCuuuuuddDD",
        "DDDDDDDD*DDDDDDDDDDDDDDDDDDDDMMMMCCCCCCCuuuuudDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDD*DDDDDDdCCCCCuuuuuudDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDduuuCuuuuuuuuudD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDduuuuuuuuuuudMM",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDduuuuuuuCuuudDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDD*DDDDDDDdduuuuuuuuuddDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDdduuuuuuuddDDD",
        "DDDDDDDDDDDD*DDDD*DDDDD*DDDDDDDDDD*DdddudddDDDDD",
        "DDDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDdDDDDDDDD",
    ),
    (
        "DDD*DDDDD*D*DDDDDDDDXDDDDDDDDDDDD*DyWy***DDDXDDD",
        "DDDDDDDDDDDD*DDXXXXXXXXXXXDDDDDDDDyWyDDXXXXXXXXX",
        "DDDDDDXDDDDDDDXXXXXXXXXXXXXD**DDDyWyDDXXXXXXXXXX",
        "DXXXXXXXXXXXDDDXXXXXXXXXXXDDDXXXXXXyWyXXXXXXXXXX",
        "XXXXXXXXXXXXXDDDDDDDXDDDDDDDXXXXXXyWyXXXXDDDXDD*",
        "*XXXXXXXXXXXDX*DDDDDDDDDDDDDXXXXyWyXXXXXDDDDDDDD",
        "DQDQDQXQXXXXXXXXXXXQDQDXXXXXXXXyWyXQDQDQDQDQDQDQ",
        "QQQQQQQXXXXXXXXXXXXXQQXX*XXXXXXXXyWyQQQQQQQQQQQQ",
        "QQQQQQQQXXXXXXXXXXXQQQQXXXXXXXyWyXQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQXQQQQQ*QQQQQQQQyWyQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQQQQQQQKQQyW*QQQQQQQQQQQQQQQQQQ",
        "QQQQQQQQQQQQQQQQQQQQQKKKKKKKQQQQQQQQQQQQQQQQQQQQ",
        "QqQqQqQqQqQqQqQqQqQqQKHHhorKQqQqQqQqQqQqQqQqQqQq",
        "qqqqqqqqqqqqqqqqqqqqKhHHhorbKqqqqqqqqqqqqqqqqqqq",
        "qqqqqqqqqqqqqqqqqqqqKhHHhorbKqqqqqqqqqqqqqqqqqqq",
        "qqqqqqqqqqqqqqqqqqqqKhHHhorbKqqqqqqqqqqqqqqqqqqq",
        "xxxxxxxxxxxxxxxxxxxxKhHHhorbKxxxxxxxxxxxxxxxxxxx",
        "xxxxxxxxxxxxxxxxxxxxKhHHhorbKxxxxxxxxxxxxxxxxxxx",
        "XxxXxxXxxXxxXxxXxxXxKhHHhorbKxXxxXxxXxxXxxXxxXxx",
        "xxxxxxxxxxxxxxxxxxxxKhHHhorbKxxxxxxxxxxxxxxxxxxx",
        "xxxxxxxxxxxxxxxxxxxxKhHHhorbKxxxxxxxxxxxxxxxxxxx",
        "xXxxXxxXxxXxxXxxXxxXKhHHhorbKxxXxxXxxXxxXxxXxxXx",
        "xxxxxxxxxxxxxxxxxxxxKhHHhorbKxxxxxxxxxxxxxxxxxxx",
        "xxxxxxxxxxxxxxxxXXXXKhHHhorbKXXXXxxxxxxxxxxxxxxx",
        "xxXxxXxxXxxXxXXXzzzzzKHHhorKzzzzzXXXxxXxxXxxXxxX",
        "xxxxxxxxxxxxXzzzzzzzzKKKKKKKzzzzzzzzXxxxxxxxxxxx",
        "xxxxxxxxxxxXzzzzzzzzzXzzzzzXzzzzzzzzzXxxxxxxxxxx",
        "XxxXxxXxxXXzzzzzzzzzzzzzXzzzzzxzzzzzzzXXxxXxxXxx",
        "xxxxxxxxxxxXzzzzzzxzzzzzzzzzzzzzzzzzzXxxxxxxxxxx",
        "xxxxxxxxxxxxXzzzzzzzzzzzzxzzzzzzzzzzXxxxxxxxxxxx",
        "xXxxXxxXxxXxxXXXzzzzzzzzzzzzzzzzzXXXxXxxXxxXxxXx",
        "xxxxxxxxxxxxxxxxXXXXXXXXzXXXXXXXXxxxxxxxxxxxxxxx",
    ),
    (
        "LLLLLwwwwwLLLLLwMwwwLLLLLwwwwwMLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwMwwLLLLLwwwwwLMLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwMwwLLLLLwwwwwLMLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwMwwwLLLLLwwwwwMLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLLLwwwwwLLLLLwwwwwLLLLLwww",
        "wwwwwLLLLLwwwKwLLLLLwwwwwLLLLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwKKKKKKKKKKKKKKKKKKKKKKKwLLLLLwwwwwLLL",
        "wwwwwLLLLLKhhhhhhhhrhhhhhhhhKKKKKKKKKLLLwwwwwLLL",
        "wwwwwLLLLLKHHHoHHHHHooHHHHoohhhhhrhhhKLLwwwwwLLL",
        "wwwwwLLBKKKhhhrrhhhhHooHHHHooHHHHHHHHKLLwwwwwLLL",
        "LLLLLwwwwwKoooobbooohhrrhhhhrrhhhhhhhKwKBLLLLwww",
        "LLLLLwwwwwKrrroobbooooobooooobbooooooKKwLLLLLwww",
        "LLLLLwwwwwKrrrrrrBBrrrrrBrrrrrBBrroooKwwLLLLLwww",
        "LLLLLwwwwwLKKKKKKKKKbbbbBBbbbbrBBrrrrKwwLLLLLwww",
        "LLLLLwwwwwLLLLKKKKKKKKKKKKKKKKKKKKKKKwwwLLLLLwww",
        "wwwwwLLLLLwwwwwLLLLLwmwwmLLmLLwwwwKLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwmwwmLLmLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwmwwmLLmLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwmmmmmmmLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwmmmmmmmLLwwwwwLLLLLwwwwwLLL",
        "LLLLLwwwwwLLLLLwwwwwLLLmmmwwwwLLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLmmmwwwwLLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLMmwwwwwLLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLMmwwwwwLLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLMmwwwwwLLLLLwwwwwLLLLLwww",
        "wwwwwLLLLLwwwwwLLLLLwwwMmLLLLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwwwMmLLLLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwwwMmLLLLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwwwMmLLLLLwwwwwLLLLLwwwwwLLL",
        "wwwwwLLLLLwwwwwLLLLLwwwMmLLLLLwwwwwLLLLLwwwwwLLL",
        "LLLLLwwwwwLLLLLwwwwwLLLMmwwwwwLLLLLwwwwwLLLLLwww",
        "LLLLLwwwwwLLLLLwwwwwLLLMmwwwwwLLLLLwwwwwLLLLLwww",
    ),
    (
        "................................................",
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
        ".........m..............m..............m........",
        ".........m..............m..............m........",
        "........mm.............mm.............mm........",
        ".........K..............K..............K........",
        ".......KKBKK..........KKBKK..........KKBKK......",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        ".......KKKKK..........KKKKK..........KKKKK......",
        ".........K..............K..............K........",
        ".......KKKKK..........KKKKK..........KKKKK......",
        "......KhHBorK........KhHBorK........KhHBorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "......KhHhorK........KhHhorK........KhHhorK.....",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
    ),
    (
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
        "llllllllllllllllllllllllMlllllllllllllllllllllll",
        "llllllllllllllllMMMMMMMMMMMMMMMMMlllllllllllllll",
        "lllllllllllllMMMMMMMMMMMwMMMMMMMMMMMllllllllllll",
        "kkkkkkkkkkkMMMMMMwwwwwwwwwwwwwwwMMMMMMkkkkkkkkkk",
        "lllllllllMMMMMwwwwwwwwwwwwwwwwwwwwwMMMMMllllllll",
        "llllllllMMMwwwwwwwwwwwwwwwwwwwwwwMwwwwMMMlllllll",
        "lllllllMMMwwwwwwwwwwwwwwwwwwwMMMMeMMMMwMMMllllll",
        "llllllMMMwwwwwwwwwwwwwwwwwwMMeeeeeeeeeMMMMMlllll",
        "lllllMMMwwwwwwwwwvtwwwwwwwMeeeeeeeEeeeeeMMMMllll",
        "kkkkkMMwwwwwwwvtwwwwwwwwwwMeeeeeEyEEEeeeMwMMkkkk",
        "llllMMwwwwwwwwwwvtwwwwwwwMeeeeeEEEEEEEeeeMwMMlll",
        "llllMMwwwwwwwvtwwwwwwwwwwwMeeeeeEEEYEeeeMwwMMlll",
        "llllMMwwwwwwwwwvtwwwwwwwwwMeeeeeeeEeeeeeMwwMMlll",
        "lllMMwwwwwwwwKwwwwwwwwwwwwwMMeeeeeeeeeMMwwwwMMll",
        "llllMMwwwwwKKKKKKKKKKKKKKKKwwMMMMeMMMMwwwwwMMlll",
        "kkkkMMwwwwwKHHoHhhhhrhhKKKKKKKwwwMwwwwwwwwwMMkkk",
        "llllMMwwBKKKhhroHHHHooHHHHHHHKwwwwwwwwwwwwwMMlll",
        "lllllMMwwwwKooobboohhrrhhhhhHKwKBwwwwwwwwwMMllll",
        "lllllMMMwwwKrrrrBBrooobboooooKKwwwwwwwwwwMMMllll",
        "llllllMMMwwKKKKKKKKKKKKKKKKKKKKKwwwwwwwwMMMlllll",
        "lllllllMMMwwwKKKKhhhrhhhhhhrrhhKwwwwwwwMMMllllll",
        "kkkkkkkkMMMwwKHHHHHHooHHHHHHoHHKwKBwwwMMMkkkkkkk",
        "lllllllllMBKKKhhhhhhhrrrhhoooooKKwwMMMMMllllllll",
        "lllllllllllMMKoobooooobbborrrrrKMMMMMMllllllllll",
        "lllllllllllllKrrBBrrrrrrBBbbKKKKMMMMllllllllllll",
        "lllllllllllllKKKKKKKKKKKKKKKKKKMMlllllllllllllll",
        "llllllllllllllllllllllllMlllllllllllllllllllllll",
        "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk",
        "llllllllllllllllllllllllllllllllllllllllllllllll",
    ),
    (
        "pppppppppppppppppppppppppppppppppppppppppppppppp",
        "ppppppppppppppppppppppppppppppppppppppsppppppppp",
        "pppppppppppppppppppppppppppppppppppssssssspppppp",
        "ppppppppppppppppppppppppppppppppppsssssssssppppp",
        "pPpPpPpPpPpPpPpPpPpPpPpPpPpPpPpPpPsssssssssPpPpP",
        "PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPsssssssssssPPPP",
        "PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPsssssssssPPPPP",
        "PPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPPsssssssssPPPPP",
        "PqPqPqPqPqPqPqPqPqPqPqPqPqPqPqPqPqPsssssssPqPqPq",
        "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqwqqqqqsqqqqqqqqq",
        "qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqwqqqqqqqqqqqqqqqqq",
        "wwwwwwwwwwaaaAaaaaaaaaaaaaaaaaaaaawaaaaaaaaaAaaa",
        "AAAAAAAAAAwwwwaaaAaaaaaaaaawwaaaaaaawaaaaaaaaaaa",
        "aaaaaaaaaaAAAAwwwaaaaAaaawwaawaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaAAAwwaaaawwAaaaaaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaAAwwwwaaaaaaAaKKKKKKKKKaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaAAAwwaaaaaKKKKhhhhHHKaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaAAwwaaaKHHHHHHHHhKKKBaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaAABKaKhhhhhhoooKaAaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaAwKKoooooorrrKaaaaaAaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaAAKrrrrrbKKKKaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaKKKKKKKKKaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaawwwwwwwwwwwwwwaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaMMMMMMMMMMMMaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaAaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaAaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaAaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaAa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaAaaaaaaaaaaaaaaaa",
    ),
    (
        "*DDDDDDDDDDDDDDDDDDDDDD*DDDD*DDDDDDDDDDDDDDD*DDD",
        "DDDDD*DDDDDDDDDDDDDDDDDDDDD*DDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD*DDDDDDDdDDDDDDD",
        "DDDDDDDDDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDddMddDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDdMMMMMdDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDdMMMMMMMdDDD",
        "DDDDDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDDDDdMMMMMd*DDD",
        "D*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDddMddDDDDD",
        "DDDDDDDDDD*DDDDDDDDDD*DDDDDDDDDD*DDDDDDDdDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDD*DD*DDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDD*DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD",
        "DDDDDDDDD*DDDDDDDDDDDKKKKKKKKKKKDDDDDDDDDDDDDDDD",
        "DDDDDDDDD*DDD*D*DDDDDKhhhhhhhhKKKDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDBKDKHHHHHHHHHHKDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDKKooohhhhhhhKKKBDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDKrrroooooooKDDDDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDKKKbbrrrrrrKDkkDDDDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDKKKKKKKKKKKDDDkkkDDDDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDDDfffDDDDDDDDDDDDDkkkDDDDDD",
        "DDDDDDDDDDDDDDDDDDDDDfffffDDDDDDDDDDDDDDDDkkkDDD",
        "DDDDDDDDDDDDDDDDDDDDfffffffDDDDDDDDDDDDDDDDDDkkk",
        "DDDDDDDDDDDDDDDDDDDfffffffffDDDDDDDDDDDDDDDDDDDD",
        "XxxxxxxxxxxXxxxxxxxffFFFFFffxxxxxXxxxxxxxxxxXxxx",
        "xxxxxxxXxxxxxxxxxxXFFFFWWWFFxXxxxxxxxxxxXxxxxxxx",
        "xxxXxxxxxxxxxxXxxffFFWWWWWFFfxxxxxxxXxxxxxxxxxxX",
        "xxxxxxxxxxXxxxxxxFFFFWWWWWFFFFxxXxxxxxxxxxxXxxxx",
        "xxxxxxXxxxxxxxxxFFFFFWWWWWFFFFxxxxxxxxxXxxxxxxxx",
        "xxXxxxxxxxxxlkkkkkkkkkkkkkkkkkkkkkxXxxxxxxxxxxXx",
        "xxxxxxxxxXxxxkkkkkkkkkkkkkkkkkkkkklxxxxxxxXxxxxx",
        "xxxxxXxxxxxxxxxxXxxxxxxxxxxXxxxxxxxxxxXxxxxxxxxx",
        "xXxxxxxxxxxxXxxxxxxxxxxXxxxxxxxxxxXxxxxxxxxxxXxx",
        "xxxxxxxxXxxxxxxxxxxXxxxxxxxxxxXxxxxxxxxxxXxxxxxx",
    ),
    (
        "pppppppppppppppppppppppppppppppppppppppppppppppp",
        "ppppppppppgppppppppppppppppppppppppppppppppppppp",
        "pppgggggggggggggggpppppppppppppppppppppppppppppp",
        "pgggggggggggggggggggpppppppppppppppppppppppppppp",
        "gggggggggggggggggggggppppppppppppppppppppppppppp",
        "gggggggggggggggggggggg1ppppppppppppppppppppppppp",
        "ggggggggggggggggggggggg1pppppppppppppppppppppppp",
        "gggggggggggggggggggggg11pppppppppppppppppppppppp",
        "gggggggggggggggggggggg111ppppppppppppppppppppppp",
        "gggggggggggggggggggg111g11pppppppppppppppppppppp",
        "ggggggggggggggggg333111111pPpPpPpPpPpPpPpPpPpPpP",
        "ggggggggggg1111111111111111PPPPPPPPPPPPPPPPPPPPP",
        "gggggggggg11111111111111111PPPPPPPPPPPPPPPPPPPPP",
        "gggggggggg11111110110111111PPPPPPPPPPPPPPPPPPPPP",
        "ggggggg2gg11111111001111111PPPPPPPPPPPPPPPPPPPPP",
        "gggggg222111111111111111112PPPPPPPPPPPPPPPPPPPPP",
        "Pggggg2221111111111111111113PPPPPPPPPPPPPPPPPPPP",
        "P1ggg2232211111111111111112PPPPPPPPPPPPPPPPPPPPP",
        "P111g122311111111111LL11151PPPPPPPPPPPPPPPPPPPPP",
        "P111112221111111111111iiiiiiKKKKKKKKKKKKKKKKKPPP",
        "P11111121111111111111555555KhhhhhrrhhhhhrrhhhKPP",
        "PP1111111111111111111555555KHHHHHHooHHHHHooHHKPP",
        "Pq1111111111111111115555BKKKhhhhhhhrrhhhhhrhhKKK",
        "qqq111111111111111111555555KoobooooobboooooooKqq",
        "qqqq11111111111111111551555KrrBBrrrrrBBrrrrrrKqq",
        "qqqqq11111111111111111iiiii5KKKKKKKKKKKKKKKKKqqq",
        "qqqqq11111111111111111111111qqqqqqqqqqqqqqqqqqqq",
        "qqqqqq11111111111111111111111qbqqqqqqqqqqqqqqqqq",
        "qqqqqqqq11111111111111111111qqqqqrqqqqqqqqqqqqqq",
        "uuuuuuuuuuuuu111111q1111111qqbqqqqqqqqqqqqqqqqqq",
        "uuuuuuuuuuuuuuuuuuuuuuq1qqqqqqqqqqqqqqqqqqqqqqqq",
        "uuuuuuuuuuuuuuuuuuuuuuqqqqqqqqqqqqqqqqqqqqqqqqqq",
    ),
    (
        "uuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuuu",
        "uuuuuuuuuuuuuuuuuuuuuuuuwuuuuuuuuuuuuuuuuuuuuuuu",
        "uuuuuuuuuuuuuuuuwuwwwwwwwwwwwwwuwuuuuuuuuuuuuuuu",
        "uuuuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwuuuuuuuuuuuu",
        "uuuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwwwuuuuuuuuuuu",
        "uuuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwwwuuuuuuuuuuu",
        "uuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwwwwwuuuKuuuuuu",
        "uuuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwwwuuKKKKKuuuu",
        "uuuuuuuuuuuuwwwwwwwwwwwwwwwwwwwwwwwwwuKhHhorKuuu",
        "uuuuuuuuuuuuuwwwwwwwwwww3wwwwwwwwwwwuuKhHhorKuuu",
        "uuuuuuuuuuuuuuuMMMMM333313333MMMMMuuuuKhHhorKuuu",
        "uuuuuuuuuuuuuuuuuu3311111111133uuuuuuuKhHhorKuuu",
        "uCuCuCuCuCuCuCuCu313331111133313uCuCuCKhHhorKCuC",
        "CCCCCCCCCCCCCCCC31111111111111113CCCCCKhHhorKCCC",
        "CCCCCCCCCCCCCCC3111101111111011113CCCCKhHhorKCCC",
        "CCCCCCCCCCCCCCC3111111111111111113CCCCKhHhorKCCC",
        "CCCCCCCCCCCCCCC3111111111111111113CCCCKhHhorKCCC",
        "CCCCCCCCCCCCCC31L111111111111111L13CCCCKKKKKCCCC",
        "CCCCCCCCCCCCCCC3111111111111111113CCCCCCCKCCCCCC",
        "CCCCCCCCCCCCCCC31111gggg1gggg11113CCCCmCmCmCCCCC",
        "CCCCCCCCCCCCCCC3111g111151111g1113CCCCmCmCmCCCCC",
        "CCCCCCCCCCCCCCCC311111iiiii111113CCCCCmCmCmCCCCC",
        "CcCcCcCcCcCcCcCcC311155555551113CcCcCcCcmcCcCcCc",
        "cccccccccccccccccc331155L551133cccccccccmccccccc",
        "cccccccccccccccccccc333353333cccccccccccmccccccc",
        "cccccccccccccccccccccccc3cccccccccccccccmccccccc",
        "ccccccccccccwwwwwwwwwwwMMMwwwwwwwwwwwcccmccccccc",
        "ccccccccccccwwwwwwwwwwwMgMwwwwwwwwwwwcccmccccccc",
        "ccccccccccccwwwwwwwwwwwMMMwwwwwwwwwwwcccmccccccc",
        "ccccccccccccwwwwwwwwwwwMMMwwwwwwwwwwwcccmccccccc",
        "ccccccccccccwwwwwwwwwwwMgMwwwwwwwwwwwcccmccccccc",
        "ccccccccccccwwwwwwwwwwwMMMwwwwwwwwwwwcccmccccccc",
    ),
    (
        "cccccccccccccccccccccccccccccccccccccccccccccccc",
        "cccccccccccccccccccccccccccccccccccccccccccccccc",
        "ccccccwccccccccc0ccccc0ccccc0ccccccccccccccccccc",
        "cccwwwwwwwcccc00000c00000c00000ccccccccccccccccc",
        "ccwwwwwwwwwcc0000000000000000000cccccccccccccccc",
        "cccwwwwwwwc00000000000000000000000ccccccwccccccc",
        "ccccccwcccc00000000000000000000000ccwwwwwwwwwccc",
        "cccccccccc0000000000000000000000000wwwwwwwwwwwcc",
        "ccccccccccc00000000000000000000000ccwwwwwwwwwccc",
        "ccccccccccc00000200000200000200000ccccccwccccccc",
        "ccccccccccccc0222220222220222220cccccccccccccccc",
        "cccccccccccc442222222222222222244ccccccccccccccc",
        "cccccccccccc422220222222220222224ccccccccccccccc",
        "cccccccccccc422202022222202022224ccccccccccccccc",
        "cccccccccccc422222222222222222224ccccccccccccccc",
        "ccccccccccc42222222222222222222224cccccccccccccc",
        "cccccccccccc422222222222222222224ccccccccccccccc",
        "cccccccccccc422222222222222222224ccccccccccccccc",
        "cccccccccccc422222222252222222224ccccccccccccccc",
        "cccccccccccc442222iiiiiiiii222244ccccccccccccccc",
        "cCcCcCcCcCcCc4222555555555552224cCcCcCcCcCcCcCcC",
        "CCCCCCCCCCCCCC42R25555555552R24CCccKKKKKKKKKKKCC",
        "CCCCCCCCCCCCCCC422222252222R24CCCccKohhhhhh3333C",
        "CCCCCCCCCCCCCCCC4422222222244CCCCccKhHHHHHH2222C",
        "CCCCCCCCCCCCCCCCCC444424444CCCCCCccKoohhhhh3333K",
        "CCCCCCCCCCCCCCCCCCCCCC4CCCCCCCCCCccKroooooo2222C",
        "CCCCCCCCCCCCTTTTTTTTTTTTTTTTTTTTTccKorrrrrr3333C",
        "CCCCCCCCCCCCtttttttttttttttttttttccKKKKKKKKKKKCC",
        "CCCCCCCCCCCCtttttttttttttttttttttCCCCCCCCCCCCCCC",
        "CCCCCCCCCCCCtttttttttttttttttttttCCCCCCCCCCCCCCC",
        "CCCCCCCCCCCCtttttttttttttttttttttCCCCCCCCCCCCCCC",
        "CCCCCCCCCCCCtttttttttttttttttttttCCCCCCCCCCCCCCC",
    ),
)


DESKTOP_APP_LAUNCHERS = {
    "files": "open_default_file_manager",
    "games": "open_games_suite",
    "snake": "open_snake",
    "sudoku": "open_sudoku",
    "chess": "open_chess",
    "messenger": "open_messenger",
    "calculator": "open_calculator",
    "images": "open_image_viewer",
    "notepad": "open_notepad",
    "editor": "open_text_editor",
    "dispenser": "open_dispenser",
    "media": "open_media_player",
    "ide": "open_python_ide",
    "browser": "open_browser",
    "drive-a": "open_drive_a",
    "drive-b": "open_drive_b",
    "settings": "open_settings",
    "modding": "open_modding_environment",
    "virtual-drives": "open_virtual_drive_manager",
    "weather": "open_weather",
    "news": "open_news",
    "pyai": "open_ai_chat",
    "about": "show_about",
}


def main():
    """Main entry point"""
    relaunch_in_configured_environment(__file__)
    root = tk.Tk()
    app = DesktopGUI(root)
    root.update_idletasks()
    app.lock_desktop()
    app.start_notifications()
    if len(sys.argv) >= 3 and sys.argv[1] == "--app":
        launcher = DESKTOP_APP_LAUNCHERS.get(sys.argv[2].casefold())
        if launcher:
            root.after(0, getattr(app, launcher))
        else:
            messagebox.showerror("pyOS", f"Unknown desktop application: {sys.argv[2]}")
    root.mainloop()


if __name__ == "__main__":
    main()
