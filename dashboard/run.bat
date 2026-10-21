@echo off
setlocal

echo.
echo  ============================================================
echo   Trading Dashboard — local launcher
echo  ============================================================

:: ── Find Python ──────────────────────────────────────────────────────────────
set PYTHON=
for %%P in (python3.exe python.exe) do (
  if not defined PYTHON (
    where %%P >nul 2>&1
    if not errorlevel 1 set PYTHON=%%P
  )
)

:: Also check common install locations
if not defined PYTHON (
  for %%D in (
    "%USERPROFILE%\AppData\Local\Programs\Python\Python312"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python311"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python310"
    "%USERPROFILE%\AppData\Local\Programs\Python\Python39"
    "C:\Python312"
    "C:\Python311"
    "C:\Python310"
  ) do (
    if not defined PYTHON (
      if exist "%%~D\python.exe" set PYTHON=%%~D\python.exe
    )
  )
)

if not defined PYTHON (
  echo.
  echo  ERROR: Python not found.
  echo.
  echo  Install Python 3.9+ from https://www.python.org/downloads/
  echo  Make sure to tick "Add Python to PATH" during installation.
  echo.
  pause
  exit /b 1
)

echo   Python : %PYTHON%

:: ── Install / verify deps ─────────────────────────────────────────────────────
echo.
echo  Installing / verifying dependencies...
%PYTHON% -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo  ERROR: pip install failed. Check your internet connection.
  pause
  exit /b 1
)

:: ── Launch ────────────────────────────────────────────────────────────────────
echo.
echo  Starting server...
echo  Open in browser: http://localhost:5000
echo  Press Ctrl+C to stop.
echo.
%PYTHON% app.py

pause
endlocal
