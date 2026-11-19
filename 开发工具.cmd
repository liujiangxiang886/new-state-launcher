@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PY=%~dp0_build_tools\venv\Scripts\python.exe"
set "PYW=%~dp0_build_tools\venv\Scripts\pythonw.exe"
set "LAUNCHER=%~dp0机甲旋风兼容启动器.spec"
set "AVAILABLE=0"
rem Python 输出默认 UTF-8，而 cmd 控制台按 GBK 解码，会显示成乱码。
rem 统一让 Python 子进程也用 GBK 输出。
set "PYTHONIOENCODING=gbk"
set "PYTHONUTF8=0"

if exist "%PY%" set "AVAILABLE=1"

:MENU
cls
echo ================================================================
echo   机甲旋风兼容启动器 - 开发工具
echo   目录：%CD%
echo ================================================================
if "%AVAILABLE%"=="0" (
  echo.
  echo   [错误] 找不到 Python 环境：
  echo          %PY%
  echo   请确认 _build_tools\venv 存在。
  echo.
  pause
  exit /b 1
)
echo.
echo   --- 日常使用 ---
echo    [1] 启动启动器（源码态，改动即时生效）
echo    [2] 启动启动器（捕获报错，出问题用这个）
echo.
echo   --- 验证 ---
echo    [3] 快速自检（版本 / 两档校验）
echo    [4] 跑全部测试（约 3 分钟，605 项）
echo    [5] 只跑 UI 相关测试（约 30 秒）
echo    [6] 冒烟 4.6（真启动一次，约 30 秒）
echo    [7] 冒烟 4.7（真启动一次，约 30 秒）
echo.
echo   --- 打包 ---
echo    [8] 打包开发者版 exe
echo    [9] 打包玩家版 exe
echo.
echo   --- 其他 ---
echo    [0] 退出
echo.
set "CHOICE="
set /p "CHOICE=请输入编号后回车："

if /i "%CHOICE%"=="1" goto RUN_SILENT
if /i "%CHOICE%"=="2" goto RUN_VERBOSE
if /i "%CHOICE%"=="3" goto SELFTEST
if /i "%CHOICE%"=="4" goto TEST_ALL
if /i "%CHOICE%"=="5" goto TEST_UI
if /i "%CHOICE%"=="6" goto SMOKE46
if /i "%CHOICE%"=="7" goto SMOKE47
if /i "%CHOICE%"=="8" goto PACK_DEV
if /i "%CHOICE%"=="9" goto PACK_PLAYER
if /i "%CHOICE%"=="0" exit /b 0

echo.
echo   [!] 没有这个编号：%CHOICE%
ping -n 3 127.0.0.1 >nul
goto MENU


:RUN_SILENT
echo.
echo   正在启动（无控制台窗口）...
start "" "%PYW%" -m app.main
echo   已启动。若无窗口出现，请用 [2] 看报错。
ping -n 4 127.0.0.1 >nul
goto MENU


:RUN_VERBOSE
echo.
echo   正在启动（保留控制台，可看报错）...
echo   ------------------------------------------------
"%PY%" -m app.main
echo   ------------------------------------------------
echo   已退出，退出码 %ERRORLEVEL%
pause
goto MENU


:SELFTEST
echo.
echo   快速自检...
echo   ------------------------------------------------
"%PY%" -c "import sys; sys.path.insert(0,'.'); from app import edition; print('版本  :', edition.label()); from app.ui.pyside import features; print('4.6 档:', features.available_modes('4.6')); print('4.7 档:', features.available_modes('4.7')); from app.core import config as c; cfg=c.load(); print('引擎线:', c.known_lines(cfg)); print('基线  :', list((cfg.get('baselines') or {}).keys()))"
echo   ------------------------------------------------
pause
goto MENU


:TEST_ALL
echo.
echo   跑全部测试（约 3 分钟）...
echo   ------------------------------------------------
set "QT_QPA_PLATFORM=offscreen"
"%PY%" -m pytest tests/ -q
echo   ------------------------------------------------
echo   结束，退出码 %ERRORLEVEL%
pause
goto MENU


:TEST_UI
echo.
echo   只跑 UI 相关测试...
echo   ------------------------------------------------
set "QT_QPA_PLATFORM=offscreen"
"%PY%" -m pytest tests/test_ui_integration.py tests/test_detail_mode.py -q
echo   ------------------------------------------------
echo   结束，退出码 %ERRORLEVEL%
pause
goto MENU


:SMOKE46
echo.
echo   冒烟 4.6（会真启动游戏，约 30 秒）...
echo   存档已隔离到 profiles\smoke46，不会动你的存档。
echo   ------------------------------------------------
"%PY%" -m app.main cli smoke 4.6 --profile smoke46 --timeout 100
echo   ------------------------------------------------
echo   结束，退出码 %ERRORLEVEL%
pause
goto MENU


:SMOKE47
echo.
echo   冒烟 4.7（会真启动游戏，约 30 秒）...
echo   存档已隔离到 profiles\smoke47，不会动你的存档。
echo   ------------------------------------------------
"%PY%" -m app.main cli smoke 4.7 --profile smoke47 --timeout 100
echo   ------------------------------------------------
echo   结束，退出码 %ERRORLEVEL%
pause
goto MENU


:PACK_DEV
echo.
echo   打包开发者版...
set "DEST=%~dp0_tmp_perf\dist-dev"
set "WORK=%~dp0builds\_pyi_dev"
set "JJXF_EDITION=dev"
call :DO_PACK
goto MENU


:PACK_PLAYER
echo.
echo   打包玩家版...
set "DEST=%~dp0_tmp_perf\dist-player"
set "WORK=%~dp0builds\_pyi_player"
set "JJXF_EDITION=player"
call :DO_PACK
goto MENU


:DO_PACK
echo   输出：%DEST%
echo   ------------------------------------------------
"%~dp0_build_tools\venv\Scripts\pyinstaller.exe" "%LAUNCHER%" --noconfirm --distpath "%DEST%" --workpath "%WORK%"
set "RC=%ERRORLEVEL%"
echo   ------------------------------------------------
if not "%RC%"=="0" (
  echo   [失败] 退出码 %RC%
  pause
  exit /b %RC%
)
echo   打包完成：%DEST%\机甲旋风兼容启动器\机甲旋风兼容启动器.exe
echo.
echo   注意：config / baseline / runtime / engines / schemas
echo         这些数据目录不在 exe 内，需要复制到 exe 同级才能运行。
echo.
pause
exit /b 0