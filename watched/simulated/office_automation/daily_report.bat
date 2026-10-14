@echo off
REM Scheduled task: runs every weekday at 7:30
cd /d "%~dp0"
set INCOMING=C:\Users\amy.chen\Documents\Reports\incoming
python merge_monthly_excel.py "%INCOMING%" > last_run.log 2>&1
if errorlevel 1 (
    echo Merge failed, see last_run.log
    exit /b 1
)
for %%f in (C:\Users\amy.chen\Documents\Reports\merged_*.xlsx) do set LATEST=%%f
python send_report.py "%LATEST%" >> last_run.log 2>&1
