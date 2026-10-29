# 一键打包：PyInstaller -> dist\AIOps_Agent\ -> dist\AIOps_Agent.zip
# 用法：powershell -ExecutionPolicy Bypass -File build\build.ps1

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "[1/3] 清理旧产物..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "$root\build\pyinstaller", "$root\dist", "$root\build\AIOps_Agent.spec" -ErrorAction SilentlyContinue

Write-Host "[2/3] PyInstaller 打包..." -ForegroundColor Cyan
python -m PyInstaller `
    --noconfirm `
    --clean `
    --name AIOps_Agent `
    --onedir `
    --console `
    --add-data "$root\src\index.html;." `
    --distpath "$root\dist" `
    --workpath "$root\build\pyinstaller" `
    --specpath "$root\build" `
    "$root\src\main.py"

Write-Host "[3/3] 压缩发布包..." -ForegroundColor Cyan
Compress-Archive -Path "$root\dist\AIOps_Agent" -DestinationPath "$root\dist\AIOps_Agent.zip" -Force

$zip = Get-Item "$root\dist\AIOps_Agent.zip"
Write-Host ("完成：{0}（{1:N1} MB）" -f $zip.FullName, ($zip.Length / 1MB)) -ForegroundColor Green
