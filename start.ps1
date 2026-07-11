param(
    [int]$Port = 8742,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'

if (-not $NoBrowser -and [Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    $relayArgs = @('-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass', '-File', $PSCommandPath, '-Port', $Port)
    & powershell.exe @relayArgs
    exit $LASTEXITCODE
}

$createdNew = $false
$launcherMutex = [Threading.Mutex]::new($true, 'Local\SuperGrokRouter.Launcher', [ref]$createdNew)
if (-not $createdNew) {
    $launcherMutex.Dispose()
    exit 0
}

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:$Port"

if ($NoBrowser) {
    try {
        python (Join-Path $root 'app.py') --port $Port
    } finally {
        $launcherMutex.ReleaseMutex()
        $launcherMutex.Dispose()
    }
    exit
}

$browserCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$browser = $browserCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $browser) {
    throw 'Chrome or Edge is required to open the 1280x720 app window.'
}

$dataDir = Join-Path $env:LOCALAPPDATA 'SuperGrokRouter'
$backend = Start-Process python -ArgumentList @((Join-Path $root 'app.py'), '--port', $Port) -WindowStyle Hidden -PassThru
try {
    for ($i = 0; $i -lt 50; $i++) {
        try {
            Invoke-RestMethod "$url/health" -TimeoutSec 1 | Out-Null
            break
        } catch {
            Start-Sleep -Milliseconds 200
        }
    }
    if ($i -eq 50) { throw 'The local service did not become healthy in time.' }
    $profile = Join-Path $dataDir 'browser-profile'
    $window = Start-Process $browser -ArgumentList @("--app=$url", '--window-size=1280,720', "--user-data-dir=$profile", '--no-first-run') -PassThru

    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class SuperGrokWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr hWnd, uint message, IntPtr wParam, IntPtr lParam);
}
'@

    $script:windowHandle = [IntPtr]::Zero
    $script:windowHidden = $false
    $script:windowIconApplied = $false
    $tray = New-Object System.Windows.Forms.NotifyIcon
    $appIcon = [System.Drawing.Icon]::new((Join-Path $root 'static\app-icon.ico'))
    $tray.Icon = $appIcon
    $tray.Text = 'SuperGrok Router'
    $menu = New-Object System.Windows.Forms.ContextMenuStrip
    $openItem = $menu.Items.Add((-join [char[]](0x6253, 0x5F00)))
    $exitItem = $menu.Items.Add((-join [char[]](0x9000, 0x51FA)))
    $tray.ContextMenuStrip = $menu

    $restoreWindow = {
        if ($script:windowHandle -ne [IntPtr]::Zero -and [SuperGrokWindow]::IsWindow($script:windowHandle)) {
            [SuperGrokWindow]::ShowWindow($script:windowHandle, 5) | Out-Null
            [SuperGrokWindow]::ShowWindow($script:windowHandle, 9) | Out-Null
            [SuperGrokWindow]::SetForegroundWindow($script:windowHandle) | Out-Null
            $script:windowHidden = $false
            $tray.Visible = $false
        }
    }
    $openItem.add_Click($restoreWindow)
    $tray.add_DoubleClick($restoreWindow)
    $exitItem.add_Click({ [System.Windows.Forms.Application]::ExitThread() })

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 250
    $timer.add_Tick({
        $window.Refresh()
        if ($window.HasExited) {
            [System.Windows.Forms.Application]::ExitThread()
            return
        }
        if ($script:windowHandle -eq [IntPtr]::Zero -and $window.MainWindowHandle -ne [IntPtr]::Zero) {
            $script:windowHandle = $window.MainWindowHandle
        }
        if (-not $script:windowIconApplied -and $script:windowHandle -ne [IntPtr]::Zero) {
            [SuperGrokWindow]::SendMessage($script:windowHandle, 0x0080, [IntPtr]1, $appIcon.Handle) | Out-Null
            [SuperGrokWindow]::SendMessage($script:windowHandle, 0x0080, [IntPtr]0, $appIcon.Handle) | Out-Null
            $script:windowIconApplied = $true
        }
        if ($script:windowHandle -ne [IntPtr]::Zero -and -not [SuperGrokWindow]::IsWindow($script:windowHandle)) {
            [System.Windows.Forms.Application]::ExitThread()
            return
        }
        if (-not $script:windowHidden -and $script:windowHandle -ne [IntPtr]::Zero -and [SuperGrokWindow]::IsIconic($script:windowHandle)) {
            [SuperGrokWindow]::ShowWindow($script:windowHandle, 0) | Out-Null
            $script:windowHidden = $true
            $tray.Visible = $true
        }
    })
    $timer.Start()
    [System.Windows.Forms.Application]::Run()
} finally {
    if ($timer) { $timer.Stop(); $timer.Dispose() }
    if ($tray) { $tray.Visible = $false; $tray.Dispose() }
    if ($appIcon) { $appIcon.Dispose() }
    if ($menu) { $menu.Dispose() }
    if ($window -and -not $window.HasExited) { Stop-Process -Id $window.Id -ErrorAction SilentlyContinue }
    if (-not $backend.HasExited) { Stop-Process -Id $backend.Id }
    $launcherMutex.ReleaseMutex()
    $launcherMutex.Dispose()
}
