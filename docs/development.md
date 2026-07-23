# Development and testing

## Environment

Create a dedicated environment and install the locked development set:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
```

Use `requirements.lock` for the reproducible runtime, `requirements-build.lock` for release tooling, and `requirements-optional.lock` for optional packages. `setup.py` contains supported package ranges for end-user source installations; lock changes should be deliberate and reviewed.

## Running from source

```powershell
.\.venv\Scripts\python.exe pyOSgui.py
.\.venv\Scripts\python.exe pyOScli.py
```

Both surfaces share configuration and accounts. Use isolated environment variables or temporary directories in tests so development runs do not modify real profiles.

## Checks

```powershell
.\.venv\Scripts\python.exe -m py_compile `
  pyOSgui.py pyOScli.py pyos_config.py pyos_auth.py pyos_updater.py setup.py
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
git diff --check
```

Pytest discovers maintained tests under `tests/`. The suite covers GUI task/thread safety, configuration and storage ownership, authentication, setup/uninstall safety, network boundaries, CLI command safety, and updater trust/rollback. Hardware and UI integrations are mocked where deterministic automation is required.

## Change guidelines

- Keep Tk widget operations on the UI thread; perform blocking work through the task manager.
- Treat filesystem paths from configuration, archives, manifests, and users as untrusted. Resolve and validate containment before mutation.
- Preserve unknown user files. Never expand an ownership manifest implicitly during repair or uninstall.
- Use atomic writes for configuration, credentials, manifests, and update state.
- Optional dependencies must fail locally to their feature rather than aborting core startup.
- Do not weaken digest, immutable-reference, Authenticode, symlink/junction, or rollback checks to make an update pass.
- Keep source and frozen modes working when changing setup. Frozen setup uses embedded executables; source setup uses `.venv`.

## Adding a dependency or source asset

1. Add the supported range to `setup.py` when source installations need it.
2. Update the appropriate lock file and verify transitive pins.
3. Add dynamic/native packages to `pyOS.spec` collection when PyInstaller cannot infer them.
4. Add required files or trees to setup payload declarations and `pyOS-Setup.spec`.
5. Add a regression test that fails when the payload is incomplete.
6. Build and smoke-test the final setup executable on a clean Windows profile.