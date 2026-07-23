# Building the Windows setup executable

## Prerequisites

Use 64-bit Windows, PowerShell, a supported Python installation, and dependencies from `requirements-build.lock`:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.lock
```

Runtime packages are pinned through the build lock, which includes `requirements.lock` and PyInstaller. Build from a clean, trusted checkout; the executable packages the current working source files.

## Public build command

```powershell
.\build_pyos.ps1
```

The wrapper forwards version/resource parameters to `exe_tools\Build-pyOSSetup.ps1`. The orchestrator resolves Python in this order: explicit `-PythonPath`, project `.venv`, then `python` on `PATH`.

Common options:

```powershell
.\build_pyos.ps1 `
  -Version 2.0.0.0 `
  -ProductVersion 2.0 `
  -CompanyName pyOS `
  -FileDescription "pyOS Setup" `
  -IconPath pyos2.0.png `
  -OutputName pyOS-Setup `
  -FactoryNamespace "pyOS-Release-2.0-Factory" `
  -PythonPath .\.venv\Scripts\python.exe
```

`Version` must contain four numeric parts. Output and namespace values are validated before PyInstaller runs.

## Build stages

1. `Build-pyOSExe.ps1` runs `pyOS.spec` with `pyOSgui.py`, a windowed bootloader, and output name `pyOS.exe`.
2. The same spec runs with `PYOS_BUILD_ENTRY=pyOScli.py`, a console bootloader, and output name `pyOS-cli.exe`.
3. `pyOS-Setup.spec` analyzes `setup.py` and embeds both runtime executables under `runtime/`, plus application sources, documentation, sounds, licence, README, and artwork.
4. A SHA-256 manifest is generated for the final setup executable. Intermediate GUI/CLI executables and their hashes are removed from `dist/` because they are private installer payloads.

PyInstaller analysis/work files live under `build/`; distributable files live under `dist/`. Both directories are ignored by Git.

## Dependency collection

`pyOS.spec` explicitly collects optional packages whose imports or native/data files are not reliably discovered. This includes Pillow, FIDO2, Paramiko, pygame, psutil, tkinterweb, PythonMonkey, and `pminit`. The latter is required by PythonMonkey at runtime even though it is imported dynamically.

The runtime hook creates a factory-isolated configuration under the platform state directory. This prevents developer accounts, preferences, and local configuration from entering or influencing release builds.

## Verification and release

A successful local build must satisfy:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
git diff --check
$expected = ((Get-Content dist\pyOS-Setup.exe.sha256 -Raw) -split '\s+')[0]
$actual = (Get-FileHash dist\pyOS-Setup.exe -Algorithm SHA256).Hash
$expected -eq $actual
```

Release CI imports the signing certificate, builds through `build_pyos.ps1`, signs `pyOS-Setup.exe` with SHA-256 and a trusted timestamp, verifies Authenticode, regenerates the post-signing hash, uploads both files, and publishes them on the tagged GitHub release. Signing secrets and certificates must never be placed in the repository or payload.