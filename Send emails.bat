@echo off
rem Sends the emails listed in outbox.txt from your Gmail (spaced 45-90 s apart, daily limit in %%USERPROFILE%%\.clientfinder\config.json).
set "IN=%~1"
if "%IN%"=="" set "IN=%USERPROFILE%\Downloads\leads_emails.csv"
python -I "%~dp0tools\outreach.py" send "%IN%" --send
pause
