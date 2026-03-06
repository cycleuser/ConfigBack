@echo off
REM ConfigBack - PyPI Upload Script (Windows)
REM Usage:
REM   upload_pypi.bat          Upload to PyPI
REM   upload_pypi.bat --test   Upload to TestPyPI

echo === ConfigBack PyPI Upload ===

set REPO_FLAG=
if "%1"=="--test" (
    set REPO_FLAG=--repository testpypi
    echo Target: TestPyPI
) else (
    echo Target: PyPI
)

REM Clean old builds
echo Cleaning old builds...
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
for /d %%i in (*.egg-info) do rmdir /s /q "%%i"

REM Install build tools
echo Installing build tools...
pip install --upgrade build twine
if %errorlevel% neq 0 goto :error

REM Build
echo Building package...
python -m build
if %errorlevel% neq 0 goto :error

REM Check
echo Checking package...
twine check dist\*
if %errorlevel% neq 0 goto :error

REM Upload
echo Uploading...
twine upload %REPO_FLAG% dist\*
if %errorlevel% neq 0 goto :error

echo.
echo === Upload complete! ===
if "%1"=="--test" (
    echo View at: https://test.pypi.org/project/configback/
) else (
    echo View at: https://pypi.org/project/configback/
)
goto :end

:error
echo.
echo === Upload FAILED ===
exit /b 1

:end
