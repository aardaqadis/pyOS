# pyOS

pyOS is a Python desktop environment containing a graphical desktop, authenticated command center, configurable virtual drives, development and app-making tools, live weather and news, file and media applications, notifications, games, a graphing calculator, and peer-to-peer messaging.

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
Source Setup uses the supported dependency ranges declared in `setup.py`, so separate Setup runs can select newer
compatible packages. The `requirements.lock`, `requirements-dev.lock`, and `requirements-build.lock` files are the
pinned CI and release-build inputs; use those locks when an exactly repeatable environment is required.

### Development checks

Install the locked development dependencies, then run the test and lint suites from the project root:

```powershell
python -m pip install -r requirements-dev.lock
python -m pytest
python -m ruff check .
```

Pytest discovers the maintained suite under `tests/`. Generated caches, local environments, and build outputs are
excluded from both test and lint discovery.

### Standalone Windows Executable and Disk Usage

pyOS can also be packaged as a single Windows executable. The executable contains the Python runtime and the
GUI dependencies, so a recipient does not need the project virtual environment or build directory.

Build a factory-isolated release from the project root with:

```powershell
.\exe_tools\Build-pyOSExe.ps1
```

Build dependencies are fixed in `requirements-build.lock`. The build script prefers the project `.venv`, falls
back to the active `python` command, and also accepts an explicit interpreter:

```powershell
python -m pip install -r requirements-build.lock
$Python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
.\exe_tools\Build-pyOSExe.ps1 -PythonPath $Python
```

The result is written to `dist\pyOS.exe`. Release builds use their own
`%LOCALAPPDATA%\pyOS-Release-2.0-Factory` profile, so developer accounts and preferences are not loaded or
packaged. Building does not delete or replace the Python source files. PyInstaller intermediates are reproducible
and excluded from Git.

Verify the executable's PE structure, SHA-256 hash, version resources, signature state, and packaged-file privacy:

```powershell
.\exe_tools\Test-pyOSExe.ps1
```

To rebuild with a different icon or Windows version metadata:

```powershell
.\exe_tools\Set-pyOSExeResources.ps1 `
  -IconPath pyos2.0.png `
  -Version 2.0.0.0 `
  -ProductVersion 2.0
```

`Reset-pyOSFactory.ps1`, `Start-pyOSExe.ps1`, and `Clear-pyOSBuild.ps1` provide confirmed factory reset,
launch, and cleanup operations. Reset and cleanup support `-WhatIf`; destructive actions request confirmation.

Approximate sizes for the current Windows build are:

| Item | Size | Needed by an end user? |
| --- | ---: | --- |
| `dist\pyOS.exe` | 58.27 MiB | Yes, for standalone distribution |
| Core source and resource files | 714 KB | No, unless developing or rebuilding |
| `.venv` development environment | 88.07 MB | No |
| `build` intermediate files | 53.70 MB | No |
| Complete development project | 188.07 MB | No |

These figures are a snapshot and will change as applications and dependencies are added. For ordinary Windows
distribution, only `dist\pyOS.exe` is required. Keep the source tree when developing, rebuilding, or installing
source-based updates.

pyOS checks GitHub for Stable releases or Unstable commits only after the user chooses an update channel, and
always asks before downloading and installing. Updates must resolve to an immutable full commit ID and publish
trusted SHA-256 metadata. Source updates are staged under a cross-process lock and rolled back if any overlay
step fails. A packaged Windows executable is additionally accepted only when its digest and Authenticode
signature validate; otherwise pyOS leaves the executable unchanged.

Release builders must embed the official commit-to-source-digest bindings and Authenticode signer thumbprint in
`pyos_updater.py`. Both trust sets are empty in an unconfigured source checkout, so automatic installation fails
closed until release trust is deliberately provisioned. Interrupted source transactions are durably journaled
and recovered at the next GUI or Command Center startup.

Signed Windows releases are produced only for numeric `v` tags such as `v2.0.0`. Configure a protected GitHub
Actions environment named `release`, restrict it to release tags and trusted reviewers, and add both required
environment secrets:

- `PYOS_SIGNING_PFX_BASE64`: the complete code-signing PFX encoded as base64
- `PYOS_SIGNING_PFX_PASSWORD`: the PFX import password

The release job fails if either secret is absent or the PFX does not contain exactly one private code-signing
certificate. It derives and embeds that certificate's SHA-1 thumbprint only in the runner's ephemeral updater
source, builds with the locked dependency set, signs and verifies the executable, uploads the signed executable
and post-signing SHA-256 manifest, then creates the GitHub release. `GH_TOKEN` is the short-lived token supplied
by GitHub Actions; no long-lived personal access token is required.

This Windows tag workflow deliberately does not publish a `pyos-source.zip` asset or populate
`TRUSTED_SOURCE_RELEASE_BINDINGS`, so automatic source overlays remain disabled. Enabling them requires a
separate, reviewed publication process that creates a canonical source archive first and pre-provisions its
immutable commit-to-digest binding in an already trusted build. Publishing an unbound archive would make it
appear updateable while every secure installation must reject it.

### Unattended Setup

```powershell
python setup.py --quiet `
  --install-dir "C:\Apps\pyOS" `
  --data-dir "C:\Users\me\pyOSData" `
  --downloads-dir "C:\Users\me\Downloads"
```

