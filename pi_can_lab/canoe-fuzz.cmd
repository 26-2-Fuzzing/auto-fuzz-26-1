@echo off
setlocal

if defined AUTOFUZZ_PYTHON if exist "%AUTOFUZZ_PYTHON%" goto run_python

set "AUTOFUZZ_PYTHON=%~dp0.venv-win\Scripts\python.exe"
if exist "%AUTOFUZZ_PYTHON%" goto run_python

set "AUTOFUZZ_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%AUTOFUZZ_PYTHON%" goto run_python

where py >nul 2>&1
if not errorlevel 1 goto run_launcher

set "AUTOFUZZ_PYTHON=python"
goto run_python

:run_launcher
py -3 "%~dp0canoe_fuzzer.py" %*
exit /b %ERRORLEVEL%

:run_python
"%AUTOFUZZ_PYTHON%" "%~dp0canoe_fuzzer.py" %*
exit /b %ERRORLEVEL%
