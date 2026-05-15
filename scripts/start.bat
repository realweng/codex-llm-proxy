@echo off
REM Start Codex LLM Proxy on Windows (supports GLM and Kimi backends)
REM Usage: scripts\start.bat [-p <glm|kimi>]

setlocal

set "SCRIPT_DIR=%~dp0"
set "BACKEND=glm"
set "PROXY_PORT=18765"

if /I "%~1"=="-p" set "BACKEND=%~2"
if /I "%~1"=="/p" set "BACKEND=%~2"

REM Validate backend
if /I not "%BACKEND%"=="glm" if /I not "%BACKEND%"=="kimi" (
    echo Error: Unsupported provider '%BACKEND%'. Use 'glm' or 'kimi'.
    exit /b 1
)

REM Check for API key based on backend
if /I "%BACKEND%"=="kimi" (
    if "%KIMI_API_KEY%"=="" (
        echo Error: KIMI_API_KEY environment variable is not set
        echo Please run: set KIMI_API_KEY=your_api_key
        exit /b 1
    )
) else (
    if "%GLM_API_KEY%"=="" (
        echo Error: GLM_API_KEY environment variable is not set
        echo Please run: set GLM_API_KEY=your_api_key
        exit /b 1
    )
)

set "BACKEND=%BACKEND%"

REM Snapshot ~/.codex/config.toml and rewrite it to point Codex at this proxy.
python "%SCRIPT_DIR%codex_config.py" apply --port %PROXY_PORT% --backend %BACKEND%
if errorlevel 1 (
    echo Warning: codex config apply failed (proxy will still start)
)

echo Starting Codex LLM Proxy (backend: %BACKEND%)...
echo Press Ctrl+C to stop.
echo.
python "%SCRIPT_DIR%..\proxy.py"
