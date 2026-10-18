' 在桌面创建 fund-sandbox 快捷方式
' 用法：cscript //nologo scripts\create_desktop_shortcut.vbs
'
' 为什么用 VBScript 而不是 PowerShell：
'   某些环境下从命令行调用 PowerShell 会被安全策略拦截，
'   而 WScript.Shell 的 COM 接口是 Windows 自带的传统方式，不受影响。

Option Explicit

Dim ws, shortcut, desktop, target

Set ws = CreateObject("WScript.Shell")
desktop = ws.SpecialFolders("Desktop")

' 相对路径：本脚本位于 fund-sandbox/scripts/，run.bat 在上一级目录
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\") - 1)
parentDir = Left(scriptDir, InStrRev(scriptDir, "\") - 1)
target   = parentDir & "\run.bat"

Set shortcut = ws.CreateShortcut(desktop & "\fund-sandbox.lnk")
shortcut.TargetPath = target
shortcut.WorkingDirectory = parentDir
shortcut.Description = "fund-sandbox 基金模拟交易沙盒"
shortcut.Save

WScript.Echo "快捷方式已创建: " & desktop & "\fund-sandbox.lnk"
WScript.Echo "指向: " & target
