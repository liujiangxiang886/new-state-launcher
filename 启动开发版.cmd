@echo off
set "PYTHONIOENCODING=gbk"
set "PYTHONUTF8=0"
cd /d "%~dp0"
title JJXF Launcher (dev, source mode)

echo ============================================
echo  JJXF Launcher - DEV / source mode
echo  Code changes take effect on restart.
echo ============================================
echo.
echo This console window belongs to the launcher.
echo Closing it exits the launcher.
echo.

"%~dp0_build_tools\venv\Scripts\python.exe" -m app.main
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo ============================================
  echo  [FAILED] exit code %RC%
  echo  Copy the Traceback above and send it over.
  echo ============================================
  pause
)