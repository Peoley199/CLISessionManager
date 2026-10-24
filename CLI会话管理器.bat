@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 依赖 PATH 中的 Python：优先用 pythonw（无控制台窗口），回退到 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw cli_session_manager.py
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python cli_session_manager.py
    goto :eof
)

echo(
echo 未找到 Python。请先安装 Python 3.10 或更高版本，
echo 并在安装时勾选 "Add Python to PATH"（或手动把 Python 加入 PATH）。
echo 下载地址：https://www.python.org/downloads/
echo(
pause
