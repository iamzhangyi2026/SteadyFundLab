@echo off
chcp 65001 >nul
cd /d "%~dp0"
title fund-sandbox 基金模拟交易沙盒

:MENU
cls
echo ============================================================
echo    fund-sandbox  基金模拟交易沙盒
echo    虚拟资金 + 真实规则，AI 主导执行
echo ============================================================
echo.
echo    [1] 每日数据刷新（抓净值/板块/状态，含交叉验证）
echo    [2] 数据源体检（接口失效时先跑这个）
echo    [3] 查看运行状态（混合模式/心跳/AI 是否在线）
echo    [4] 资金预算 + 费率 + FIFO 演示
echo    [5] 持仓穿透与可投范围校验
echo    [6] 基金交易状态（限购/暂停/海外节假日）
echo    [7] 订单闭环演示（下单到建仓）
echo    [9] 启动桌面窗口（Electron 原生窗口，双击即用）
echo    [10] 仅启动后端服务（浏览器开 127.0.0.1:8765，备用）
echo    [0] 退出
echo.
set /p choice=  请输入序号后回车:

if "%choice%"=="1" goto FETCH
if "%choice%"=="2" goto VERIFY
if "%choice%"=="3" goto STATUS
if "%choice%"=="4" goto FEES
if "%choice%"=="5" goto HOLDINGS
if "%choice%"=="6" goto FSTATUS
if "%choice%"=="7" goto ORDERS
if "%choice%"=="9" goto DESKTOP
if "%choice%"=="10" goto SERVE
if "%choice%"=="0" goto END
goto MENU

:FETCH
echo.
runtime\python\python.exe scripts\fetch_daily.py
goto PAUSE

:VERIFY
echo.
runtime\python\python.exe scripts\verify_sources.py
goto PAUSE

:STATUS
echo.
runtime\python\python.exe -m backend.engine.coordinator
goto PAUSE

:FEES
echo.
runtime\python\python.exe -m backend.engine.budget
echo.
runtime\python\python.exe -m backend.engine.fees
echo.
runtime\python\python.exe -m backend.engine.ledger
goto PAUSE

:HOLDINGS
echo.
runtime\python\python.exe -m backend.engine.holdings
goto PAUSE

:FSTATUS
echo.
runtime\python\python.exe -m backend.engine.status
goto PAUSE

:ORDERS
echo.
runtime\python\python.exe -m backend.engine.orders
goto PAUSE

:DESKTOP
echo.
echo   正在启动 Electron 桌面窗口……
echo   （会自动拉起 Python 后端，窗口关闭即退出）
echo.
if not exist "%~dp0electron\node_modules\electron\dist\electron.exe" (
  echo   [提示] 检测到 electron 运行时缺失，请确认 electron\node_modules 已随包，
  echo   或先在 electron 目录执行 npm install。
  goto PAUSE
)
cd /d "%~dp0electron"
start "" "%~dp0electron\node_modules\electron\dist\electron.exe" .
cd /d "%~dp0"
goto PAUSE

:SERVE
echo.
echo   仅启动后端服务（浏览器备用模式）。
echo   服务启动后，在浏览器打开：
echo.
echo        http://127.0.0.1:8765
echo.
echo   只监听本机回环地址，外部无法访问。
echo   看门狗已启用：进程异常退出后自动重启；关闭本窗口可彻底停止。
echo.
:SERVE_LOOP
runtime\python\python.exe -m backend.api.main
echo.
echo   [看门狗] 后端进程已退出（退出码 %errorlevel%），3 秒后自动重启……
echo   （若需彻底停止，请直接关闭本窗口）
timeout /t 3 /nobreak >nul
goto SERVE_LOOP

:PAUSE
echo.
echo ------------------------------------------------------------
pause
goto MENU

:END
exit
