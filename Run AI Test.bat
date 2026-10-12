@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto try_python
py -3 ai_test.py --interactive
set "task_result=%errorlevel%"
goto finished
:try_python
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 goto missing_python
python ai_test.py --interactive
set "task_result=%errorlevel%"
goto finished
:missing_python
echo Please install Python 3.10 or newer: https://www.python.org/downloads/
set "task_result=1"
:finished
pause
exit /b %task_result%
