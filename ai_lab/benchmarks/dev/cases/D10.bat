@echo off
"%~dp0bin\report.exe" --config "%~dp0settings.ini" %*
exit /b %errorlevel%
