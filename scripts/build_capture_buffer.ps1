param(
    [string]$VisualStudioPath = 'C:/Program Files/Microsoft Visual Studio/18/Community',
    [switch]$WithTests
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$buildDir = Join-Path $repoRoot 'native/capture_buffer/build'
$assetDir = Join-Path $repoRoot 'app/native'
$vcvars = Join-Path $VisualStudioPath 'VC/Auxiliary/Build/vcvars64.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC x64 environment missing: $vcvars" }
New-Item -ItemType Directory -Force -Path $buildDir, $assetDir | Out-Null
# cmd is used solely for MSVC environment setup/compiler execution, not file operations.
$source = Join-Path $repoRoot 'native/capture_buffer/capture_buffer.c'
$headerDir = Join-Path $repoRoot 'native/capture_buffer'
$dll = Join-Path $assetDir 'capture_buffer.dll'
$lib = Join-Path $buildDir 'capture_buffer.lib'
$obj = Join-Path $buildDir 'capture_buffer.obj'
$compile = 'call "{0}" >nul && cl /nologo /W4 /WX /O2 /MT /LD /DCAPTURE_BUILD /I"{1}" "{2}" /Fo"{3}" /link /OUT:"{4}" /IMPLIB:"{5}"' -f $vcvars, $headerDir, $source, $obj, $dll, $lib
& cmd.exe /d /s /c $compile
if ($LASTEXITCODE -ne 0) { throw 'Native capture DLL compilation failed' }
if ($WithTests) {
    $driver = Join-Path $repoRoot 'tests/native_capture/capture_test_driver.c'
    $driverDll = Join-Path $repoRoot 'tests/native_capture/capture_test_driver.dll'
    $driverObj = Join-Path $buildDir 'capture_test_driver.obj'
    $driverLib = Join-Path $buildDir 'capture_test_driver.lib'
    $compile = 'call "{0}" >nul && cl /nologo /W4 /WX /O2 /MT /LD /I"{1}" "{2}" "{3}" /Fo"{4}" /link /OUT:"{5}" /IMPLIB:"{6}"' -f $vcvars, $headerDir, $driver, $lib, $driverObj, $driverDll, $driverLib
    & cmd.exe /d /s /c $compile
    if ($LASTEXITCODE -ne 0) { throw 'Native capture test driver compilation failed' }
}
