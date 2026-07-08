@echo off
REM ============================================================================
REM  setup-windows.bat  -  ONE-TIME Windows driver setup so CAS can FLASH (Root/Seal).
REM
REM  Windows does not ship the USB drivers CAS needs to flash a unit. This installs them
REM  into the Windows DRIVER STORE, so EVERY unit auto-binds on plug - no Zadig, and no
REM  per-device setup (that was the Zadig trap: it binds per unit-serial, so you had to
REM  redo it for each device). Two drivers get installed:
REM     * fastboot / bootloader (Retroid / AYN / Odin)  - so `fastboot devices` sees the unit
REM     * Qualcomm EDL 9008 (MANGMI)                     - so the EDL COM port appears
REM
REM  Run it ONCE per Windows PC. Safe to re-run. It installs PC-side drivers ONLY - it does
REM  NOT flash, reboot, or modify any device. (Linux needs none of this - udev covers it.)
REM
REM  The actual work is in drivers\install-drivers.ps1 (this .bat just elevates + runs it).
REM ============================================================================
setlocal
pushd "%~dp0"

REM --- writing to the driver store needs admin; self-elevate if we're not already. ---
net session >nul 2>&1
if %errorlevel% NEQ 0 (
  echo [*] requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  popd
  endlocal
  exit /b
)

echo ============================================================================
echo   CAS - Windows driver setup (one-time; publishes to the Windows driver store)
echo   Installs the fastboot + EDL USB drivers so every unit binds on plug (no Zadig).
echo   PC-side only: does NOT flash or modify any device.
echo ============================================================================
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0drivers\install-drivers.ps1"
set "RC=%errorlevel%"

echo.
if "%RC%"=="0" (
  echo [ok] Driver setup complete. Unplug/replug a unit, then run ^(0^) Root in CAS.
  echo      This PC is now set for the whole fleet - you do NOT repeat this per device.
) else (
  echo [X] Driver setup hit an error ^(exit %RC%^). Scroll up for the reason.
  echo      Manual fallback: install Google's USB driver by hand - see
  echo      provision\root\WINDOWS-RUNBOOK.md.
)

popd
endlocal
echo.
pause
