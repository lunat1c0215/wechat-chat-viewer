<#
.SYNOPSIS
    打包发布压缩包：release\微信聊天记录查看器-v<版本>-win64.zip
.EXAMPLE
    .\scripts\release.ps1
#>
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$version = (Get-Content "$root\VERSION" -Raw).Trim()
$appDir = "$root\dist\微信聊天记录查看器"
if (-not (Test-Path $appDir)) {
    throw "未找到构建产物，请先运行 .\scripts\build.ps1"
}

$stage = "$root\build\release-stage"
$zip = "$root\release\微信聊天记录查看器-v$version-win64.zip"

if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage -Force | Out-Null

Copy-Item "$appDir\*" $stage -Recurse -Force
Copy-Item "$root\启动查看器.bat" $stage -Force
Copy-Item "$root\README.md" $stage -Force
Copy-Item "$root\VERSION" $stage -Force
Copy-Item "$root\LICENSE" $stage -Force

New-Item -ItemType Directory -Path "$root\release" -Force | Out-Null
if (Test-Path $zip) { Remove-Item $zip -Force }

Compress-Archive -Path "$stage\*" -DestinationPath $zip -CompressionLevel Optimal

$size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "[release] 完成：$zip（$size MB）"
