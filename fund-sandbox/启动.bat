@echo off
chcp 65001 >nul
cd /d "%~dp0"
title fund-sandbox 基金模拟交易沙盒

:: 一键启动（便携版）：直接拉起 Electron 桌面窗口，后端由 Electron 自动带起。
:: 无需目标机安装 Python / Node —— 运行时已随包（runtime\python、electron\node_modules）。

if not exist "%~dp0runtime\python\python.exe" (
  echo [错误] 未找到随包 Python 运行时：runtime\python\python.exe
  echo 请确认整个 fund-sandbox 文件夹已完整复制。
  pause
  exit /b 1
)
if not exist "%~dp0electron\node_modules\electron\dist\electron.exe" (
  echo [错误] 未找到随包 Electron 运行时：electron\node_modules\electron\dist\electron.exe
  echo 请确认 electron\node_modules 已随包。
  pause
  exit /b 1
)

cd /d "%~dp0electron"
start "" "%~dp0electron\node_modules\electron\dist\electron.exe" .
exit
