param(
    [int]$Port = 8742,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($NoBrowser) {
    python (Join-Path $root 'app.py') --port $Port
    exit $LASTEXITCODE
}

$python = py -3.11 -c "import sys; print(sys.executable)"
& $python (Join-Path $root 'desktop.py') --port $Port
exit $LASTEXITCODE
