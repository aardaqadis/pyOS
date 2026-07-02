# pyOS

pyOS is a Python desktop environment containing a graphical desktop, command center, virtual drives, file tools, media applications, Sticky Notes and an image viewer.

https://img.shields.io/badge/latest-releases-green%3Flogo%3Dgithub?link=https%3A%2F%2Fgithub.com%2Faardaqadis%2FpyOS%2Freleases%2F

## Install

Python 3.10 or newer is recommended. On Windows, launch the setup wizard from PowerShell:

```powershell
python setup.py
```

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

To run directly from this project:

```powershell
.venv\Scripts\python.exe pyOSgui.py
.venv\Scripts\python.exe pyOScli.py
```

## Desktop Basics

- Click a desktop launcher to open its application.
- Drag launchers to move them; they snap to the desktop grid.
- Drag an application title bar to move its window.
- Drag the bottom-right handle to resize a window.
- Left-click empty desktop space to open the desktop menu.
- Right-click the desktop for application shortcuts.

The default appearance is monochrome. Open **Settings > Appearance** to change system colors or use a solid/image desktop background. Animated GIF backgrounds are supported.

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

Click the taskbar clock to open a movable analogue clock and calendar. Use `<` and `>` to navigate between months.

## Applications

### File Manager

Browse the home directory and virtual drives. Toolbar shortcuts provide direct access to:

- **Home**: your user home directory
- **Drive A**: temporary pyOS storage
- **Drive B**: persistent storage configured during setup

Double-click a directory to enter it or a file to open it in the appropriate pyOS application.

### Text Editor

Opens text and arbitrary binary files using replacement characters for undecodable bytes. It supports new, open, save, undo, and `Ctrl+S`.

### Sticky Notes

Creates independent memory-only notes.

- Use `+` or `Ctrl+N` for another note.
- Drag the title bar to move a note.
- Resize using the bottom-right handle.
- Notes support undo, redo, clear, and live character count.
- Note colors follow the active OS color scheme.
- Notes are discarded when pyOS closes.

### Internet Browser

Enter a URL and select **Go**. The browser can render HTML/CSS when `tkinterweb` is available.

- **Inspect**: displays HTML source, final URL, HTTP status, response headers, and page size.
- **Save Page**: downloads the loaded response.
- **Network**: shows network traffic information when `psutil` is available.

Page inspection is limited to 10 MB.

### Image Viewer

Supports PNG, JPEG, BMP, ICO, TIFF, WebP, APNG, and animated GIF files.

- Enter an image path or use **Open**.
- **Fit** scales the image to the window.
- **Actual** displays its native size.
- Use `+` and `-` to zoom.
- Use the scrollbars for large images.

### Media Player

Plays common audio and video formats with VLC.

- Open, play/pause, stop, seek, mute, and adjust volume.
- VLC Media Player and the `python-vlc` package must both be installed.
- Setup can install VLC automatically on Windows through `winget`.

### Python IDE

Edit, run, stop, and debug Python scripts. Program output appears in the embedded output console.

## Settings

Settings are saved in the data directory selected by setup.

### Appearance

- Solid-color or image desktop background
- Editable desktop, window, text, title-bar, and title-text colors
- Interface font size
- **Defaults** restores the monochrome color scheme

### Clock

- 12-hour or 24-hour format
- Optional seconds display

### Files

- Show or hide dot-prefixed files
- Select the default File Manager location: Home, Drive A, or Drive B

## Virtual Drives

- **A:** temporary storage under the operating system's temporary directory
- **B:** persistent storage in the data location selected during setup
- **C:** user home directory in the CLI

The shared setup configuration is stored in:

```text
~/.pyos_install.json
```

## Command Center

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
.venv\Scripts\python.exe -m pip install tkinterweb
```

The source inspector remains available when rendering is unavailable.

### Reset Installation Locations

Run `setup.py` again and select new locations. Both pyOS GUI and CLI read the same shared configuration.
