# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

project_root = Path(SPECPATH).resolve()
output_name = os.environ.get("PYOS_SETUP_OUTPUT_NAME", "pyOS-Setup")
icon_path = os.environ.get("PYOS_BUILD_ICON", str(project_root / "pyos2.0.png"))
version_file = os.environ.get("PYOS_BUILD_VERSION_FILE", str(project_root / "exe_tools" / "version_info.txt"))

datas = [
    (str(project_root / "pyOSgui.py"), "."),
    (str(project_root / "pyOScli.py"), "."),
    (str(project_root / "pyos_config.py"), "."),
    (str(project_root / "pyos_auth.py"), "."),
    (str(project_root / "pyos_updater.py"), "."),
    (str(project_root / "setup.py"), "."),
    (str(project_root / "README.md"), "."),
    (str(project_root / "LICENSE.md"), "."),
    (str(project_root / "pyos2.0.png"), "."),
    (str(project_root / "sounds"), "sounds"),
    (str(project_root / "dist" / "pyOS.exe"), "runtime"),
    (str(project_root / "dist" / "pyOS-cli.exe"), "runtime"),
]

a = Analysis(
    [str(project_root / "setup.py")],
    pathex=[str(project_root)],
    binaries=[], datas=datas, hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [], name=output_name,
    debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
    upx_exclude=[], runtime_tmpdir=None, console=False,
    disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon=[icon_path], version=version_file if os.name == "nt" else None,
)