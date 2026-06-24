@echo off
chcp 65001 >nul
cd /d "%~dp0"
python bot_control.py
pause
