@echo off
rem Tracd for Windows: double-click to install (first time) and start.
setlocal
cd /d "%~dp0"
set "STAMP_CHECK=import hashlib,pathlib,sys; s=pathlib.Path('.venv/tracd-installed'); sys.exit(0 if s.exists() and s.read_text()==hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest() else 1)"
set "STAMP_WRITE=import hashlib,pathlib; pathlib.Path('.venv/tracd-installed').write_text(hashlib.sha256(pathlib.Path('pyproject.toml').read_bytes()).hexdigest())"

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
rem Install only on the first run or after an update changed what Tracd needs,
rem so a normal start works offline.
".venv\Scripts\python.exe" -c "%STAMP_CHECK%" 2>nul && goto :run
echo Installing what Tracd needs...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -e . || goto :fail
".venv\Scripts\python.exe" -c "%STAMP_WRITE%" || goto :fail

:run

echo.
".venv\Scripts\python.exe" -m tracd %*
exit /b %errorlevel%

:fail
echo.
echo  Setup failed. Check your internet connection and try again.
pause
exit /b 1
