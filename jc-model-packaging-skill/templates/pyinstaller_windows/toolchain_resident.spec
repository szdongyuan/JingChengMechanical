# PyInstaller spec for the resident stdio entrypoint.

from pathlib import Path


block_cipher = None

SOURCE_ROOT = Path(r"C:/REPLACE/WITH/PACKAGE_ROOT")
ENTRY_SCRIPT = SOURCE_ROOT / "resident_cli.py"
WEIGHTS_DIR = SOURCE_ROOT / "weights"
ASSET_DIR = SOURCE_ROOT / "assets"

datas = []
if WEIGHTS_DIR.exists():
    datas.append((str(WEIGHTS_DIR), "weights"))
if ASSET_DIR.exists():
    datas.append((str(ASSET_DIR), "assets"))

a = Analysis(
    [str(ENTRY_SCRIPT)],
    pathex=[str(SOURCE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="toolchain-resident",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name="toolchain-resident",
)
