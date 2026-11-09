@echo off
setlocal
if not exist "%~dp0vendor\python\python.exe" (
  echo XM5 bundled Python is missing. Run the normal setup first. This command never starts the GUI. 1>&2
  exit /b 2
)
"%~dp0vendor\python\python.exe" -B -s -m xm5lib.control.client %*
