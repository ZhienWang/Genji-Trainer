@echo off
REM Double-click to start coaching. First run sets up a virtual environment.
setlocal
cd /d "%~dp0"

if exist .venv\Scripts\python.exe goto :run

REM Find Python 3.11+: try the py launcher first, then plain python.
set "PY="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.11 or newer wasn't found.
  echo.
  echo Install it from https://www.python.org/downloads/ and tick
  echo "Add python.exe to PATH" on the first screen of the installer.
  echo Or run this in a terminal:  winget install Python.Python.3.12
  echo.
  echo Then close this window and double-click run.bat again.
  pause
  exit /b 1
)

echo Setting up for the first time, this takes a minute...
%PY% -m venv .venv || goto :error
.venv\Scripts\python -m pip install -r requirements.txt || goto :error

:run
where claude >nul 2>nul
if errorlevel 1 (
  echo Claude Code isn't installed. Install it from https://claude.com/claude-code
  echo then run "claude" once and sign in with your Claude account.
  echo To use an API key instead, run: run.bat --backend api
  pause
  exit /b 1
)
.venv\Scripts\python -m genji_coach %*
pause
exit /b 0

:error
echo Setup failed. Delete the .venv folder and try again, or paste the error above to Claude.
pause
exit /b 1
