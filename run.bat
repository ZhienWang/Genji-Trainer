@echo off
REM Double-click to start coaching. First run sets up a virtual environment.
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv || goto :error
  .venv\Scripts\pip install -r requirements.txt || goto :error
)
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
echo Setup failed. Make sure Python 3.11 or newer is installed from python.org.
pause
exit /b 1
