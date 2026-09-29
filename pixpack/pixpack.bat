@echo off
py -3 "%~dp0pixpack.py" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="9009" echo Cannot find the Python launcher py. Install Python 3.
exit /b %RC%
