@echo off
rem Double-click to open the Contract Checker.
rem You can also drag a folder of contracts onto this file.
setlocal
set HERE=%~dp0
for /f "delims=" %%P in ('py -c "import sys; print(sys.executable)" 2^>nul') do set PYTHON=%%P
set PYTHONW=%PYTHON:python.exe=pythonw.exe%
if exist "%PYTHONW%" (
    start "" "%PYTHONW%" "%HERE%xtenda_contract_check.py" "%~1"
) else (
    py "%HERE%xtenda_contract_check.py" "%~1"
    if errorlevel 1 pause
)
