@echo off
cd /d "%~dp0"
echo ============================================
echo   Game Memory Watcher
echo   Waiting for jjxf47.exe / jjxf46.exe ...
echo   It starts recording once the game launches,
echo   and stops automatically when the game exits.
echo   Keep this window open while playing.
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watch-mem.ps1" -IntervalSec 5 -MaxMinutes 240
echo.
echo Done. Press any key to close.
pause >nul
