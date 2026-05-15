@echo off
REM Stop Codex LLM Proxy on Windows

setlocal

set "SCRIPT_DIR=%~dp0"

REM Always attempt to restore ~/.codex/config.toml (idempotent)
python "%SCRIPT_DIR%codex_config.py" restore
if errorlevel 1 (
    echo Warning: codex config restore failed
)

REM Try to find and stop proxy.py process
echo Stopping proxy...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'proxy\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('Stopped PID ' + $_.ProcessId) }"
