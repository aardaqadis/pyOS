# Security and updates

## Trust boundaries

pyOS treats downloaded releases, source archives, configuration, ownership metadata, filesystem links, website content, LAN input, and external command arguments as untrusted. Validation should occur before any persistent mutation or process launch.

## Installation ownership

Setup writes `.pyos-installation-owner.json` and `install_manifest.json` with matching product, schema, installation UUID, root, owned paths, and path types. Recursive ownership is restricted to explicitly approved trees. Before overwrite or removal, setup validates that:

- the manifest belongs to the selected installation root;
- paths are normalized relative paths without traversal or Windows-reserved names;
- parent resolution remains under the installation root;
- files have not been replaced by unsafe directories, links, or junctions;
- external paths are limited to known desktop shortcuts.

Unknown files are preserved. Manifest/configuration removal occurs only after payload removal succeeds.

## Configuration and credentials

Configuration and ownership files are written to unique temporary files and atomically replaced. Existing malformed configuration is not silently reset over user state. Authentication data is handled by `pyos_auth.py`; startup diagnostics must never display credential values. User-specific data resolves through a sanitized stable profile identifier rather than a raw username path.

## Source updates

Source updates require an immutable full commit ID, SHA-256 metadata, a trusted commit-to-archive digest binding, and a safe archive layout. Extraction rejects traversal, links, collisions, oversized archives, and excluded control/build paths.

The updater stages the new tree, writes a managed-source inventory, and overlays files transactionally. Replaced files are backed up; newly created files are recorded; retired managed files are removed only when prior ownership is established. Any failure rolls the overlay back. A cross-process lock prevents concurrent update mutation.

## Executable updates

Executable updates require a declared SHA-256 digest and a valid Authenticode signature whose thumbprint is present in the release trust set. Verification occurs before handoff/replacement. If trust cannot be established, the running executable remains unchanged.

The distributed artifact is now `pyOS-Setup.exe`. It installs or repairs pyOS; it must not be treated as a drop-in replacement for the installed `pyOS.exe` runtime. Runtime update selection should use source updates unless a separately signed runtime asset and matching handoff contract are published.

## Web and optional code execution

The browser's JavaScript support is experimental and disabled until the user explicitly enables it. PythonMonkey load failures disable JavaScript without crashing startup. Enabling scripts should be limited to trusted sites because DOM isolation is incomplete.

Custom applications and shell/command features should maintain explicit boundaries around filesystem access, process arguments, and UI ownership. Never interpolate untrusted values into a shell command; pass argument arrays and validate supported operations.

## Release checklist

- Run the complete test, lint, compile, and diff checks.
- Build from locked dependencies in a clean trusted checkout.
- Confirm no user profiles, credentials, tokens, certificates, `.git`, IDE state, caches, or test data are packaged.
- Sign the final setup executable and verify its timestamped Authenticode chain.
- Generate the SHA-256 manifest after signing.
- Test setup, repair, launchers, source update, rollback, and uninstall on a clean Windows machine.
- Publish only the intended executable and matching hash manifest.