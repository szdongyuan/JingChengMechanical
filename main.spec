import os
import sys
from PyInstaller.utils.hooks import collect_submodules

snap7_dll = os.path.join(
    sys.prefix,
    "Lib",
    "site-packages",
    "snap7",
    "lib",
    "snap7.dll",
)

binaries = []
if os.path.exists(snap7_dll):
    binaries.append((snap7_dll, "snap7/lib"))

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=[
        ('data', 'data'),
    ],
    hiddenimports=collect_submodules('snap7'),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='精成电机',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)