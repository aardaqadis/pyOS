## Audit result

I completed a read-only audit of all 12,944 Python lines, build scripts, packaging, documentation, and the current executable. The project imports and parses successfully, but I would not release it until the P0 issues below are fixed. No files were changed; the worktree remains clean.

### P0 — fix before release

1. **Standalone uninstall can delete an unrelated `~/apps` directory.**

   Standalone settings live directly under home ([fallback path](C:/Users/rich/PyCharmMiscProject/pyos_config.py:135)), making the custom-app directory `~/apps` ([derivation](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:3425)). Uninstall then recursively removes it ([deletion list](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:1602)). This also occurs when configuration loading fails.

   Move every standalone artifact under a dedicated root such as `~/.pyos/` or `%LOCALAPPDATA%\pyOS`. Only recursively delete directories containing a pyOS ownership marker and manifest. Generic `~/virtual_drives.json` and `~/remembered_session.json` have the same collision problem.

2. **Authentication fails open on corrupt state.**

   An unreadable or malformed credential database becomes an empty database ([loader](C:/Users/rich/PyCharmMiscProject/pyos_auth.py:119)); the next login becomes first-account creation and therefore a new administrator ([creation logic](C:/Users/rich/PyCharmMiscProject/pyos_auth.py:215)). Invalid configuration can also switch pyOS to a different credential path.

   Distinguish “missing” from “invalid.” Corruption must fail closed into an explicit recovery flow, retaining validated backups.

3. **Non-ASCII usernames cause permanent lockout.**

   Validation accepts Unicode, but username lookup uses `hmac.compare_digest()` on strings ([lookup](C:/Users/rich/PyCharmMiscProject/pyos_auth.py:137)), which raises `TypeError` for non-ASCII text. I reproduced this with `josé`.

   Normalize usernames using Unicode normalization plus `casefold()`, then compare normally or compare UTF-8 bytes.

4. **Authorization is incomplete.**

   Any standard user can remove every account/profile ([global uninstall](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:1524)), approve global updates ([update flow](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:2013)), and launch Setup. Require recent administrator authentication for all global mutations.

   Also, roles cannot securely isolate mutually untrusted users while everyone has arbitrary Python and host-filesystem access. Either describe roles as cooperative UI policy, or move real authorization into an OS-protected service/account.

5. **“Lock CLI” does not lock the application.**

   It only resets flags ([lock implementation](C:/Users/rich/PyCharmMiscProject/pyOScli.py:418)); menus and file open/delete remain usable, and remembered credentials can silently unlock the next command. Use a modal full-window lock, gate every action, and disable remembered-session authentication after an explicit lock.

6. **Updates are neither strongly authenticated nor transactional.**

   Source-update hashes are recorded but never checked against trusted metadata ([source install](C:/Users/rich/PyCharmMiscProject/pyos_updater.py:117)). Executables are accepted using an `MZ` header, with digest verification skipped when no digest is supplied ([executable validation](C:/Users/rich/PyCharmMiscProject/pyos_updater.py:184)). Files are then replaced individually without rollback, and executable updates are marked installed before the helper succeeds ([helper flow](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:2135)).

   Require an immutable commit plus signed release manifest, mandatory digest, and Authenticode verification. Stage a complete version, acquire a cross-process lock, atomically switch versions, and acknowledge success only after the new build starts.

7. **Installer layouts can lead to data loss.**

   Setup accepts the same or nested directory for installation, data, and downloads ([validation](C:/Users/rich/PyCharmMiscProject/setup.py:88)). I confirmed all three can be identical. Uninstall subsequently removes the installation tree while claiming data is preserved ([uninstall](C:/Users/rich/PyCharmMiscProject/setup.py:567)).

   Reject overlapping protected directories, require an installation ownership marker, delete only manifest-listed files, and remove configuration only after successful uninstall.

### High-priority reliability and data integrity

- **Tkinter threading is unsafe and inconsistent.** The Command Center starts a worker for every command ([thread launch](C:/Users/rich/PyCharmMiscProject/pyOScli.py:635)), but that worker directly modifies Tk widgets through methods such as [log_message](C:/Users/rich/PyCharmMiscProject/pyOScli.py:1759). The desktop has similar teardown races. Introduce one bounded task manager whose workers emit plain results into a queue drained solely by Tk.

- **Shutdown is incomplete.** Global shutdown primarily stops Messenger; IDE processes, media resources, AI requests, timers, servers, and update tasks can survive or be interrupted ([shutdown](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:1511), [main](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:9162)). Add one idempotent lifecycle manager and `WM_DELETE_WINDOW` handler.

- **Several operations freeze the GUI.** Browser dependency installation and page loading run synchronously ([browser setup](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:8227), [page load](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:8287)); router mapping and connection enumeration can also block. Dependencies should never be installed automatically from the UI.

