@echo off
:: Called by the "TradingScannerAgent" scheduled task every 15 minutes.
:: Runs one agent cycle and appends its console output to scheduler.out.
call "%~dp0..\run_agent.bat" cycle >> "%~dp0scheduler.out" 2>&1
