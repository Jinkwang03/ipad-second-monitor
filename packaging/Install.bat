@echo off
rem Installs iPad Display for this Windows user (no admin rights needed).
rem Add -AutoStart to also start it when you log in:  Install.bat -AutoStart
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
pause