- **Editors can silently lose or alter data.** Text Editor clears its buffer before a read succeeds, decodes with replacement, saves everything as UTF-8, and lacks unsaved-change prompts ([editor load/save](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:7435)). The IDE, App Maker, and Modding Environment have related problems.

- **Paint can overwrite a large original with an 800×520 thumbnail.** The working image is downsampled while retaining the original path ([open](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:6771)), then normal Save overwrites it. Preserve full-resolution source data or require Save As after resizing.

- **Failed UPnP cleanup can leave a permanent public port mapping.** Mapping state is discarded before deletion is confirmed, while the lease is indefinite ([cleanup](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:5663), [lease](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:5714)). Use finite renewable leases and retain state until confirmed removal.

- **Persistence is race-prone.** Credentials and configuration use unlocked read-modify-write operations with fixed temporary filenames ([credential save](C:/Users/rich/PyCharmMiscProject/pyos_auth.py:168), [config save](C:/Users/rich/PyCharmMiscProject/pyos_config.py:43)). GUI and CLI can overwrite one another. SQLite transactions or cross-process locking are appropriate.

- **Standalone accounts disappear after Setup.** Credentials move from `~/.pyos_credentials.json` to the configured data directory without migration ([credential path](C:/Users/rich/PyCharmMiscProject/pyos_auth.py:25), [configuration write](C:/Users/rich/PyCharmMiscProject/setup.py:286)).

### Architecture and product consistency

- `DesktopGUI` spans 7,827 lines and 93 methods; its largest method is 698 lines. Split apps into controllers with `open/close/dispose`, plus shared services for tasks, theming, storage, networking, and lifecycle.

- `_run_command()` is a 433-line conditional dispatcher ([command implementation](C:/Users/rich/PyCharmMiscProject/pyOScli.py:653)). Replace it with a command registry and testable handlers.

- Drive A is documented as temporary but is stored persistently inside the user profile ([GUI path](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:3209), [CLI path](C:/Users/rich/PyCharmMiscProject/pyOScli.py:135)). Either implement true temporary lifecycle behavior or relabel it.

- Setup’s optional-app selection only hides desktop icons; disabled apps remain accessible through menus and command-line launchers ([icon filtering](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:2817), [unfiltered menu](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:1456)). Drive every surface from one application registry.

- Custom apps execute unrestricted Python synchronously inside the desktop process ([execution](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:3430)). A blocking or hostile app can freeze or control pyOS. Prefer subprocess isolation with IPC.

- Async Weather, News, AI, SSH, and IDE operations need request-generation IDs so stale completions cannot overwrite newer state. Messenger history, connection threads, images, and persistent notifications also need bounded limits.

- Theme and accessibility support is incomplete: many widgets hard-code black/white, and label-based controls lack keyboard activation. Use named styles and real focusable buttons.

- Sudoku puzzles are not checked for unique solutions, yet only the generated solution is accepted ([generation/checking](C:/Users/rich/PyCharmMiscProject/pyOSgui.py:4331)).

### Build, release, and documentation

The tracked executable passed PE and packaged-file privacy checks, but:

- It is **58.27 MiB**, while README says 42.37 MiB.
- Version and ProductVersion are blank.
- It is unsigned.
- Its archive does not contain the documented factory-isolation runtime hook.
- Two competing build flows exist: [basic build](C:/Users/rich/PyCharmMiscProject/build_pyos.ps1:12) and [release build](C:/Users/rich/PyCharmMiscProject/exe_tools/Build-pyOSExe.ps1:77).
- The release test reports signature/version state but does not fail when they are missing ([release test](C:/Users/rich/PyCharmMiscProject/exe_tools/Test-pyOSExe.ps1:22)).
- The 58 MiB executable is tracked in Git; the local repository currently contains about 550 MiB of loose objects.

Use one CI-controlled release build, require factory isolation, version metadata, signing, artifact hashes, and smoke tests, then publish the executable as a release artifact instead of committing it.

There is also no lockfile, test suite, or CI configuration. Add `pyproject.toml`, pinned/locked dependencies, Ruff, pytest, a dependency audit, and Windows/macOS/Linux CI. The highest-value tests are auth corruption/Unicode/concurrency, path-safe uninstall, updater rollback/signatures, editor and Paint preservation, GUI shutdown with active jobs, CLI command parsing, and build-artifact assertions.

### Checks that passed

- All Python modules parsed and imported successfully.
- All PowerShell scripts and `pyOS.spec` parsed successfully.
- `pip check` found no broken installed requirements.
- The executable is a valid PE and contains no detected credential/settings files.
- Calculator evaluation is AST-restricted, Messenger validates packet sizes, hosted paths have traversal guards, and password hashing uses salted 600,000-iteration PBKDF2-SHA256.

The best implementation order is: storage/deletion safety → authentication/configuration → updater/release security → task/lifecycle manager → editor/Paint safeguards → modularization and automated tests.