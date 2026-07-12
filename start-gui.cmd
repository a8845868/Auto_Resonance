@echo off
cd /d "%~dp0"
set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYTHONW%" goto launch
set "PYTHONW=%~dp0..\..\.venv\Scripts\pythonw.exe"
if not exist "%PYTHONW%" goto missing

:launch
start "" "%PYTHONW%" "%~dp0gui_launcher.pyw"
exit /b 0

:missing
echo Python environment not found in this worktree or the main repository.
pause
exit /b 1
