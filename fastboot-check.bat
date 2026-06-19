@echo off
REM ============================================================
REM  console-auto-setup : FASTBOOT / BOOTLOADER CHECK
REM  Tests whether the bootloader is reachable + unlocked - the escalation path
REM  to ROOT if 'adb root' and Magisk both fail.
REM  WARNING: this REBOOTS the Odin into bootloader mode, checks it, then reboots
REM  back to Android. It does NOT flash or modify anything.
REM ============================================================
setlocal enabledelayedexpansion
pushd "%~dp0"

set "ADB=adb"
if exist "%~dp0..\odin-provisioning\platform-tools\adb.exe" set "ADB=%~dp0..\odin-provisioning\platform-tools\adb.exe"
if exist "%~dp0platform-tools\adb.exe" set "ADB=%~dp0platform-tools\adb.exe"
set "FB=fastboot"
if exist "%~dp0..\odin-provisioning\platform-tools\fastboot.exe" set "FB=%~dp0..\odin-provisioning\platform-tools\fastboot.exe"
if exist "%~dp0platform-tools\fastboot.exe" set "FB=%~dp0platform-tools\fastboot.exe"

echo ============================================================
echo   FASTBOOT / BOOTLOADER CHECK
echo   This REBOOTS the Odin to the bootloader, reads its lock state,
echo   then reboots back to Android. It does NOT flash anything.
echo ============================================================
echo.

"%FB%" --version >nul 2>nul
if errorlevel 1 ( echo [X] fastboot not found. It ships in platform-tools next to adb.exe. & goto :end )
echo [ok] fastboot found: %FB%

"%ADB%" get-state 1>nul 2>nul
if errorlevel 1 ( echo [X] No device in Android/adb mode. Plug in, USB debugging on, tap Allow. & goto :end )

set "GO="
set /p GO=Reboot the Odin to the bootloader now? Type Y then Enter to continue:
if /I not "!GO!"=="Y" ( echo cancelled - nothing changed. & goto :end )

echo [*] rebooting to bootloader...
"%ADB%" reboot bootloader 1>nul 2>nul

echo [*] waiting for the device to appear in fastboot (up to ~20s)...
set "FBDEV="
for /L %%i in (1,1,20) do (
  if not defined FBDEV (
    for /f "delims=" %%D in ('"%FB%" devices 2^>nul') do set "FBDEV=%%D"
    if not defined FBDEV ping -n 2 127.0.0.1 >nul
  )
)

if not defined FBDEV (
  echo.
  echo [X] Device did NOT appear in fastboot. Likely causes:
  echo     - Windows needs the bootloader USB driver for this device, or
  echo     - it didn't enter the bootloader (check the Odin screen).
  echo [*] trying to reboot back to Android...
  "%FB%" reboot 1>nul 2>nul
  goto :end
)
echo [ok] device visible in fastboot: !FBDEV!
echo.
echo --- bootloader variables ---
"%FB%" getvar unlocked       2>&1 | findstr /i "unlocked"
"%FB%" getvar unlock-ability 2>&1 | findstr /i "unlock-ability"
"%FB%" getvar secure         2>&1 | findstr /i "secure"
"%FB%" getvar product        2>&1 | findstr /i "product"

echo.
echo [*] rebooting back to Android...
"%FB%" reboot 1>nul 2>nul

echo.
echo ============================================================
echo   READ-OUT
echo   - device reached fastboot           : YES (bootloader talks to the PC)
echo   - unlocked: yes  -^> you CAN temp-boot a patched boot image to get root
echo                       (the escalation if adb root + Magisk are both NO)
echo   - unlocked: no   -^> bootloader locked; fastboot CANNOT grant root here.
echo                       Use adb root / Magisk / backup-restore instead.
echo.
echo   NOTE: fastboot itself does NOT set up emulators or map storage. It is
echo   only a route to ROOT, which in turn lets us clone the stubborn bits
echo   (SAF folder grants, /data/data settings) for identical plug-and-play state.
echo ============================================================
:end
popd
endlocal
pause
