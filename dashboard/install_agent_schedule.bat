@echo off
:: Wrapper so you can double-click or run from cmd/PowerShell:
::   install_agent_schedule.bat            install / update the task
::   install_agent_schedule.bat status     last run, next run, result code
::   install_agent_schedule.bat remove     delete the task
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_agent_schedule.ps1" %*
