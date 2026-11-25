@echo off
rem Runs sportsedge with the project's own Python (the .venv folder), from any folder.
rem   .\sportsedge ui          the dashboard on this computer
rem   .\sportsedge share       the dashboard, reachable from anywhere
rem   .\sportsedge passcode    set the passcode for remote access
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo The project's Python environment is missing. Create it once with:
  echo   py -m venv .venv
  echo   .\.venv\Scripts\pip install -r requirements.txt
  exit /b 1
)
pushd "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m sportsedge %*
set SE_EXIT=%ERRORLEVEL%
popd
exit /b %SE_EXIT%
