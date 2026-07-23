# Architecture

## Component map

| Component | Responsibility |
| --- | --- |
| `pyOSgui.py` | Tk desktop, built-in applications, startup diagnostics, preferences, and GUI task coordination. |
| `pyOScli.py` | Authenticated command center and command execution. |
| `pyos_config.py` | Validated configuration, storage ownership, user-profile paths, and atomic persistence. |
| `pyos_auth.py` | Account creation, credential verification, roles, password storage, and active sessions. |
| `pyos_updater.py` | Release discovery, source/executable verification, staging, rollback, and update handoff. |
| `setup.py` | GUI/unattended installation, repair, optional components, launchers, manifests, and uninstall. |
| `pyOS.spec` | Shared PyInstaller recipe for compiled GUI and CLI runtimes. |
| `pyOS-Setup.spec` | Packages setup, sources, resources, and both compiled runtimes into one installer. |

## Runtime flow

1. GUI startup creates a diagnostic screen and probes required and optional modules. Optional module failures are warnings and do not abort startup.
2. Configuration is loaded and validated. A missing configuration creates standalone first-run state; malformed existing configuration is accepted only through a valid backup recovery.
3. Interrupted update state is checked before authentication.
4. Authentication selects the active user and therefore the user-specific profile directory.
5. The desktop initializes preferences, services, and the application registry, then displays the shell.

The CLI uses the same configuration, authentication, data roots, and update code, so GUI and CLI operate on one installation and account store.

## Configuration and data

`pyos_config.py` selects platform-appropriate standalone state. Environment overrides such as `PYOS_HOME` and `PYOS_CONFIG_FILE` are used by factory builds and tests. The validated configuration records:

- installation directory;
- shared data directory;
- downloads directory;
- Drive B location;
- enabled application IDs;
- configured/first-run state.

Global data lives under the selected data root. After authentication, per-user settings and Drive B resolve beneath a stable, filesystem-safe profile directory. GUI and CLI settings are separate files inside that profile.

Writes use temporary files followed by atomic replacement where durability matters. Storage roots carry ownership metadata so pyOS can distinguish its files from unrelated user data.

## UI concurrency

Tk widgets are owned by the UI thread. `TkTaskManager` runs bounded background work, queues callbacks, rejects stale/closed results, and delivers completion callbacks on the owner thread. Network and other blocking work should use this mechanism rather than touching widgets from worker threads.

## Optional integrations

VLC provides media playback; Ollama backs local AI features; PythonMonkey and tkinterweb provide experimental JavaScript/HTML rendering. Missing optional integrations degrade their feature only. JavaScript remains opt-in because web scripts execute in an experimental environment.