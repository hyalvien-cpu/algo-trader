@echo off
chcp 65001 >nul
title 智能模拟交易系统
color 0A

echo ============================================
echo    智能模拟交易系统 - Windows 启动器
echo ============================================
echo.

:: 检查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python！
    echo.
    echo 请先安装 Python 3.10+：
    echo   https://www.python.org/downloads/
    echo.
    echo 安装时务必勾选 "Add Python to PATH"
    echo.
    pause
    exit /b 1
)

echo [1/3] 检测 Python...
python --version
echo.

:: 安装依赖
echo [2/3] 安装依赖（首次较慢，请耐心等待）...
pip install flask yfinance apscheduler requests vaderSentiment --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    echo.
    echo [提示] pip 安装出错，尝试用 pip3...
    pip3 install flask yfinance apscheduler requests vaderSentiment --quiet --disable-pip-version-check
)
echo    依赖安装完成！
echo.

:: 启动
echo [3/3] 启动交易系统...
echo.
echo ============================================
echo    浏览器会自动打开，如果没有请手动访问：
echo    http://localhost:5200
echo.
echo    关闭此窗口即可停止系统
echo ============================================
echo.

python sim_trader_web.py

pause
