@echo off
cd /d "%~dp0"

rem 发布包：exe 与本文件同级
if exist "微信聊天记录查看器.exe" (
    start "" "微信聊天记录查看器.exe"
    exit /b
)

rem 源码仓库：构建产物
if exist "dist\微信聊天记录查看器\微信聊天记录查看器.exe" (
    start "" "dist\微信聊天记录查看器\微信聊天记录查看器.exe"
    exit /b
)

rem 回退：直接运行源码（无控制台窗口）
if exist "src\微信聊天记录查看器.py" (
    start "" pythonw "src\微信聊天记录查看器.py"
    exit /b
)

echo 未找到程序文件。请先运行 scripts\build.ps1 构建，或使用发布包。
pause
