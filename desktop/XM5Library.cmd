@echo off
setlocal
set "APPDIR=%~dp0"
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONNOUSERSITE=1"
if not exist "%APPDIR%vendor\python\python.exe" (
  echo The bundled runtime is missing. Extract the complete portable package again.
  exit /b 1
)
if /i "%~1"=="cli" goto console
if /i "%~1"=="script" goto console
if /i "%~1"=="--check" goto console
if exist "%APPDIR%XM5Studio.exe" (
  start "XM5 Studio" "%APPDIR%XM5Studio.exe" %*
  exit /b 0
)
start "XM5 Studio" "%APPDIR%vendor\python\pythonw.exe" -B "%APPDIR%bootstrap.py" %*
exit /b 0
:console
"%APPDIR%vendor\python\python.exe" -B "%APPDIR%bootstrap.py" %*
exit /b %errorlevel%