Validate setup without changing the computer:

```powershell
python setup.py --dry-run --no-vlc --no-ollama --no-shortcuts
```

## Launch

After setup, use the generated `pyOS GUI` or `pyOS CLI` shortcut.

Directly launching `pyOSgui.py` or `pyOScli.py` after setup automatically switches to the configured isolated Python environment so installed dependencies such as `fido2` remain available.

To run directly from this project:

```powershell
.venv\Scripts\python.exe pyOSgui.py
.venv\Scripts\python.exe pyOScli.py
```

The GUI can open a specific application after startup with `--app`:

```powershell
.venv\Scripts\python.exe pyOSgui.py --app weather
.venv\Scripts\python.exe pyOSgui.py --app news
.venv\Scripts\python.exe pyOSgui.py --app modding
.venv\Scripts\python.exe pyOSgui.py --app virtual-drives
.venv\Scripts\python.exe pyOSgui.py --app pyai
```

Authentication still occurs before the requested application opens.

## Authentication

On first use, pyOS asks you to create a username and password. The account is stored permanently in the configured shared data directory (or below the dedicated `~/.pyos` root when running without setup). Passwords are stored as salted PBKDF2-SHA256 hashes, not as plain text.

- The desktop starts locked and cannot be used until the correct credentials are entered.
- Use **Settings > Security** or the desktop context menu to lock the desktop again.
- The CLI requests credentials immediately before it sends the first command. Authentication remains valid for that CLI session.
- Use **Settings > Lock CLI** to show a full-window modal lock immediately. Explicit locking clears any remembered session and requires fresh authentication before any CLI control can be used.
- Username and password changes are available from the desktop Security settings and the CLI Settings menu. The current password is required.
- On supported Windows systems, **Settings > Security** can register a Windows Hello platform passkey. The lock screen then offers passwordless **Use Passkey** authentication while retaining the password as a fallback.
- Passkey registration and removal require the current password. pyOS stores the public credential; Windows Hello retains the private key.

Administrator and standard-user roles are cooperative pyOS UI policy. Global operations such as Setup,
updates, and removing all pyOS data require recent administrator authentication, but roles do not isolate
mutually hostile users at the host operating-system boundary. Python tools and custom apps run with the
permissions of the signed-in OS account; use separate OS accounts or an OS-protected service where hard
isolation is required.

## Desktop Basics

- Click a desktop launcher to open its application.
- Drag launchers to move them; they snap to the desktop grid and remember their positions across sessions.
- Drag an application title bar to move its window.
- Drag the bottom-right handle to resize a window.
- Application windows are constrained to the usable pyOS desktop and automatically fitted when the host window shrinks, keeping title bars and close controls reachable.
- Select `_` to minimize an application into the taskbar.
- Left-click empty desktop space to open the desktop menu.
- Right-click the desktop for application shortcuts.

The default appearance is monochrome. Open **Settings > Appearance** to change system colors, select an installed text font and size, or use a solid/image desktop background. Animated GIF backgrounds are supported.

## System Bar

The persistent top bar provides pyOS-wide controls:

- **pyOS**: About, Lock Desktop, Run Setup, Restart pyOS, Restart and Run Setup, and Shut Down pyOS
- **Applications**: shortcuts to core pyOS applications and Settings
- **Window**: minimize all windows, restore all windows, or raise an individual open window
- **Help**: display a usage tip or open About

Restart and shutdown actions require confirmation. **Shut Down pyOS** closes the pyOS environment and its internal applications; it does not shut down the host operating system. **Restart and Run Setup** closes pyOS, runs the setup wizard, and relaunches pyOS after setup exits.

### Uninstall pyOS Data

Select **Settings > Security > Uninstall pyOS** to reset and remove pyOS data without removing the program or its Python dependencies. The operation requires fresh administrator authentication followed by typing `UNINSTALL` exactly.

