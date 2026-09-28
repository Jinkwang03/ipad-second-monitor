@echo off
setlocal
rem iPad Display launcher. First run creates a private Python environment in .venv
rem and installs the requirements; later runs start the server straight away.
rem Extra arguments are passed through, e.g.  run.bat --list  or  run.bat --monitor 2
pushd "%~dp0"

if not exist ".venv\installed.ok" (
  echo Setting up iPad Display - first run only, this takes a minute...
  if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul && py -3 -m venv .venv
    if not exist ".venv\Scripts\python.exe" python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo Could not create a Python environment. Install Python 3.10 or newer from
    echo https://www.python.org/downloads/ and run this again.
    goto :end
  )
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Installing the requirements failed - check your internet connection and run this again.
    goto :end
  )
  echo ok> ".venv\installed.ok"
)

".venv\Scripts\python.exe" server.py %*

:end
popd
pause
