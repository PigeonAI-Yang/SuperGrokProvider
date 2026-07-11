$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv-build'
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    py -3.11 -m venv $venv
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $root 'requirements-desktop.txt')
& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name SuperGrokRouter `
    --icon (Join-Path $root 'static\app-icon.ico') `
    --add-data "$(Join-Path $root 'static');static" `
    --hidden-import webview.platforms.edgechromium `
    (Join-Path $root 'desktop.py')

Write-Host "Built: $(Join-Path $root 'dist\SuperGrokRouter.exe')"
