@echo off
chcp 65001 >nul
title 知了 · 电脑版
cd /d "%~dp0"

echo.
echo   ============================================================
echo    知了 · 电脑版
echo    只开一个窗口，看全学校所有通知
echo   ============================================================
echo.

rem 两种运行方式：
rem   1) 打包成便携版后，用自带的 python\pythonw.exe（不依赖本机 Python）
rem   2) 从源码直接跑，用 PATH 里的 pythonw
rem 用 pythonw.exe 而不是 python.exe：避免多弹一个黑窗口。
rem 注意：这里调用的是 ASCII 文件名的入口 quanzhi_desktop.py。
rem 直接用中文文件名启动会被 Windows 静默杀掉（返回 0、无任何输出）。

if exist "%~dp0python\pythonw.exe" (
    echo   使用自带 Python 运行时...
    start "" "%~dp0python\pythonw.exe" "%~dp0quanzhi_desktop.py"
) else (
    echo   未找到自带运行时，使用系统 Python...
    start "" pythonw "%~dp0quanzhi_desktop.py"
)

timeout /t 3 /nobreak >nul
exit
