# Removes iPad Display and its shortcuts. Your access key (%USERPROFILE%\.ipad-display-key)
# is kept, so a reinstall keeps working with the iPad's saved Home Screen icon.
$ErrorActionPreference = 'SilentlyContinue'
$target = Join-Path $env:LOCALAPPDATA 'Programs\iPad Display'

Get-Process iPadDisplay | Where-Object { $_.Path -like "$target*" } | Stop-Process -Force
Start-Sleep -Milliseconds 300
Remove-Item (Join-Path ([Environment]::GetFolderPath('Programs')) 'iPad Display') -Recurse -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Desktop')) 'iPad Display.lnk') -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'iPad Display.lnk') -Force
Set-Location $env:TEMP   # step out of the folder before deleting it
Remove-Item $target -Recurse -Force

Write-Host 'iPad Display has been removed.'
Start-Sleep -Seconds 2
