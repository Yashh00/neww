@echo off
REM ==========================================================================
REM  maintdoc launcher for Windows
REM  - creates a local virtual environment (.venv) on first use
REM  - installs requirements (from .\wheels if present = fully offline install)
REM  - runs maintdoc commands, or shows a menu when started without arguments
REM  Processing is local and offline. All outputs are DRAFTS until qualified
REM  engineering review and formal release.
REM ==========================================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
set "CONFIG=config.yaml"
if not "%MAINTDOC_CONFIG%"=="" set "CONFIG=%MAINTDOC_CONFIG%"

if exist "%PY%" goto ready
echo [maintdoc] First start: creating virtual environment in .venv ...
where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3.11 -m venv .venv 2>nul
    if not exist "%PY%" py -3 -m venv .venv
) else (
    python -m venv .venv
)
if not exist "%PY%" (
    echo [maintdoc] ERROR: Python 3.11 or newer was not found. Install it from https://www.python.org/
    exit /b 1
)
"%PY%" -m pip install --upgrade pip >nul
if exist "wheels\" (
    echo [maintdoc] Installing packages offline from .\wheels ...
    "%PY%" -m pip install --no-index --find-links wheels -r requirements.txt
) else (
    echo [maintdoc] Installing packages from the package index ...
    "%PY%" -m pip install -r requirements.txt
)
if errorlevel 1 (
    echo [maintdoc] ERROR: package installation failed.
    exit /b 1
)

:ready
if not "%~1"=="" goto direct

:menu
echo.
echo  ====================================================================
echo   maintdoc - local maintenance document processor  (config: %CONFIG%)
echo   Outputs are DRAFTS until qualified engineering review and release.
echo  ====================================================================
echo   1  run-all      inventory - extract - validate - generate - verify - export
echo   2  inventory    discover and register source PDFs
echo   3  extract      page-level extraction with OCR fallback (resumable)
echo   4  validate     classify, values/units, duplicates, conflicts
echo   5  review       open the local review application in the browser
echo   6  generate     draft and approved master manuals (DOCX + PDF)
echo   7  verify       verification and acceptance criteria
echo   8  status       show counts and last verification result
echo   9  run-all --dry-run   preview without changing anything
echo   T  run tests    (pytest)
echo   Q  quit
echo.
set "choice="
set /p "choice=Select: "
if /i "%choice%"=="1" call :cmd run-all
if /i "%choice%"=="2" call :cmd inventory
if /i "%choice%"=="3" call :cmd extract
if /i "%choice%"=="4" call :cmd validate
if /i "%choice%"=="5" call :cmd review
if /i "%choice%"=="6" call :cmd generate
if /i "%choice%"=="7" call :cmd verify
if /i "%choice%"=="8" call :cmd status
if /i "%choice%"=="9" call :cmd run-all --dry-run
if /i "%choice%"=="T" "%PY%" -m pytest -q
if /i "%choice%"=="Q" exit /b 0
goto menu

:cmd
"%PY%" -m maintdoc -c "%CONFIG%" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" echo [maintdoc] finished OK.
if "%RC%"=="2" echo [maintdoc] VERIFICATION FAILED - see output\Validation_Report.xlsx
if "%RC%"=="3" echo [maintdoc] Verification incomplete - human review / visual checks pending.
if "%RC%"=="4" echo [maintdoc] Another maintdoc run is active (workspace locked).
echo.
pause
exit /b %RC%

:direct
"%PY%" -m maintdoc -c "%CONFIG%" %*
exit /b %ERRORLEVEL%
