@echo off
REM ============================================================================
REM  setup-windows.bat  -  One-time Windows setup so CAS can FLASH (root/seal).
REM
REM  Why this exists:
REM    adb and fastboot use DIFFERENT USB drivers on Windows. The kit's adb driver
REM    is usually already there (Download + the on-device Magisk patch work), but the
REM    BOOTLOADER/fastboot interface needs its OWN driver that Windows does not ship.
REM    Without it, (0) Root patches the image fine, then the flash step reports
REM    "device did not enter fastboot" because `fastboot devices` is EMPTY.
REM    Linux gets this driver for free via udev, which is why the same unit roots there.
REM
REM  This script REBOOTS the unit to its bootloader and checks whether Windows can see
REM  it in fastboot; if not, it walks you through installing the WinUSB driver (Zadig).
REM  It does NOT flash or modify the device. Safe to re-run.
REM ============================================================================
setlocal enabledelayedexpansion
pushd "%~dp0"

REM platform-tools ships next to the exe (dist\cas\platform-tools) or one level up from this script.
set "ADB=adb"
set "FB=fastboot"
if exist "%~dp0..\platform-tools\adb.exe"      set "ADB=%~dp0..\platform-tools\adb.exe"
if exist "%~dp0..\platform-tools\fastboot.exe" set "FB=%~dp0..\platform-tools\fastboot.exe"
if exist "%~dp0platform-tools\adb.exe"         set "ADB=%~dp0platform-tools\adb.exe"
if exist "%~dp0platform-tools\fastboot.exe"    set "FB=%~dp0platform-tools\fastboot.exe"

echo ============================================================================
echo   CAS - Windows fastboot driver setup
echo   Goal: make Windows see the unit in fastboot so Root/Seal can flash.
echo   This reboots the unit to its bootloader; it does NOT flash anything.
echo ============================================================================
echo.

"%FB%" --version >nul 2>nul || ( echo [X] fastboot not found. It ships in platform-tools next to adb.exe.& goto :end )

REM --- Already in fastboot? Then the driver is already good. ---
"%FB%" devices 2>nul | findstr /i "fastboot" >nul
if not errorlevel 1 ( echo [ok] A unit is already visible in fastboot -- the driver is installed.& "%FB%" devices & goto :reboot_prompt )

"%ADB%" get-state 1>nul 2>nul
if errorlevel 1 (
  echo [!] No unit in adb mode. Put a unit in fastboot ^(Root leaves it there on failure^), or plug one in
  echo     with USB debugging on and tap Allow, then re-run this script.
  goto :end
)

set "GO="
set /p GO=Reboot the connected unit to its bootloader now? Type Y then Enter:
if /I not "!GO!"=="Y" ( echo cancelled - nothing changed.& goto :end )

echo [*] rebooting to bootloader...
"%ADB%" reboot bootloader 1>nul 2>nul

echo [*] waiting up to ~50s for it to appear in fastboot...
set "SEEN="
for /L %%i in (1,1,25) do (
  if not defined SEEN (
    "%FB%" devices 2>nul | findstr /i "fastboot" >nul && set "SEEN=1"
    if not defined SEEN ping -n 3 127.0.0.1 >nul
  )
)

if defined SEEN (
  echo.
  echo [ok] Windows sees the unit in fastboot -- the driver is ALREADY installed:
  "%FB%" devices
  echo     Root/Seal will flash fine. You're done.
  goto :reboot_prompt
)

echo.
echo ============================================================================
echo   [X] The unit is on its bootloader screen but Windows can't see it in fastboot.
echo       Install the WinUSB "Android Bootloader Interface" driver ONCE:
echo.
echo    1) Download Zadig (single .exe, no install):  https://zadig.akeo.ie
echo    2) Run Zadig.  Menu: Options -^> tick "List All Devices".
echo    3) In the dropdown pick the bootloader/fastboot device -- often shown as
echo       "Android Bootloader Interface", "Android", "Fastboot", or an "Unknown Device"
echo       that appeared when the unit rebooted.  Tip: Windows Device Manager shows it
echo       with a yellow warning triangle.
echo    4) Set the target driver to  WinUSB  and click "Install Driver" / "Replace Driver".
echo       Wait for "The driver was installed successfully."
echo    5) Come back here and press any key to re-check.
echo ============================================================================
echo.
pause

"%FB%" devices 2>nul | findstr /i "fastboot" >nul
if not errorlevel 1 (
  echo [ok] Fixed -- Windows now sees the unit in fastboot:
  "%FB%" devices
  echo     Re-run (0) Root in CAS; the flash will work now.
) else (
  echo [X] Still not visible. Re-open Zadig, confirm "List All Devices" is ticked, and pick the
  echo     entry that appears/disappears as you unplug-replug the unit in bootloader mode; install WinUSB.
)

:reboot_prompt
echo.
set "RB="
set /p RB=Reboot the unit back to Android now? Type Y then Enter:
if /I "!RB!"=="Y" "%FB%" reboot 1>nul 2>nul
echo   ^(If it does not reboot -- e.g. driver still missing -- just hold Power ~10s on the unit.^)

:end
popd
endlocal
echo.
pause
