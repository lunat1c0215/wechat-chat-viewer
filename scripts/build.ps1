<#
.SYNOPSIS
    构建微信聊天记录查看器（PyInstaller onedir）。
.EXAMPLE
    .\scripts\build.ps1
#>
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = "python"
if (Test-Path "$root\.venv\Scripts\python.exe") {
    $python = "$root\.venv\Scripts\python.exe"
}

$version = (Get-Content "$root\VERSION" -Raw).Trim()
Write-Host "[build] version = $version"

& $python -m PyInstaller `
    --noconfirm --clean `
    --onedir --windowed `
    --name "微信聊天记录查看器" `
    --distpath "$root\dist" `
    --workpath "$root\build" `
    --specpath "$root\build" `
    --collect-all imageio_ffmpeg `
    --hidden-import zstandard `
    --hidden-import psutil `
    --hidden-import Crypto `
    "$root\src\微信聊天记录查看器.py"

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 构建失败，退出码 $LASTEXITCODE"
}

$exe = "$root\dist\微信聊天记录查看器\微信聊天记录查看器.exe"
if (-not (Test-Path $exe)) {
    throw "未找到构建产物：$exe"
}

$size = [math]::Round(((Get-ChildItem "$root\dist\微信聊天记录查看器" -Recurse -File |
    Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "[build] 完成：$exe（$size MB）"
