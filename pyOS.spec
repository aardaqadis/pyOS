# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH).resolve()


def project_file(relative_path):
    return str(project_root / relative_path)


icon_path = os.environ.get("PYOS_BUILD_ICON", project_file("pyos2.0.png"))
output_name = os.environ.get("PYOS_BUILD_OUTPUT_NAME", "pyOS")
runtime_hook = os.environ.get(
    "PYOS_BUILD_RUNTIME_HOOK", project_file("exe_tools/factory_runtime.py")
)
version_file = os.environ.get(
    "PYOS_BUILD_VERSION_FILE", project_file("exe_tools/version_info.txt")
)

datas = [
    (project_file("pyOSgui.py"), "."),
    (project_file("pyOScli.py"), "."),
    (project_file("pyos_config.py"), "."),
    (project_file("pyos_auth.py"), "."),
    (project_file("pyos_updater.py"), "."),
    (project_file("setup.py"), "."),
    (project_file("README.md"), "."),
    (project_file("LICENSE.md"), "."),
    (icon_path, "."),
]
binaries = []
hiddenimports = ["chess", "vlc"]
for package in (
    "fido2",
    "PIL",
    "mido",
    "paramiko",
    "pygame",
    "psutil",
    "pythonmonkey",
    "tkinterweb",
):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports


a = Analysis(
    [project_file("pyOSgui.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[runtime_hook],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=output_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[icon_path],
    version=version_file if os.name == "nt" else None,
)