It permanently removes:

- The pyOS account, password hash, registered passkey metadata, GUI and CLI settings
- App Maker applications and modding backups
- The current session's temporary Drive A
- Manifest-listed pyOS data, including Drive B
- Registered custom-drive directories only when their pyOS ownership markers validate
- The shared pyOS installation-location configuration

It preserves:

- pyOS source files, modules, the isolated virtual environment, and installed packages
- The configured Downloads directory and its files
- Unrelated files when pyOS data was configured directly in a shared location such as the home directory

For safety, pyOS removes only manifest-listed paths or roots with matching pyOS ownership markers, refuses unsafe roots and escaping manifest entries, and deletes the primary configuration last. Unknown files are preserved. After uninstalling data, pyOS closes. Running it again starts first-use account and setup behavior.

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
- Setup can install VLC automatically on Windows through `winget` and on macOS through Homebrew (`brew install --cask vlc`).

### Python IDE

<img width="952" height="666" alt="image" src="https://github.com/user-attachments/assets/5fb7b94e-fd0f-4ece-ad11-6a7c50d69bd2" />

Edit, run, stop, and debug Python scripts. Program output appears in the embedded output console.

### Modding Environment

The Modding Environment provides direct access to pyOS source and configuration files from inside the desktop.

- Edit every Python module in the project and all applications created with App Maker.
- Validate Python syntax or JSON before saving.
- Save with `Ctrl+S` or **Save + Validate**.
- Create timestamped backups in `.pyos_mod_backups` before files are changed.
- Exclude virtual environments, caches, Git metadata, and backup directories from the source list.

Changes to pyOS source generally require restarting the desktop. Source mods execute with the same permissions as pyOS, so only use code you understand and trust.

#### App Maker

Select **App Maker** from the Modding Environment to create applications that launch in dedicated child processes.

- Start from a working Python template.
- Create, edit, rename, validate, run, and delete apps.
- Show every saved app as an individual desktop launcher; launchers update immediately after saving, renaming, or deleting an app.
- Run an app immediately with **Run Isolated**.
- Store custom apps in the shared pyOS data directory under `apps`.
- Back up overwritten custom apps automatically.

Each custom app defines an optional `APP_NAME` and a required entry point:

```python
APP_NAME = "Example"

def build(app, window):
    tk.Label(window.content, text="Hello from pyOS").pack(pady=20)
```

The runtime supplies `tk`, `ttk`, and `messagebox`. The `app` argument is a deliberately limited host exposing theme preferences, `create_window()`, and `show_notification()`; `window.content` is the app's parent frame. The separate process prevents a crash or blocking event loop from freezing the desktop, but it is not an OS sandbox. App Maker code remains unrestricted Python with the host user's file and network permissions, so only run code you trust.

### Weather

Weather displays current conditions and a seven-day forecast.

- Select **My Location** to use an approximate IP-based location.
- Search manually by city or postcode.
- View temperature, apparent temperature, humidity, cloud cover, precipitation, pressure, wind, and gusts.
- View daily conditions, temperature ranges, rain probability, and maximum wind speed.

Forecast data comes from Open-Meteo. **My Location** sends the public IP address to `ipapi.co` for approximate geolocation; use manual search if you do not want that lookup. Weather requests run in the background and require an internet connection.

### News

News displays current stories from Google News RSS.

- Browse top stories, World, UK, Business, Technology, Science, Health, Sports, and Entertainment.
- Search by keyword.
- Read the source, publication date, and feed summary.
- Double-click a headline or select **Open Full Story** to open the article in the system browser.
- Refresh feeds without blocking the desktop.

News requires an internet connection. Headlines and summaries are supplied by third parties and may link to external sites with their own privacy policies or subscriptions.

### pyAI

