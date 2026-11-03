@echo off
chcp 65001 >nul
python "%~dp0scripts\start.py" --demo
if errorlevel 1 pause
