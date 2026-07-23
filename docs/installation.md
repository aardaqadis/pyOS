# Installation and operation

## Windows installer mode

`pyOS-Setup.exe` is a PyInstaller one-file setup program. At startup PyInstaller extracts its private payload to a temporary directory. `setup.py` detects the frozen runtime through `sys.frozen` and resolves payload files through `sys._MEIPASS`.

Frozen setup validates that all required sources and `runtime\pyOS.exe` / `runtime\pyOS-cli.exe` exist before changing the destination. It then:

1. validates that install, data, and downloads directories are safe and do not overlap;
2. establishes ownership metadata;
3. copies sources, documentation, artwork, and sounds;
4. installs the embedded GUI and CLI executables;
5. optionally installs VLC and Ollama through the platform package manager;
6. creates GUI/CLI command launchers and optional desktop shortcuts;
7. atomically writes shared configuration and the final install manifest.

No Python installation or package download is required for the pyOS runtimes. VLC and Ollama are external optional products and can require internet access.

## Source setup mode

Running `python setup.py` uses the same wizard and validation rules, but creates `<install>\.venv` and installs the package ranges declared by `PYTHON_PACKAGES`. Optional packages may fail with a warning. Launchers run `pyOSgui.py` or `pyOScli.py` with the installed virtual environment.

Useful unattended options:

```powershell
python setup.py --quiet `
  --install-dir "C:\Apps\pyOS" `
  --data-dir "$env:APPDATA\pyOS" `
  --downloads-dir "$env:USERPROFILE\Downloads" `
  --no-vlc --no-ollama --no-shortcuts
```

Use `--dry-run` to validate and print planned actions without changing files. The same flags are accepted by `pyOS-Setup.exe`.

## Installation layout

A frozen Windows installation contains the compiled GUI and CLI, launchers, application sources, documentation, artwork, sounds, the ownership marker, and `install_manifest.json`. A source installation replaces the compiled runtimes with `.venv` while retaining the sources.

Data is intentionally separate from program files. It contains accounts, profiles, settings, user Drive B contents, update staging/state, and ownership metadata. Uninstalling program files does not delete user data.

## Repair and uninstall safety

Re-running setup against an owned installation repairs or upgrades only paths listed in its manifest. A non-empty unowned destination is rejected. Unexpected files are never silently claimed.

Uninstall validates both the installation UUID marker and manifest, rejects links/junctions that could escape the root, removes only typed owned entries, and preserves unknown files. Ownership metadata and configuration are removed last, so a partial failure remains recoverable.

## Troubleshooting

- **Setup says a directory is unsafe:** choose a dedicated child directory, not a drive root, home directory, system directory, or a directory containing another configured root.
- **Destination is not owned:** select an empty folder or the exact folder created by an earlier pyOS setup.
- **VLC/Ollama was skipped:** install it manually or rerun setup when `winget`/Homebrew and network access are available.
- **JavaScript is unavailable:** rebuild or repair with PythonMonkey and `pminit` present; the desktop continues without it.
- **Startup reports an optional module warning:** the named feature is disabled, but core startup should continue.