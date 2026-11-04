param(
    [string]$DataDir,
    [ValidateRange(1,65535)][int]$Port = 18766
)
$ErrorActionPreference = 'Stop'
$packagedLauncher = Join-Path $PSScriptRoot 'PaperLearningAssistant.exe'
if (Test-Path -LiteralPath $packagedLauncher) {
    $packagedArguments = @('--port', [string]$Port)
    if ($DataDir) {
        $resolvedDataDir = [System.IO.Path]::GetFullPath($DataDir)
        $packagedArguments += @('--data-dir', ('"' + $resolvedDataDir.TrimEnd('\') + '"'))
    }
    Start-Process -FilePath $packagedLauncher -ArgumentList $packagedArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
    exit 0
}
$launcherPath = Join-Path $PSScriptRoot 'launcher.py'
$interpreter = $null
foreach ($candidate in @('.venv\Scripts\pythonw.exe', '.venv\Scripts\python.exe')) {
    $candidatePath = Join-Path $PSScriptRoot $candidate
    if (Test-Path -LiteralPath $candidatePath) { $interpreter = $candidatePath; break }
}
if (-not $interpreter) {
    foreach ($commandName in @('pythonw.exe', 'pyw.exe', 'python.exe', 'py.exe')) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) { $interpreter = $command.Source; break }
    }
}
if (-not $interpreter) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show('源码运行需要 Python 3.11 或更新版本及 requirements.txt 中的依赖。也可使用 Windows 打包版直接启动。', '文献学习助手') | Out-Null
    exit 1
}
$launchArguments = @(('"' + $launcherPath + '"'), '--port', [string]$Port)
if ($DataDir) {
    $resolvedDataDir = [System.IO.Path]::GetFullPath($DataDir)
    $launchArguments += @('--data-dir', ('"' + $resolvedDataDir.TrimEnd('\') + '"'))
}
Start-Process -FilePath $interpreter -ArgumentList $launchArguments -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
