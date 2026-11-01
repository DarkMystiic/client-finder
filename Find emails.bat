@echo off
rem Finds emails for the leads you exported from Client Finder (Downloads\leads.csv, or drop a CSV on this file),
rem then writes outbox.txt with every email it WOULD send. Nothing is sent.
set "IN=%~1"
if "%IN%"=="" set "IN=%USERPROFILE%\Downloads\leads.csv"
for %%F in ("%IN%") do set "OUT=%%~dpnF_emails.csv"
python -I "%~dp0tools\outreach.py" find "%IN%" && python -I "%~dp0tools\outreach.py" send "%OUT%"
pause
