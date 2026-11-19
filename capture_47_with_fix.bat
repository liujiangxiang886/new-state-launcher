@echo off
pushd "%~dp0"
echo ============================================
echo 4.7 capture - WITH avatar_fix overlay
echo Start game, create new char / import save,
echo go to avatar screen, see white portrait,
echo then CLOSE game window.
echo Logs -> _perf47\run47\
echo ============================================
"%~dp0_build_tools\venv\Scripts\python.exe" "_perf47\capture_47.py"
echo.
echo [DONE] logs in _perf47\run47\  (engine_stderr.txt)
pause
