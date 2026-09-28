# Installs iPad Display for the current Windows user (no admin rights needed):
# copies the app to %LOCALAPPDATA%\Programs\iPad Display and adds Start menu and desktop shortcuts.
#
#   Install.bat                                              (double-click)
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-AutoStart]
#
# -AutoStart also starts iPad Display every time you log in to Windows.
param([switch]$AutoStart)
$ErrorActionPreference = 'Stop'

# Next to this script in a release zip; in dist\ when run from the source folder after build.ps1.
$source = Join-Path $PSScriptRoot 'iPadDisplay'
if (-not (Test-Path "$source\iPadDisplay.exe")) { $source = Join-Path $PSScriptRoot '..\dist\iPadDisplay' }
if (-not (Test-Path "$source\iPadDisplay.exe")) { throw 'iPadDisplay.exe was not found. Build it first with build.ps1.' }

$target = Join-Path $env:LOCALAPPDATA 'Programs\iPad Display'
Get-Process iPadDisplay -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$target*" } | Stop-Process -Force
Start-Sleep -Milliseconds 300
if (Test-Path $target) { Remove-Item $target -Recurse -Force }
New-Item -ItemType Directory $target -Force | Out-Null
Copy-Item "$source\*" $target -Recurse
Copy-Item (Join-Path $PSScriptRoot 'uninstall.ps1') $target
# A downloaded zip marks every file as "from the internet"; this is your own install, so clear that.
Get-ChildItem $target -Recurse -File | Unblock-File

$exe = Join-Path $target 'iPadDisplay.exe'
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$Path, [string]$TargetPath, [string]$Arguments = '',
                      [string]$Description = 'Use your iPad as a second monitor') {
    $link = $shell.CreateShortcut($Path)
    $link.TargetPath = $TargetPath
    $link.Arguments = $Arguments
    $link.WorkingDirectory = $target
    $link.IconLocation = "$exe,0"
    $link.Description = $Description
    $link.Save()
}

$menu = Join-Path ([Environment]::GetFolderPath('Programs')) 'iPad Display'
New-Item -ItemType Directory $menu -Force | Out-Null
New-Shortcut "$menu\iPad Display.lnk" $exe
New-Shortcut "$menu\Uninstall iPad Display.lnk" 'powershell.exe' `
    "-NoProfile -ExecutionPolicy Bypass -File `"$target\uninstall.ps1`"" 'Remove iPad Display'
New-Shortcut (Join-Path ([Environment]::GetFolderPath('Desktop')) 'iPad Display.lnk') $exe
if ($AutoStart) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'iPad Display.lnk') $exe
}

Write-Host ''
Write-Host "iPad Display is installed in $target"
Write-Host 'Start it from the "iPad Display" shortcut on your desktop or in the Start menu.'
if ($AutoStart) { Write-Host 'It will also start automatically when you log in.' }