pyAI is a private, offline chat assistant powered by a language model running on your own computer through [Ollama](https://ollama.com), a free and open-source AI runtime. Nothing you type leaves the machine.

- Chat with the default model `llama3.2` (about 2 GB, downloaded once on first use with an in-window progress indicator).
- Press **Enter** to send and **Shift+Enter** for a new line; **Stop** cancels a response mid-generation.
- **New Chat** clears the conversation; history is memory-only and discarded when the window closes.
- Change the model in the toolbar **Model** field (any model from the Ollama library, e.g. `qwen2.5:3b`); the choice is saved with your desktop preferences.
- If Ollama is missing or not running, the window explains what to do instead of failing — Setup can install Ollama automatically (see Components), or get it from [ollama.com/download](https://ollama.com/download).

Responses are generated by a small local model and can be inaccurate. Model downloads require an internet connection; chatting afterwards does not.

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

### SSH Client

Open **SSH Client** from the desktop, Applications menu, context menu, or with `--app ssh`. Setup installs the
fixed Paramiko 5 line; the client refuses to run with Paramiko 4.0.0 or older because those releases are affected
by CVE-2026-44405.

- Connect to SSH servers with a password, private key and optional passphrase, or an SSH agent.
- Use the embedded interactive command terminal without leaving the pyOS desktop.
- Unknown host keys are rejected until you explicitly confirm their SHA-256 fingerprint.
- Trusted host keys are stored per pyOS profile in `ssh_known_hosts`.
- Passwords and key passphrases are kept only for the connection attempt and are cleared afterwards.

Confirm every new server fingerprint through a trusted channel before accepting it. pyOS does not silently trust
unknown SSH servers.

### Notifications

<img width="361" height="108" alt="image" src="https://github.com/user-attachments/assets/fdbe465b-017e-4ca8-80aa-1874e4f1e4ab" />


pyOS displays dismissible desktop toasts for system events, Messenger messages, and rotating usage tips. Toasts close automatically and stack above the taskbar.

### Games Suite

<img width="1452" height="724" alt="image" src="https://github.com/user-attachments/assets/03c87b83-4acf-4873-bbb4-2f55bd2cec7f" />

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
- Installed text-font family and interface font size
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
- Uninstall all pyOS user data and registered virtual drives without removing modules or packages

### Notifications

- Enable or disable all system notifications
- Enable or disable rotating pyOS tips independently
- Disabling system notifications also suppresses tips

## Virtual Drives

- **A:** temporary storage under the operating system's temporary directory
- **B:** persistent storage in the data location selected during setup
- **C:** user home directory in the CLI

Open **Virtual Drives** to create additional directory-backed drives. Each drive has:

- A unique name and parent location
- A configured quota value in MB
- Persistent or temporary storage metadata
- A read-only configuration flag

Double-click a registered drive or select **Open** to browse it in File Manager. **Unregister** removes its pyOS registration without deleting its directory or files. Quotas, storage mode, and read-only status are currently descriptive configuration metadata; pyOS does not enforce them at filesystem level.

Custom drive definitions are stored as `virtual_drives.json` beside the GUI settings file.

The shared setup configuration is stored in:

```text
~/.pyos/install.json
```

## Command Center

<img width="1186" height="792" alt="image" src="https://github.com/user-attachments/assets/f5dc74e4-159e-4929-b3a3-d27b6e70e74c" />

Run `help` in pyOS CLI for the complete command reference.

On Windows, the Command Center can run one-shot Windows PowerShell and WSL commands while keeping their output
inside the pyOS console:

```text
powershell Get-ChildItem -Force
ps Get-Process | Sort-Object CPU -Descending
wsl uname -a
wsl ls -la
```

Commands run from the Command Center's current directory through tracked child processes. Use the **Stop** button,
the **Shell > Stop Active Command** menu item, or enter `stop` to cancel active work. Shell commands time out after
60 seconds, and displayed output is bounded. WSL commands require Windows Subsystem for Linux to be installed.

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

### Desktop Applications

The Command Center's **Apps** menu and CLI commands can launch enabled pyOS desktop
applications directly. Run `apps` to list them, or use commands such as
`calculator`, `messenger`, `ssh`, `games`, `ide`, `notepad`, `images`,
`desktop_browser`, and `desktop_media`. The GUI also accepts `--app weather`,
`--app news`, `--app modding`, `--app virtual-drives`, and `--app pyai`.

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

Install both components. The `python-vlc` package is installed by setup; if VLC
itself is missing, install it for your platform.

On Windows:

```powershell
.venv\Scripts\python.exe -m pip install python-vlc
winget install --id VideoLAN.VLC --exact
```

On macOS:

```bash
brew install --cask vlc
```

(or download VLC from https://www.videolan.org/vlc/ and move it to `/Applications`).

Restart pyOS after installation.

### pyAI Reports Ollama Unavailable

pyAI needs the free Ollama runtime installed and running. Setup installs it
when the **Install Ollama local AI runtime** component is selected; otherwise
install it manually.

On Windows:

```powershell
winget install --id Ollama.Ollama --exact
```

On macOS:

```bash
brew install ollama
ollama serve
```

On Linux:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

(or download it from https://ollama.com/download). If Ollama is installed but
the window says it is not running, use the **Start Ollama** button or run
`ollama serve` in a terminal. The first chat also needs a one-time model
download (about 2 GB for `llama3.2`), which pyAI offers automatically.

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
