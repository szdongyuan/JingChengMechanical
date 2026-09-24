import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_submodules

site_packages = os.environ.get(
    "PACKAGING_SITE_PACKAGES",
    os.path.join(sys.prefix, "Lib", "site-packages"),
)
runtime_root = os.environ.get("PACKAGING_PYTHON_RUNTIME", sys.prefix)
runtime_dependency_root = os.environ.get(
    "PACKAGING_RUNTIME_DEPENDENCIES",
    runtime_root,
)

snap7_dll = os.path.join(
    site_packages,
    "snap7",
    "lib",
    "snap7.dll",
)

def _runtime_scipy_module(name):
    return ".tests" not in name and not name.endswith(".tests")


scipy_datas, scipy_binaries, scipy_hiddenimports = collect_all(
    "scipy",
    include_py_files=False,
    filter_submodules=_runtime_scipy_module,
    exclude_datas=["**/tests/**"],
)

binaries = list(scipy_binaries)
binaries.append(("app/native/capture_buffer.dll", "app/native"))
if os.path.exists(snap7_dll):
    binaries.append((snap7_dll, "snap7/lib"))

for runtime_dll in (
    "python3.dll",
    "python314.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
):
    runtime_dll_path = os.path.join(runtime_root, runtime_dll)
    if os.path.exists(runtime_dll_path):
        binaries.append((runtime_dll_path, "."))

# The packaging interpreter is hosted from an extracted Python runtime.  Its
# extension modules and companion DLLs are not discoverable from sys.prefix,
# so carry the complete matching Python 3.14 extension set into the bundle.
if os.path.isdir(runtime_dependency_root):
    for filename in os.listdir(runtime_dependency_root):
        source = os.path.join(runtime_dependency_root, filename)
        lowered = filename.lower()
        if os.path.isfile(source) and (
            lowered.endswith(".pyd")
            or lowered in {
                "libcrypto-3.dll",
                "libffi-8.dll",
                "libssl-3.dll",
                "sqlite3.dll",
            }
        ):
            binaries.append((source, "."))

datas = [
    ('assets', 'assets'),
    *scipy_datas,
]

# The restricted packaging runtime loads Python 3.14's pure-Python standard
# library from a previously extracted PyInstaller archive. Runtime hooks may
# import modules that static analysis cannot see, so include every available
# standard-library top-level module as a filesystem fallback.
frozen_stdlib = os.path.join(runtime_root, "pyz_modules")
for module_name in sorted(
    {name.split(".", 1)[0] for name in sys.stdlib_module_names}
):
    package_dir = os.path.join(frozen_stdlib, module_name)
    module_file = os.path.join(frozen_stdlib, f"{module_name}.pyc")
    if os.path.isdir(package_dir):
        datas.append((package_dir, module_name))
    elif os.path.isfile(module_file):
        datas.append((module_file, "."))

# Include Qt's runtime DLLs explicitly. Binary dependency scanning cannot
# resolve them when the packaging interpreter is hosted from a copied runtime.
qt_bin = os.path.join(site_packages, "PyQt6", "Qt6", "bin")
if os.path.isdir(qt_bin):
    datas.append((qt_bin, "PyQt6/Qt6/bin"))

qt_plugins = os.path.join(site_packages, "PyQt6", "Qt6", "plugins")
if os.path.isdir(qt_plugins):
    datas.append((qt_plugins, "PyQt6/Qt6/plugins"))

block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        'app.service.capture_process.worker',
        *collect_submodules('snap7'),
        *scipy_hiddenimports,
    ],
    hookspath=[],
    runtime_hooks=['scripts/capture_spawn_hook.py'],
    excludes=[
        'PyQt5',
        'PySide2',
        'PySide6',
        'torch',
        'tensorflow',
        'matplotlib',
        'pytest',
        'IPython',
        'jupyter',
        'notebook',
        'pandas',
        'pygame',
        'tkinter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

# Codex's packaging environment adds Poppler's ICU 78 directory to PATH.
# PyInstaller may then collect its version-suffixed icuuc.dll as a dependency
# of Qt6Core.  Qt expects the unversioned Windows ICU exports, so that foreign
# DLL shadows System32's compatible icuuc.dll and makes PyQt6.QtGui fail with
# WinError 127.  Never ship those two Poppler ICU files with this application.
_foreign_icu_names = {"icuuc.dll", "icudt78.dll"}
a.binaries = [
    entry
    for entry in a.binaries
    if os.path.basename(entry[0]).lower() not in _foreign_icu_names
]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='精成电机',
    icon='assets/logo.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='精成电机',
)
