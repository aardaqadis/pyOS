# pyOS

pyOS is a Python desktop environment containing a graphical desktop, authenticated command center, virtual drives, file tools, media applications, notifications, a games suite, a graphing calculator, and peer-to-peer messaging.

<img width="685" height="387" alt="pyOS" src="https://github.com/user-attachments/assets/16bfe9df-7f53-4034-93d6-840e2414adef" />

## Install

To get started, either download the source code from the releases page for the latest safe build, or the source code of the latest commit.
Python 3.10 or newer is recommended. 

On Windows, launch the setup wizard from PowerShell:

```powershell
python setup.py
```

On macOS you can double click "pyOS Desktop.command" for the GUI or "pyOS Command Center.command" for the CLI.

The wizard lets you choose:

- Application installation directory
- Shared pyOS data directory
- Downloads directory
- Whether to install VLC media support
- Whether to create desktop shortcuts

Setup creates an isolated virtual environment and installs the Python packages required by pyOS GUI and CLI.

### Unattended Setup

```powershell
python setup.py --quiet `
  --install-dir "C:\Apps\pyOS" `
  --data-dir "C:\Users\me\pyOSData" `
  --downloads-dir "C:\Users\me\Downloads"
```

Validate setup without changing the computer:

```powershell
python setup.py --dry-run --no-vlc --no-shortcuts
```

## Launch

After setup, use the generated `pyOS GUI` or `pyOS CLI` shortcut.

Directly launching `pyOSgui.py` or `pyOScli.py` after setup automatically switches to the configured isolated Python environment so installed dependencies such as `fido2` remain available.

To run directly from this project:

```powershell
.venv\Scripts\python.exe pyOSgui.py
.venv\Scripts\python.exe pyOScli.py
```

## Authentication

On first use, pyOS asks you to create a username and password. The account is stored permanently in the configured shared data directory (or in your home directory when running without setup). Passwords are stored as salted PBKDF2-SHA256 hashes, not as plain text.

- The desktop starts locked and cannot be used until the correct credentials are entered.
- Use **Settings > Security** or the desktop context menu to lock the desktop again.
- The CLI requests credentials immediately before it sends the first command. Authentication remains valid for that CLI session.
- Use **Settings > Lock CLI** to require the password before the next command.
- Username and password changes are available from the desktop Security settings and the CLI Settings menu. The current password is required.
- On supported Windows systems, **Settings > Security** can register a Windows Hello platform passkey. The lock screen then offers passwordless **Use Passkey** authentication while retaining the password as a fallback.
- Passkey registration and removal require the current password. pyOS stores the public credential; Windows Hello retains the private key.

## Desktop Basics

- Click a desktop launcher to open its application.
- Drag launchers to move them; they snap to the desktop grid.
- Drag an application title bar to move its window.
- Drag the bottom-right handle to resize a window.
- Select `_` to minimize an application into the taskbar.
- Left-click empty desktop space to open the desktop menu.
- Right-click the desktop for application shortcuts.

The default appearance is monochrome. Open **Settings > Appearance** to change system colors or use a solid/image desktop background. Animated GIF backgrounds are supported.

## System Bar

The persistent top bar provides pyOS-wide controls:

- **pyOS**: About, Lock Desktop, Run Setup, Restart pyOS, Restart and Run Setup, and Shut Down pyOS
- **Applications**: shortcuts to core pyOS applications and Settings
- **Window**: minimize all windows, restore all windows, or raise an individual open window
- **Help**: display a usage tip or open About

Restart and shutdown actions require confirmation. **Shut Down pyOS** closes the pyOS environment and its internal applications; it does not shut down the host operating system. **Restart and Run Setup** closes pyOS, runs the setup wizard, and relaunches pyOS after setup exits.

## Desktop Menu

Left-click empty desktop space to access:

- **Open File Explorer**: opens the embedded pyOS File Manager.
- **Shortcut to Taskbar**: pins any selected file or directory.
- **File to Taskbar**: pins a file.
- **Directory to Taskbar**: pins a directory.
- **New Empty File**: creates and pins a file of any extension.
- **New Directory**: creates and pins a directory.

All Add operations use the embedded pyOS explorer rather than native operating-system dialogs.

## Taskbar

Taskbar shortcuts display a small icon with their name below it.

- Click a shortcut to open it inside pyOS.
- Right-click a shortcut to remove it.
- Directories open in File Manager.
- Images open in Image Viewer.
- Audio and video open in Media Player.
- Other files open in Text Editor.
- Minimized applications appear as titled taskbar buttons. Click one to restore and raise its window.

Click the taskbar clock to open a movable analogue clock and calendar. Use `<` and `>` to navigate between months.

## Applications

### File Manager

<img width="827" height="532" alt="image" src="https://github.com/user-attachments/assets/04271260-6e98-4659-84be-9463cbcd7269" />

Browse the home directory and virtual drives. Toolbar shortcuts provide direct access to:

- **Home**: your user home directory
- **Drive A**: temporary pyOS storage
- **Drive B**: persistent storage configured during setup

Double-click a directory to enter it or a file to open it in the appropriate pyOS application.

### Text Editor

<img width="810" height="525" alt="image" src="https://github.com/user-attachments/assets/9e0c1956-97f8-4e99-8094-b98668db7339" />

Opens text and arbitrary binary files using replacement characters for undecodable bytes. It supports new, open, save, undo, and `Ctrl+S`.

### Sticky Notes

<img width="338" height="333" alt="image" src="https://github.com/user-attachments/assets/719e01e9-983d-4655-ac41-3edf614027a5" />

Creates independent memory-only notes.

- Use `+` or `Ctrl+N` for another note.
- Drag the title bar to move a note.
- Resize using the bottom-right handle.
- Notes support undo, redo, clear, and live character count.
- Note colors follow the active OS color scheme.
- Notes are discarded when pyOS closes.

### Internet Browser

<img width="874" height="583" alt="image" src="https://github.com/user-attachments/assets/8aebe4dc-3b38-442f-aeeb-893aca27c23b" />

Enter a URL and select **Go**. The browser can render HTML/CSS when `tkinterweb` is available.

- **Inspect**: displays HTML source, final URL, HTTP status, response headers, and page size.
- **Save Page**: downloads the loaded response.
- **Network**: shows network traffic information when `psutil` is available.
- **JavaScript**: enables experimental SpiderMonkey scripting and supported DOM events for the current browser window. It is off by default and should only be enabled for trusted websites.

Page inspection is limited to 10 MB.
JavaScript support is not equivalent to Chromium: modern sites may still fail because TkinterWeb implements a limited HTML/CSS and DOM environment.

### Image Viewer

<img width="848" height="589" alt="image" src="https://github.com/user-attachments/assets/63bad579-c314-49d2-8a3e-a66e6edd37f2" />

Supports PNG, JPEG, BMP, ICO, TIFF, WebP, APNG, and animated GIF files.

- Enter an image path or use **Open**.
- **Fit** scales the image to the window.
- **Actual** displays its native size.
- Use `+` and `-` to zoom.
- Use the scrollbars for large images.

### Media Player

<img width="866" height="585" alt="image" src="https://github.com/user-attachments/assets/67f6fccc-d4a8-49d2-96dc-2f9662b883a2" />

Plays common audio and video formats with VLC.

- Open, play/pause, stop, seek, mute, and adjust volume.
- VLC Media Player and the `python-vlc` package must both be installed.
- Setup can install VLC automatically on Windows through `winget`.

### Python IDE

<img width="952" height="666" alt="image" src="https://github.com/user-attachments/assets/5fb7b94e-fd0f-4ece-ad11-6a7c50d69bd2" />

Edit, run, stop, and debug Python scripts. Program output appears in the embedded output console.

### Calculator

<img width="1023" height="597" alt="image" src="https://github.com/user-attachments/assets/4f6d2f90-d459-4337-9328-9c29f3fd4054" />


Run basic arithmetic and interactive graphical calculations.

- Supports `sqrt`, trigonometric functions, logarithms, powers, modulo, and parentheses.
- Provides constants `pi`, `e`, and `tau` through a restricted expression evaluator rather than Python `eval`.
- Plot multiple enabled expressions as `y = expression` or plain expressions, each with an independent color.
- Edit shared variables `a` through `z` directly or with sliders whose minimum and maximum thresholds are configurable.
- Drag the graph and grid together to pan. Use the mouse wheel to zoom around the pointer.
- Configure visible X and Y ranges manually.

### Peer-to-Peer Messenger

<img width="892" height="617" alt="image" src="https://github.com/user-attachments/assets/04d51dc6-9917-493f-b703-2dd09ba54639" />

Messenger discovers other running pyOS Messenger instances by their shared pyOS username on the same local network.

- Send text and images directly to an online username.
- Preview received images and save them to disk.
- Images are limited to 5 MB and incoming packets are validated before display.
- Incoming messages generate desktop notifications, including while the Messenger window is closed.

Messenger is LAN-only: discovery uses local broadcasts and messages use direct TCP connections. Messages are not end-to-end encrypted. Connecting exposes your username, IP address, and online status to peers, and hackers or malicious peers could obtain information or send harmful content. Only use Messenger on trusted networks and only open images from people you trust.

### Notifications

<img width="361" height="108" alt="image" src="https://github.com/user-attachments/assets/fdbe465b-017e-4ca8-80aa-1874e4f1e4ab" />


pyOS displays dismissible desktop toasts for system events, Messenger messages, and rotating usage tips. Toasts close automatically and stack above the taskbar.

### Games Suite

Open **Games Suite** from its desktop launcher or the desktop context menu.

- **Snake**: control the snake with the arrow keys or WASD, collect food, and avoid walls and your own body. The game accelerates as the snake grows.
- **Sudoku**: play generated 9 x 9 puzzles, validate entries, and highlight incorrect cells without exposing the solution.
- **Automated Chess**: play White against a computer opponent. Legal movement, check, checkmate, castling, promotion, and draw rules are provided by the `chess` package installed through setup.

All games run in movable, resizable, and minimizable pyOS windows.

## Settings

Settings are saved in the data directory selected by setup.

### Appearance

- Solid-color or image desktop background
- Editable desktop, window, text, title-bar, and title-text colors
- Interface font size
- **Defaults** restores the monochrome color scheme

### Clock

<img width="598" height="410" alt="image" src="https://github.com/user-attachments/assets/f3019cf1-907c-4b40-a767-37305253056b" />

- 12-hour or 24-hour format
- Optional seconds display

### Files

- Show or hide dot-prefixed files
- Select the default File Manager location: Home, Drive A, or Drive B

### Security

- Change the shared pyOS username and password
- Register or remove a Windows Hello/WebAuthn platform passkey
- Lock the desktop immediately

### Notifications

- Enable or disable all system notifications
- Enable or disable rotating pyOS tips independently
- Disabling system notifications also suppresses tips

## Virtual Drives

- **A:** temporary storage under the operating system's temporary directory
- **B:** persistent storage in the data location selected during setup
- **C:** user home directory in the CLI

The shared setup configuration is stored in:

```text
~/.pyos_install.json
```

## Command Center

<img width="1186" height="792" alt="image" src="https://github.com/user-attachments/assets/f5dc74e4-159e-4929-b3a3-d27b6e70e74c" />

Run `help` in pyOS CLI for the complete command reference.

### Navigation and Files

```text
cd <path>                 Change directory; supports A:, B:, and C:
pwd                       Print the current directory
dir | ls                  List directory contents
tree                      Display a directory tree
drives                    List virtual drives
driveinfo                 Display drive paths and types
open <path>               Open a file or directory
explorer [path]           Open a directory
mkdir <name>              Create a directory
copy <source> <target>    Copy a file
move <source> <target>    Move a file
del <path>                Delete a file or directory
hash <file> [algorithm]   Calculate a file hash
```

### Browser and Media

```text
browser [url]             Open the internal page inspector
browse <url>              Open the system browser
inspect <url>             Print response headers and source preview
savepage <url> [file]     Download a web page
download <url>            Download to the configured downloads directory
play <file>               Play audio or video
```

### System and Console

```text
sysinfo                    Display system information
diskspace                  Display disk usage
tasklist | ps              List running processes
ipconfig                   Display network information
ping <host>                Ping a host
history                    Display command history
gui_settings               Display saved GUI settings
deskgui                    Launch the graphical desktop
clear                      Clear the console
exit                       Close pyOS CLI
```

## Troubleshooting

### Media Player Reports Missing VLC

Install both components:

```powershell
.venv\Scripts\python.exe -m pip install python-vlc
winget install --id VideoLAN.VLC --exact
```

Restart pyOS after installation.

### Browser Does Not Render HTML

Install the renderer:

```powershell
.venv\Scripts\python.exe -m pip install "tkinterweb[javascript]>=4.25,<5.0"
```

The source inspector remains available when rendering is unavailable. The JavaScript extra includes PythonMonkey's SpiderMonkey runtime and may take longer to install than the base renderer.

### Passkey Is Unavailable

Run setup again to install the supported `fido2` package into pyOS's isolated environment. Confirm that Windows Hello is configured and that **Settings > Security** reports WebAuthn support. Standalone script launches automatically reuse the configured environment after setup.

If Windows no longer holds the registered credential, sign in with the password, remove the stale passkey from pyOS under **Settings > Security**, and register it again.

### Messenger Does Not Find Another User

Both users must open Messenger on devices connected to the same local network. Permit pyOS through the operating-system firewall when prompted. Guest Wi-Fi, client isolation, VPN policies, and routed networks may block peer discovery or direct connections.

### Reset Installation Locations

Run `setup.py` again and select new locations. Both pyOS GUI and CLI read the same shared configuration.
