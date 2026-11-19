@echo off
rem 机甲旋风启动器 — 一键启动图形界面
rem 使用项目自带的虚拟环境 _build_tools\venv（已含 PySide6）
cd /d "%~dp0"
if not exist "_build_tools\venv\Scripts\activate.bat" (
    echo [错误] 未找到虚拟环境 _build_tools\venv，请先联系开发者初始化依赖。
    pause
    exit /b 1
)
call "_build_tools\venv\Scripts\activate.bat"
echo 正在启动图形界面...
python -m app.main pyside
if errorlevel 1 (
    echo.
    echo [错误] 图形界面启动失败，请把上面的报错信息反馈给开发者。
)
pause
