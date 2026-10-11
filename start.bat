@echo off
rem Tracd for Windows: double-click to install (first time) and start.
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Tracd needs Python 3.10 or newer.
  echo  Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then run this again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Setting up Tracd for the first time. This takes a minute...
  %PY% -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check --upgrade . || goto :fail

echo.
".venv\Scripts\python.exe" -m tracd %*
exit /b %errorlevel%

:fail
echo.
echo  Setup failed. Check your internet connection and try again.
pause
exit /b 1
