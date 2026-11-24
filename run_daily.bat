@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m najdi_praci.cli %*
    exit /b %ERRORLEVEL%
)

where uv >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    uv run najdi-praci %*
    exit /b %ERRORLEVEL%
)

python -m najdi_praci.cli %*
exit /b %ERRORLEVEL%
