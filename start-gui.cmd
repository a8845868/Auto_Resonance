@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" goto missing
start "" ".venv\Scripts\pythonw.exe" "gui_launcher.pyw"
exit /b 0

:missing
echo Python environment not found: .venv\Scripts\pythonw.exe
pause
exit /b 1
