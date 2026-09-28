# Builds the standalone Windows app (no Python needed to run it):
#   dist\iPadDisplay\iPadDisplay.exe            the app
#   dist\iPadDisplay-<version>-windows-x64.zip  the app + Install.bat, ready to share
#
# Usage:  powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = if (Test-Path '.venv\Scripts\python.exe') { '.venv\Scripts\python.exe' } else { 'python' }
& $python -m pip install --disable-pip-version-check -q -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw 'Installing the build requirements failed.' }

& $python packaging\make_ico.py
if ($LASTEXITCODE) { throw 'Making the icon failed.' }

# Absolute paths: PyInstaller resolves them relative to the spec file, which lives in build\.
& $python -m PyInstaller --noconfirm --clean --console --name iPadDisplay `
    --icon "$PSScriptRoot\packaging\icon.ico" `
    --add-data "$PSScriptRoot\web;web" `
    --collect-all dxcam `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build" --specpath "$PSScriptRoot\build" `
    server.py
if ($LASTEXITCODE) { throw 'PyInstaller failed.' }

$version = (Select-String -Path server.py -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
$stage = 'dist\package'
Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory $stage | Out-Null
Copy-Item 'dist\iPadDisplay' "$stage\iPadDisplay" -Recurse
Copy-Item 'packaging\Install.bat', 'packaging\install.ps1', 'packaging\uninstall.ps1', 'README.md' $stage

$zip = "dist\iPadDisplay-$version-windows-x64.zip"
Remove-Item $zip -ErrorAction SilentlyContinue
Compress-Archive -Path "$stage\*" -DestinationPath $zip
Remove-Item $stage -Recurse -Force
Write-Host "Built dist\iPadDisplay\iPadDisplay.exe and $zip"
