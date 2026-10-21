@echo off
:: Scanner agent launcher.  Usage:  run_agent.bat <command>
::   run_agent.bat replay      (one-time: back-fill a year of history for the daily scanners)
::   run_agent.bat daemon      (leave running: scans, validates and reports on the IST clock)
::   run_agent.bat cycle       (what Task Scheduler calls every 15 min)
::   run_agent.bat report | picks | status | validate | scan
setlocal
cd /d "%~dp0"

set PYTHON=
for %%P in (python.exe python3.exe) do (
  if not defined PYTHON (
    where %%P >nul 2>&1
    if not errorlevel 1 set PYTHON=%%P
  )
)
if not defined PYTHON (
  for %%D in (
    "%USERPROFILE%\AppData\Local\Programs\Python\Python313"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python312"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python311"
  ) do (
    if not defined PYTHON if exist "%%~D\python.exe" set PYTHON=%%~D\python.exe
  )
)
if not defined PYTHON (
  echo Python not found. Install Python 3.10+ and tick "Add to PATH".
  exit /b 1
)

if not exist "scanner_agent\.deps_ok" (
  %PYTHON% -m pip install -r scanner_agent\requirements.txt --quiet --disable-pip-version-check && echo ok> "scanner_agent\.deps_ok"
)

if "%~1"=="" (
  %PYTHON% -m scanner_agent --help
) else (
  %PYTHON% -m scanner_agent %*
)
endlocal
