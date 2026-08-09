[CmdletBinding()]
param(
    [string]$OutputDirectory = "$env:LOCALAPPDATA\AutoResonance\Launcher",
    [string]$TargetRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
    [switch]$CreateStartMenuShortcut
)

$ErrorActionPreference = 'Stop'
$compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$source = Join-Path $PSScriptRoot 'stable_gui_launcher\StableGuiLauncher.cs'
$icon = Join-Path (Resolve-Path (Join-Path $PSScriptRoot '..')).Path 'app\resource\images\logo.ico'
$resolvedTarget = (Resolve-Path -LiteralPath $TargetRoot).Path
$startScript = Join-Path $resolvedTarget 'start-gui.cmd'
$pythonLauncher = Join-Path $resolvedTarget 'gui_launcher.pyw'

if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw "C# compiler not found: $compiler"
}
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Launcher source not found: $source"
}
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Launcher icon not found: $icon"
}
if (-not (Test-Path -LiteralPath $startScript -PathType Leaf) -or
    -not (Test-Path -LiteralPath $pythonLauncher -PathType Leaf)) {
    throw "Target is not a valid Auto_Resonance GUI root: $resolvedTarget"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputExe = Join-Path $OutputDirectory 'AutoResonance.exe'
$targetFile = Join-Path $OutputDirectory 'launcher-target.txt'

& $compiler /nologo /target:winexe /optimize+ `
    "/win32icon:$icon" `
    /reference:System.Windows.Forms.dll `
    "/out:$outputExe" `
    $source
if ($LASTEXITCODE -ne 0) {
    throw "Stable GUI launcher compilation failed with exit code $LASTEXITCODE"
}

[System.IO.File]::WriteAllText(
    $targetFile,
    $resolvedTarget + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)

$validation = Start-Process -FilePath $outputExe `
    -ArgumentList '--validate-only' `
    -Wait `
    -PassThru `
    -WindowStyle Hidden
if ($validation.ExitCode -ne 0) {
    throw "Stable GUI launcher validation failed with exit code $($validation.ExitCode)"
}

if ($CreateStartMenuShortcut) {
    $programs = [Environment]::GetFolderPath('Programs')
    $shortcutPath = Join-Path $programs '黑月无人驾驶.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($shortcutPath)
    $shortcut.TargetPath = $outputExe
    $shortcut.WorkingDirectory = $OutputDirectory
    $shortcut.IconLocation = "$outputExe,0"
    $shortcut.Description = '启动 Auto_Resonance 黑月无人驾驶'
    $shortcut.Save()
    Write-Output "START_MENU_SHORTCUT=$shortcutPath"
}

Write-Output "LAUNCHER_EXE=$outputExe"
Write-Output "TARGET_ROOT=$resolvedTarget"
