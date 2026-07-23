# pyOS documentation

This handbook explains how pyOS is structured, installed, built, operated, updated, tested, and released.

## Guides

- [Architecture](architecture.md) — components, startup, configuration, authentication, storage, and application services.
- [Installation and operation](installation.md) — setup modes, directory layout, launchers, repair, uninstall, and troubleshooting.
- [Building the Windows setup executable](building.md) — prerequisites, the three-stage build, artifacts, parameters, and CI signing.
- [Development and testing](development.md) — environment setup, dependency locks, test suites, linting, and safe contribution practices.
- [Security and updates](security-and-updates.md) — ownership manifests, atomic writes, update trust, rollback, and release signing.

## Quick start

For users on Windows, download and run `pyOS-Setup.exe`. It contains compiled GUI and CLI runtimes together with the source and resource payload used by setup and updates; Python is not required on the destination computer.

For source development:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe setup.py
```

To build the distributable installer:

```powershell
.\build_pyos.ps1
```

The output is `dist\pyOS-Setup.exe` and `dist\pyOS-Setup.exe.sha256`.