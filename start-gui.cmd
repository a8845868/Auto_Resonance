@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if not "%HEIYUE_PYTHONW%"=="" (
    set "PYTHONW=%HEIYUE_PYTHONW%"
    if exist "%HEIYUE_PYTHONW%" goto launch
)

if not "%VIRTUAL_ENV%"=="" (
    set "PYTHONW=%VIRTUAL_ENV%\Scripts\pythonw.exe"
    if exist "%VIRTUAL_ENV%\Scripts\pythonw.exe" goto launch
)

set "PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYTHONW%" goto launch

set "COMMON_GIT_DIR="
for /f "usebackq delims=" %%I in (`git -C "%~dp0." rev-parse --path-format^=absolute --git-common-dir 2^>nul`) do set "COMMON_GIT_DIR=%%I"
if not "!COMMON_GIT_DIR!"=="" (
    for %%I in ("!COMMON_GIT_DIR!\..") do set "SHARED_REPOSITORY_ROOT=%%~fI"
    set "PYTHONW=!SHARED_REPOSITORY_ROOT!\.venv\Scripts\pythonw.exe"
    if exist "!PYTHONW!" goto launch
)

goto missing

:launch
if /i "%~1"=="--print-python" (
    echo %PYTHONW%
    exit /b 0
)
start "" "%PYTHONW%" "%~dp0gui_launcher.pyw"
exit /b 0

:missing
echo Unable to find the Auto_Resonance Python environment.
echo Checked HEIYUE_PYTHONW, VIRTUAL_ENV, this worktree, and the shared Git repository.
echo Set HEIYUE_PYTHONW to the full path of pythonw.exe if the environment is stored elsewhere.
pause
exit /b 1
