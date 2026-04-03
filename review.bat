@echo off
REM review.bat — Windows 启动脚本
REM 用法：review.bat <PR链接或PR编号> [--owner X --repo Y] [--backend agent|api]

setlocal

set "SCRIPT_DIR=%~dp0"

REM 优先使用虚拟环境
if exist "%SCRIPT_DIR%.venv\Scripts\python.exe" (
    set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
    goto :run
)

REM 尝试 py launcher
where py >nul 2>&1
if %ERRORLEVEL% == 0 (
    set "PYTHON=py"
    goto :run
)

REM 回退到 python
where python >nul 2>&1
if %ERRORLEVEL% == 0 (
    set "PYTHON=python"
    goto :run
)

echo [ERROR] 未找到 Python，请先安装 Python 3 或创建虚拟环境 .venv
exit /b 1

:run
"%PYTHON%" "%SCRIPT_DIR%review_draft.py" %*
