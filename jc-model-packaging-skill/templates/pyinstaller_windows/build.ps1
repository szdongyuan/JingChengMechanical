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

if ($BuildOneShot) {
    pyinstaller .\toolchain_oneshot.spec
}

if ($BuildResident) {
    pyinstaller .\toolchain_resident.spec
}

# Example copy steps when files stay external:
# Copy-Item -LiteralPath .\weights -Destination .\dist\toolchain-oneshot\weights -Recurse -Force
# Copy-Item -LiteralPath .\assets -Destination .\dist\toolchain-oneshot\assets -Recurse -Force
# Copy-Item -LiteralPath .\weights -Destination .\dist\toolchain-resident\weights -Recurse -Force
# Copy-Item -LiteralPath .\assets -Destination .\dist\toolchain-resident\assets -Recurse -Force
