# Installs NINA from this checkout on Windows: a virtualenv and a Start Menu entry.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1            # NINA only
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1 -Agents    # ...plus managed agents and the chat
#   ... -Hooks       also connect NINA to Claude Code (same as: nina --install-hooks)
#   ... -Shortcut    also add the Ctrl+Alt+N global shortcut (same as: nina --install-shortcut)
#   ... -Autostart   also start NINA when you log in
#   ... -Start       start NINA at the end
param(
    [switch]$Agents,
    [switch]$Hooks,
    [switch]$Shortcut,
    [switch]$Autostart,
    [switch]$Start
)
$ErrorActionPreference = "Stop"
$Dir = Split-Path -Parent $PSScriptRoot

function Find-Python {
    # The py launcher knows every installed version; otherwise the python on PATH.
    foreach ($v in "3.13", "3.12", "3.11", "3.10") {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py "-$v" -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { return @("py", "-$v") }
        }
    }
    $p = Get-Command python -ErrorAction SilentlyContinue
    if ($p -and $p.Source -notlike "*WindowsApps*") {
        & python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
        if ($LASTEXITCODE -eq 0) { return @("python") }
    }
    throw "Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/ and run this again."
}

$Venv = Join-Path $Dir ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$PyW = Join-Path $Venv "Scripts\pythonw.exe"

if (-not (Test-Path $Py)) {
    Write-Host "==> Creating the virtual environment"
    $python = Find-Python
    $exe, $pyArgs = $python
    & $exe @pyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "could not create the virtual environment" }
}

Write-Host "==> Installing NINA"
& $Py -m pip install --upgrade pip -q
$target = if ($Agents) { "$Dir[agents]" } else { $Dir }
& $Py -m pip install -q -e $target
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "==> Creating the Start Menu entry"
# No double quotes in this code: Windows PowerShell 5.1 does not escape them for native programs.
$code = @'
import sys
from nina import shortcut
lnk = shortcut.programs_dir() / 'NINA.lnk'
shortcut.make_lnk(lnk, sys.argv[1], '-m nina', '', 'Pixel-art mascot for your AI coding agents', shortcut.icon_file())
print(lnk)
'@
& $Py -c $code $PyW
if ($LASTEXITCODE -ne 0) { throw "could not create the Start Menu entry" }

$Nina = Join-Path $Venv "Scripts\nina.exe"
if ($Hooks) { Write-Host "==> Connecting to Claude Code"; & $Nina --install-hooks }
if ($Shortcut) { Write-Host "==> Global shortcut"; & $Nina --install-shortcut }
if ($Autostart) {
    Write-Host "==> Start with Windows"
    & $Py -c "from nina import launcher; launcher.set_autostart(True)"
}
if ($Start) { Start-Process -FilePath $PyW -ArgumentList "-m", "nina" }

Write-Host ""
Write-Host "Done. Start NINA from the Start Menu, or run:  $Nina"
Write-Host "Then right click the mascot and enable 'Connect to Claude Code' (or run: $Nina --install-hooks)."
