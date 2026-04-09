param(
    [switch]$BuildOneShot = $true,
    [switch]$BuildResident = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# onedir is the practical default for ML payloads because sidecar model files
# and non-Python assets stay visible and easy to update.
# onefile is optional when single-file delivery matters more than startup time.
# If you keep model files external, copy them into the built output after the
# PyInstaller step for the matching runtime shape.

Push-Location -LiteralPath $PSScriptRoot
try {
    if ($BuildOneShot) {
        pyinstaller (Join-Path -Path $PSScriptRoot -ChildPath "toolchain_oneshot.spec")
    }

    if ($BuildResident) {
        pyinstaller (Join-Path -Path $PSScriptRoot -ChildPath "toolchain_resident.spec")
    }
}
finally {
    Pop-Location
}

# Example copy steps when files stay external:
# Copy-Item -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "weights") -Destination (Join-Path -Path $PSScriptRoot -ChildPath "dist\toolchain-oneshot\weights") -Recurse -Force
# Copy-Item -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "assets") -Destination (Join-Path -Path $PSScriptRoot -ChildPath "dist\toolchain-oneshot\assets") -Recurse -Force
# Copy-Item -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "weights") -Destination (Join-Path -Path $PSScriptRoot -ChildPath "dist\toolchain-resident\weights") -Recurse -Force
# Copy-Item -LiteralPath (Join-Path -Path $PSScriptRoot -ChildPath "assets") -Destination (Join-Path -Path $PSScriptRoot -ChildPath "dist\toolchain-resident\assets") -Recurse -Force
